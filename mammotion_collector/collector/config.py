"""Settings from environment variables / the git-ignored .env file, plus small shared helpers."""

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_dotenv(path: Path = ROOT / ".env") -> None:
    """Minimal .env reader; real environment variables take precedence."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


@dataclass
class Settings:
    client_id: str
    client_secret: str
    db_path: Path
    poll_seconds: int = 30
    error_sync_seconds: int = 20 * 60
    plan_sync_seconds: int = 6 * 60 * 60
    work_params_min_seconds: int = 60 * 60  # /work-params sends a command to the mower; keep it rare
    max_backoff_seconds: int = 10 * 60
    gap_threshold_seconds: int = 180  # a longer hole in observations makes a session "uncertain"

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()
        poll = max(10, int(os.environ.get("MAMMOTION_POLL_SECONDS", "30")))
        return cls(
            client_id=os.environ.get("MAMMOTION_CLIENT_ID", "").strip(),
            client_secret=os.environ.get("MAMMOTION_CLIENT_SECRET", "").strip(),
            db_path=Path(os.environ.get("MAMMOTION_DB_PATH", str(ROOT / "data" / "mammotion.db"))),
            poll_seconds=poll,
            gap_threshold_seconds=max(180, 6 * poll),
        )


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_ts(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


def seconds_between(a: str, b: str) -> int:
    return int((parse_ts(b) - parse_ts(a)).total_seconds())


def mask(value) -> str:
    """Never show identifiers in full: keep 3 leading and 2 trailing characters."""
    if value is None:
        return "-"
    s = str(value)
    return "****" if len(s) <= 6 else f"{s[:3]}…{s[-2:]}"
