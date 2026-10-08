"""Where the eero session token lives. The token is a credential: never print or log it."""

import json
import os
import subprocess
import time
from pathlib import Path


class FileSessionStore:
    """A JSON file readable only by its owner (the add-on keeps it in /data, which is private to it)."""

    def __init__(self, path):
        self.path = Path(path)

    def load(self):
        try:
            return json.loads(self.path.read_text()).get("token")
        except (OSError, ValueError):
            return None

    def save(self, token):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({"token": token, "saved_at": int(time.time())}, f)
        os.replace(tmp, self.path)

    def clear(self):
        self.path.unlink(missing_ok=True)

    def saved_at(self):
        try:
            return json.loads(self.path.read_text()).get("saved_at")
        except (OSError, ValueError):
            return None


class KeychainSessionStore:
    """macOS Keychain, for running the collector on the Mac during development."""

    def __init__(self, service="eero-network", account="home-assistant"):
        self.service, self.account = service, account

    def load(self):
        out = subprocess.run(["security", "find-generic-password", "-s", self.service, "-a", self.account, "-w"],
                             capture_output=True, text=True)
        return out.stdout.strip() if out.returncode == 0 else None

    def save(self, token):
        subprocess.run(["security", "add-generic-password", "-U", "-s", self.service, "-a", self.account,
                        "-w", token], check=True, capture_output=True)

    def clear(self):
        subprocess.run(["security", "delete-generic-password", "-s", self.service, "-a", self.account],
                       capture_output=True)

    def saved_at(self):
        return None
