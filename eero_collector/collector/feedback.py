"""Network notes: what the user noticed while using the network (a Teams call froze, a page stalled, a roam).

Each note is timestamped when it is entered and tagged with the mesh test running then, so it can later be
lined up with what the mesh and the devices did around that moment (report.incident, meshtest). Notes are
free text from the user's own household: they never leave the database.
"""

MAX_LEN = 1000  # Home Assistant's text entities stop at 255; the cap only guards the table


COLUMNS = ("id", "ts", "text", "experiment", "source")


class FeedbackError(ValueError):
    pass


def add(db, text, now, source=None):
    """Stores one note at `now` and returns it. Refuses an empty note."""
    text = " ".join((text or "").split())[:MAX_LEN]
    if not text:
        raise FeedbackError("empty note")
    r = db.execute("SELECT code FROM experiments WHERE kind = 'trial' AND ended_at IS NULL "
                   "ORDER BY started_at DESC LIMIT 1").fetchone()
    experiment = r[0] if r else None
    with db:
        cur = db.execute("INSERT INTO feedback(ts, text, experiment, source) VALUES(?, ?, ?, ?)",
                         (now, text, experiment, source))
    return {"id": cur.lastrowid, "ts": now, "text": text, "experiment": experiment, "source": source}


def between(db, lo, hi):
    """Notes with lo <= ts < hi, oldest first."""
    return [dict(zip(COLUMNS, r)) for r in db.execute(
        "SELECT id, ts, text, experiment, source FROM feedback WHERE ts >= ? AND ts < ? ORDER BY ts, id", (lo, hi))]


def recent(db, limit=20):
    """The newest notes, newest first."""
    return [dict(zip(COLUMNS, r)) for r in db.execute(
        "SELECT id, ts, text, experiment, source FROM feedback ORDER BY ts DESC, id DESC LIMIT ?", (limit,))]


def count(db):
    return db.execute("SELECT count(*) FROM feedback").fetchone()[0]
