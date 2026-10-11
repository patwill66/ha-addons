"""Imports eero-network's hourly history into Home Assistant's long-term statistics.

Which series exist and what they mean is decided by eero-network's collector.report.statistics();
this module only delivers them. They become external statistics `eero_collector:<key>` (source
`eero_collector`), separate from the MQTT sensors' own statistics, so HA's statistics cards can show
the history the collector backfilled from eero (network usage since eero's history starts, speed tests
back about a year) and keep it current.

Delivery: HA Core's WebSocket API through the Supervisor proxy (ws://supervisor/core/websocket,
`homeassistant_api: true`), command `recorder/import_statistics`, which inserts or replaces rows by
hour, so re-sending recent hours is safe. The first run sends everything; later runs, once an hour,
resend the last LOOKBACK hours (eero completes and corrects recent hours). The cursor lives in
/data/statistics.json; deleting it re-imports everything. Nothing here talks to eero.
"""

import base64
import datetime
import json
import os
import pathlib
import socket
import sqlite3
import struct
import time

from collector import report

SOURCE = "eero_collector"
STATE = pathlib.Path("/data/statistics.json")
LOOKBACK = 48 * 3600
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
            while b"\r\n\r\n" not in head:
                chunk = self.sock.recv(1024)
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
    for key, rows in series.items():
        name, unit, unit_class, kind = report.STATISTICS[key]
        metadata = {"statistic_id": f"{SOURCE}:{key}", "source": SOURCE, "name": name, "unit_of_measurement": unit,
                    "unit_class": unit_class, "has_sum": kind == "sum", "mean_type": MEAN_TYPE[kind]}
        for i in range(0, len(rows), CHUNK):
            stats = [{**{k: v for k, v in r.items() if k != "start"}, "start": iso(r["start"])}
                     for r in rows[i:i + CHUNK]]
            out.append({"type": "recorder/import_statistics", "metadata": metadata, "stats": stats})
    return out


class Importer:
    def __init__(self, token, db_path, log, connect=HAWebSocket, state=STATE):
        self.token, self.db_path, self.log, self.connect, self.state = token, db_path, log, connect, state

    def cursor(self):
        try:
            return json.loads(self.state.read_text()).get("through")
        except (OSError, ValueError):
            return None

    def import_once(self):
        through = self.cursor()
        since = through - LOOKBACK if through else 0
        db = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            series = report.statistics(db, since)
        finally:
            db.close()
        msgs = messages(series)
        rows = sum(len(m["stats"]) for m in msgs)
        if msgs:
            ws = self.connect(self.token)
            try:
                for m in msgs:
                    ws.call(m)
            finally:
                ws.close()
        latest = max((r["start"] for rows_ in series.values() for r in rows_), default=through)
        if latest:
            self.state.write_text(json.dumps({"through": latest}))
        return rows, through is None

    def run(self, stop):
        failing = False
        while not stop.is_set():
            try:
                if pathlib.Path(self.db_path).exists():
                    rows, first = self.import_once()
                    if first or failing:
                        self.log(f"imported {rows} hourly statistics rows into Home Assistant"
                                 + (" (full history)" if first else ""))
                failing = False
            except Exception as e:  # never let the import affect collection or publishing
                if not failing:
                    self.log(f"statistics import failed: {e}; retrying every hour")
                failing = True
            stop.wait(INTERVAL)
