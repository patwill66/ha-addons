"""Minimal MQTT 3.1.1 client (standard library only): CONNECT with a last will, QoS 0 PUBLISH (retained or
not), SUBSCRIBE, PINGREQ, DISCONNECT. A reader thread answers pings and passes incoming PUBLISH packets
to `on_message(topic, payload)`. Enough for Home Assistant MQTT discovery; nothing more.
"""

import socket
import struct
import threading
import time


def _varint(n):
    out = bytearray()
    while True:
        byte, n = n % 128, n // 128
        out.append(byte | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _str(s):
    b = s.encode() if isinstance(s, str) else s
    return struct.pack("!H", len(b)) + b


def packet(kind, body):
    return bytes([kind]) + _varint(len(body)) + body


def connect_packet(client_id, keepalive, username=None, password=None, will_topic=None, will_payload=None,
                   will_retain=True):
    flags = 0x02  # clean session
    payload = _str(client_id)
    if will_topic:
        flags |= 0x04 | (0x20 if will_retain else 0)
        payload += _str(will_topic) + _str(will_payload or b"")
    if username:
        flags |= 0x80
        payload += _str(username)
        if password:
            flags |= 0x40
            payload += _str(password)
    return packet(0x10, _str("MQTT") + bytes([4, flags]) + struct.pack("!H", keepalive) + payload)


def publish_packet(topic, payload, retain=False):
    body = payload.encode() if isinstance(payload, str) else payload
    return packet(0x30 | (0x01 if retain else 0), _str(topic) + body)


def subscribe_packet(packet_id, topic):
    return packet(0x82, struct.pack("!H", packet_id) + _str(topic) + b"\x00")


def read_packet(sock):
    """Returns (type byte, body) or raises ConnectionError."""
    head = _recv(sock, 1)[0]
    mult, length = 1, 0
    while True:
        b = _recv(sock, 1)[0]
        length += (b & 0x7F) * mult
        if not b & 0x80:
            break
        mult *= 128
    return head, _recv(sock, length) if length else b""


def _recv(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("MQTT connection closed")
        buf += chunk
    return buf


class Client:
    def __init__(self, host, port, username=None, password=None, client_id="eero_collector",
                 will_topic=None, will_payload="offline", keepalive=60, on_message=None, connector=None):
        self.host, self.port = host, port
        self.username, self.password = username, password
        self.client_id, self.keepalive = client_id, keepalive
        self.will_topic, self.will_payload = will_topic, will_payload
        self.on_message = on_message or (lambda t, p: None)
        self.connector = connector or (lambda: socket.create_connection((self.host, self.port), timeout=15))
        self.sock = None
        self.lock = threading.Lock()
        self.last_sent = 0
        self.alive = False
        self._packet_id = 0

    def connect(self):
        sock = self.connector()
        sock.sendall(connect_packet(self.client_id, self.keepalive, self.username, self.password,
                                    self.will_topic, self.will_payload))
        kind, body = read_packet(sock)
        if kind != 0x20 or len(body) < 2 or body[1] != 0:
            sock.close()
            raise ConnectionError(f"MQTT broker refused the connection (code {body[1] if len(body) > 1 else '?'})")
        sock.settimeout(None)
        self.sock, self.alive, self.last_sent = sock, True, time.monotonic()
        threading.Thread(target=self._reader, daemon=True).start()

    def _send(self, data):
        with self.lock:
            if not self.alive:
                raise ConnectionError("MQTT not connected")
            try:
                self.sock.sendall(data)
            except OSError as e:
                self.alive = False
                raise ConnectionError(f"MQTT send failed: {e}") from None
            self.last_sent = time.monotonic()

    def publish(self, topic, payload, retain=False):
        self._send(publish_packet(topic, payload, retain))

    def subscribe(self, topic):
        self._packet_id = self._packet_id % 65535 + 1
        self._send(subscribe_packet(self._packet_id, topic))

    def ping_if_idle(self):
        if self.alive and time.monotonic() - self.last_sent > self.keepalive / 2:
            self._send(b"\xc0\x00")

    def disconnect(self):
        try:
            self._send(b"\xe0\x00")
        except ConnectionError:
            pass
        self.close()

    def close(self):
        self.alive = False
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass

    def _reader(self):
        try:
            while self.alive:
                kind, body = read_packet(self.sock)
                if kind & 0xF0 == 0x30:  # PUBLISH (QoS 0 from our subscriptions)
                    n = struct.unpack("!H", body[:2])[0]
                    self.on_message(body[2:2 + n].decode(errors="replace"), body[2 + n:])
        except (ConnectionError, OSError):
            pass
        self.alive = False
