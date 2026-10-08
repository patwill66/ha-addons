"""Serves consistent snapshots of the collector database through Home Assistant ingress.

GET /snapshot makes a copy of the database with SQLite's online backup API from a read-only
connection (so rows still in the -wal are included and the collector keeps writing), streams it as a
download and deletes the temporary copy. GET / is a page with a download link. Only the Supervisor's
ingress proxy may connect: Home Assistant authenticates the user and the ingress session, so the
server itself has no credentials. It never writes to the database.
"""
import datetime
import hashlib
import http.server
import pathlib
import sqlite3
import tempfile
import threading

PORT = 8099  # ingress_port in config.yaml
INGRESS_PROXY = "172.30.32.2"
CHUNK = 1 << 16

PAGE = b"""<!doctype html><meta charset="utf-8"><title>Mammotion Collector</title>
<body style="font-family:sans-serif;margin:2em">
<h1>Mammotion Collector</h1>
<p><a href="snapshot">Download a database snapshot</a> (a consistent copy, made while the collector keeps running).</p>
"""


def make_handler(db_path, log):
    lock = threading.Lock()  # one snapshot at a time

    class Handler(http.server.BaseHTTPRequestHandler):
        server_version = "MammotionCollector"

        def do_GET(self):
            if self.client_address[0] != INGRESS_PROXY:
                self.send_error(403)
                return
            path = self.path.split("?", 1)[0]
            if path == "/":
                self.reply(200, "text/html; charset=utf-8", PAGE)
            elif path == "/snapshot":
                self.snapshot()
            else:
                self.send_error(404)

        def reply(self, code, content_type, body):
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def snapshot(self):
            stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            with lock, tempfile.TemporaryDirectory(prefix="snapshot-") as tmp:
                copy = pathlib.Path(tmp) / "mammotion.db"
                try:
                    src = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
                    dst = sqlite3.connect(copy)
                    try:
                        src.backup(dst)
                    finally:
                        src.close()
                        dst.close()
                except sqlite3.Error as e:
                    log(f"snapshot failed: {e}")
                    self.send_error(500, "snapshot failed")
                    return
                digest = hashlib.sha256()
                with copy.open("rb") as f:
                    for block in iter(lambda: f.read(CHUNK), b""):
                        digest.update(block)
                size = copy.stat().st_size
                self.send_response(200)
                self.send_header("Content-Type", "application/vnd.sqlite3")
                self.send_header("Content-Length", str(size))
                self.send_header("Content-Disposition", f'attachment; filename="mammotion-{stamp}.db"')
                self.send_header("X-Snapshot-Sha256", digest.hexdigest())
                self.end_headers()
                with copy.open("rb") as f:
                    for block in iter(lambda: f.read(CHUNK), b""):
                        self.wfile.write(block)
            log(f"served snapshot ({size // 1024} KB)")

        def log_message(self, *args):
            pass  # snapshots are logged above; ingress page loads aren't worth a line

    return Handler


def run(db_path, log):
    server = http.server.ThreadingHTTPServer(("0.0.0.0", PORT), make_handler(db_path, log))
    server.daemon_threads = True
    log(f"snapshot server listening on port {PORT} (ingress only)")
    server.serve_forever()
