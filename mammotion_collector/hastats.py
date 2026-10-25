"""Imports the mower's daily history into Home Assistant's long-term statistics.

Which series exist and what they mean is decided by mammotion-luba's collector.report.statistics();
this module only delivers them. They become external statistics `mammotion_collector:<key>` (source
`mammotion_collector`), so HA's statistics cards can show the whole history since collection began
(2026-10-07), not just the recorder's state history. Each series has one row per local day, at the
day's first hour; sums run from the first day, so every run simply re-sends all rows (a few hundred a
year), and HA's `recorder/import_statistics` replaces rows by hour.

Delivery: HA Core's WebSocket API through the Supervisor proxy (ws://supervisor/core/websocket,
`homeassistant_api: true`). The WebSocket client is the eero Collector add-on's (hastats.py there).
Nothing here talks to Mammotion.
"""

import base64
import datetime
import json
import os
import pathlib
import socket
import struct

from collector import report

SOURCE = "mammotion_collector"
CHUNK = 500  # rows per import message
INTERVAL = 3600
MEAN_TYPE = {"sum": 0, "mean": 1}  # HA's StatisticMeanType: NONE, ARITHMETIC


# --- minimal WebSocket client (RFC 6455; text frames, client-side masking) ---------------------------------
def encode_frame(payload, opcode=0x1):
    data = payload.encode() if isinstance(payload, str) else payload
    n = len(data)
    head = bytes([0x80 | opcode])
    if n < 126:
        head += bytes([0x80 | n])
    elif n < 1 << 16:
        head += bytes([0x80 | 126]) + struct.pack("!H", n)
    else:
        head += bytes([0x80 | 127]) + struct.pack("!Q", n)
    mask = os.urandom(4)
    return head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data))


def _recv_exact(sock, n):
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("WebSocket closed")
        buf += chunk
    return bytes(buf)


def read_frame(sock):
    """Returns (fin, opcode, payload)."""
    b1, b2 = _recv_exact(sock, 2)
    n = b2 & 0x7F
    if n == 126:
        n = struct.unpack("!H", _recv_exact(sock, 2))[0]
    elif n == 127:
        n = struct.unpack("!Q", _recv_exact(sock, 8))[0]
    mask = _recv_exact(sock, 4) if b2 & 0x80 else None
    data = _recv_exact(sock, n)
    if mask:
        data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
    return bool(b1 & 0x80), b1 & 0x0F, data


class HAWebSocket:
    def __init__(self, token, host="supervisor", port=80, path="/core/websocket", sock=None, timeout=60):
        self.token, self.next_id = token, 1
        self.sock = sock or socket.create_connection((host, port), timeout=timeout)
        if sock is None:
            key = base64.b64encode(os.urandom(16)).decode()
            self.sock.sendall((f"GET {path} HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                               f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
            head = b""
            while not head.endswith(b"\r\n\r\n"):  # byte by byte: the first frame can follow in the same packet
                chunk = self.sock.recv(1)
                if not chunk:
                    raise ConnectionError("no WebSocket handshake")
                head += chunk
            if b" 101 " not in head.split(b"\r\n", 1)[0]:
                raise ConnectionError(head.split(b"\r\n", 1)[0].decode(errors="replace"))
        if self.recv().get("type") != "auth_required":
            raise ConnectionError("unexpected greeting")
        self.send({"type": "auth", "access_token": self.token})
        reply = self.recv()
        if reply.get("type") != "auth_ok":
            raise PermissionError(f"Home Assistant refused the token ({reply.get('type')})")

    def send(self, msg):
        self.sock.sendall(encode_frame(json.dumps(msg)))

    def recv(self):
        parts = []
        while True:
            fin, opcode, data = read_frame(self.sock)
            if opcode == 0x8:
                raise ConnectionError("WebSocket closed by Home Assistant")
            if opcode == 0x9:
                self.sock.sendall(encode_frame(data, 0xA))
                continue
            if opcode in (0x1, 0x0):
                parts.append(data)
                if fin:
                    return json.loads(b"".join(parts))

    def call(self, msg):
        msg = {**msg, "id": self.next_id}
        self.next_id += 1
        self.send(msg)
        while True:
            reply = self.recv()
            if reply.get("id") == msg["id"] and reply.get("type") == "result":
                if not reply.get("success"):
                    raise RuntimeError(f"{msg['type']}: {reply.get('error')}")
                return reply.get("result")

    def close(self):
        try:
            self.sock.sendall(encode_frame(b"", 0x8))
        except OSError:
            pass
        self.sock.close()


# --- import ------------------------------------------------------------------------------------------------
def iso(epoch):
    return datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc).isoformat()


def messages(series):
    """recorder/import_statistics messages for report.statistics() output, CHUNK rows each."""
    out = []
    for key, s in series.items():
        metadata = {"statistic_id": f"{SOURCE}:{key}", "source": SOURCE, "name": s["name"],
                    "unit_of_measurement": s["unit"], "unit_class": s["unit_class"],
                    "has_sum": s["kind"] == "sum", "mean_type": MEAN_TYPE[s["kind"]]}
        for i in range(0, len(s["rows"]), CHUNK):
            stats = [{**{k: v for k, v in r.items() if k != "start"}, "start": iso(r["start"])}
                     for r in s["rows"][i:i + CHUNK]]
            out.append({"type": "recorder/import_statistics", "metadata": metadata, "stats": stats})
    return out


class Importer:
    def __init__(self, token, db_path, analysis, log, connect=HAWebSocket):
        """`analysis()` returns (report.analyze() result, time zone) for the mower."""
        self.token, self.db_path, self.analysis, self.log, self.connect = token, db_path, analysis, log, connect

    def import_once(self):
        result, tz = self.analysis()
        msgs = messages(report.statistics(result, tz))
        if msgs:
            ws = self.connect(self.token)
            try:
                for m in msgs:
                    ws.call(m)
            finally:
                ws.close()
        return sum(len(m["stats"]) for m in msgs)

    def run(self, stop, first_delay=120):
        """Imports once an hour. The first import waits `first_delay` seconds, so a startup rebuild of
        derived history (the collector's derivation version check) has finished."""
        first, failing = True, False
        if stop.wait(first_delay):
            return
        while not stop.is_set():
            try:
                if pathlib.Path(self.db_path).exists():
                    rows = self.import_once()
                    if first or failing:
                        self.log(f"imported {rows} daily statistics rows into Home Assistant")
                    first = False
                failing = False
            except Exception as e:  # never let the import affect collection or publishing
                if not failing:
                    self.log(f"statistics import failed: {e}; retrying every hour")
                failing = True
            stop.wait(INTERVAL)
