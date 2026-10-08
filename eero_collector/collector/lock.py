"""Single-instance guard: two collectors on one database would double the requests to eero and
race on the same session token, so a second instance refuses to start before it makes any request.

Uses an exclusive, non-blocking flock() on a lock file. The kernel releases the lock when the
holding process exits for any reason (crash, kill -9), so a stale file never blocks a restart;
the PID written inside is informational only.
"""

import fcntl
import os


class AlreadyRunning(Exception):
    def __init__(self, path, pid):
        super().__init__(f"another collector is already running (pid {pid or 'unknown'}; lock {path})")
        self.path, self.pid = path, pid


class InstanceLock:
    def __init__(self, path):
        self.path = path
        self._fh = None

    def acquire(self) -> "InstanceLock":
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        fh = open(self.path, "a+")
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            fh.seek(0)
            pid = fh.read().strip() or None
            fh.close()
            raise AlreadyRunning(self.path, pid) from None
        fh.seek(0)
        fh.truncate()
        fh.write(str(os.getpid()))
        fh.flush()
        self._fh = fh
        return self

    def release(self) -> None:
        if self._fh:
            try:
                self._fh.seek(0)
                self._fh.truncate()
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            finally:
                self._fh.close()
                self._fh = None

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *exc):
        self.release()
