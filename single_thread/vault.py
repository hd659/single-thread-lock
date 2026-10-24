"""Zero-list vault: RAM-to-Disk brain-dump sink + 10-minute micro-slice decomposer.

Design invariants
-----------------
1. Write-only backlog: `dump()` accepts thoughts, but there is NO API returning the
   backlog as a list. The only read surfaces are `pending_count()` (an integer) and
   `acquire()` (exactly one slice).
2. Single aperture: at most one slice may be ACTIVE at any time.
3. Ground-state gate: `acquire()` requires a completed, unconsumed reset of at least
   `RESET_SECONDS` since the last acquisition.
4. Every exposed slice is budgeted to <= `SLICE_MINUTES` minutes.
"""
from __future__ import annotations

import re
import sqlite3
import time
from dataclasses import dataclass
from typing import Callable, List, Optional

SLICE_MINUTES = 10
RESET_SECONDS = 60
DEFAULT_MINUTES = SLICE_MINUTES

_SPLIT_RE = re.compile(r"(?:\n+|;|(?<=[.!?。！？])\s*|\s+(?:then|and then|after that)\s+|然后|接着)", re.I)
_EST_RE = re.compile(r"[\(\[]\s*~?(\d+)\s*(m|min|mins|minutes|h|hr|hrs|hours)\s*[\)\]]", re.I)

PENDING, ACTIVE, DONE, DROPPED = "pending", "active", "done", "dropped"


class LockError(RuntimeError):
    """Raised when the single-aperture or ground-state invariant would be violated."""


@dataclass(frozen=True)
class Slice:
    id: int
    dump_id: int
    seq: int
    action: str
    minutes: int
    state: str


def _estimate(fragment: str) -> tuple[str, int]:
    m = _EST_RE.search(fragment)
    if not m:
        return fragment.strip(), DEFAULT_MINUTES
    n, unit = int(m.group(1)), m.group(2).lower()
    minutes = n * 60 if unit.startswith("h") else n
    return _EST_RE.sub("", fragment).strip(), max(1, minutes)


def decompose(text: str, budget: int = SLICE_MINUTES) -> List[tuple[str, int]]:
    """Split raw dump text into (action, minutes) pairs, each <= budget minutes.

    Fragments estimated above budget are chopped into ceil(m / budget) parts.
    """
    if budget <= 0:
        raise ValueError("budget must be positive")
    out: List[tuple[str, int]] = []
    for frag in _SPLIT_RE.split(text or ""):
        if not frag or not frag.strip(" \t-*•.,，、"):
            continue
        action, minutes = _estimate(frag.strip(" \t-*•"))
        action = action.rstrip(" .;。")
        if not action:
            continue
        parts = -(-minutes // budget)
        if parts == 1:
            out.append((action, minutes))
            continue
        remaining = minutes
        for i in range(1, parts + 1):
            chunk = min(budget, remaining)
            remaining -= chunk
            out.append((f"{action} [part {i}/{parts}]", chunk))
    return out


_SCHEMA = """
CREATE TABLE IF NOT EXISTS dumps(
    id INTEGER PRIMARY KEY, raw TEXT NOT NULL, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS slices(
    id INTEGER PRIMARY KEY,
    dump_id INTEGER NOT NULL REFERENCES dumps(id),
    seq INTEGER NOT NULL,
    action TEXT NOT NULL,
    minutes INTEGER NOT NULL CHECK(minutes BETWEEN 1 AND 10),
    state TEXT NOT NULL DEFAULT 'pending',
    started_at REAL, ended_at REAL);
CREATE UNIQUE INDEX IF NOT EXISTS one_active ON slices(state) WHERE state='active';
CREATE TABLE IF NOT EXISTS resets(
    id INTEGER PRIMARY KEY, started_at REAL NOT NULL, ended_at REAL NOT NULL,
    consumed INTEGER NOT NULL DEFAULT 0);
"""


class Vault:
    def __init__(self, path: str = ":memory:", clock: Callable[[], float] = time.time):
        self._db = sqlite3.connect(path, isolation_level=None)
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.executescript(_SCHEMA)
        self._clock = clock

    def close(self) -> None:
        self._db.close()

    # ---- RAM -> Disk ------------------------------------------------------
    def dump(self, text: str) -> int:
        """Persist a thought and its micro-slices. Returns number of slices created."""
        pieces = decompose(text)
        if not pieces:
            return 0
        with self._tx():
            cur = self._db.execute(
                "INSERT INTO dumps(raw, created_at) VALUES (?, ?)", (text, self._clock()))
            self._db.executemany(
                "INSERT INTO slices(dump_id, seq, action, minutes) VALUES (?,?,?,?)",
                [(cur.lastrowid, i, a, m) for i, (a, m) in enumerate(pieces)])
        return len(pieces)

    def pending_count(self) -> int:
        return self._db.execute(
            "SELECT COUNT(*) FROM slices WHERE state=?", (PENDING,)).fetchone()[0]

    # ---- Ground-state gate -------------------------------------------------
    def begin_reset(self) -> float:
        return self._clock()

    def end_reset(self, started_at: float) -> bool:
        """Record a reset; returns True iff it lasted >= RESET_SECONDS."""
        ended = self._clock()
        if ended - started_at < RESET_SECONDS:
            return False
        self._db.execute(
            "INSERT INTO resets(started_at, ended_at) VALUES (?, ?)", (started_at, ended))
        return True

    def grounded(self) -> bool:
        return self._db.execute(
            "SELECT 1 FROM resets WHERE consumed=0 LIMIT 1").fetchone() is not None

    # ---- Single aperture ---------------------------------------------------
    def active(self) -> Optional[Slice]:
        return self._one("WHERE state=?", (ACTIVE,))

    def acquire(self) -> Optional[Slice]:
        """Expose exactly one slice. Requires a fresh reset and no active slice."""
        if self.active():
            raise LockError("a slice is already active; finish or drop it first")
        if not self.grounded():
            raise LockError(f"ground-state reset (>= {RESET_SECONDS}s) required")
        nxt = self._one("WHERE state=? ORDER BY dump_id, seq LIMIT 1", (PENDING,))
        if nxt is None:
            return None
        with self._tx():
            self._db.execute(
                "UPDATE resets SET consumed=1 WHERE id=(SELECT MIN(id) FROM resets WHERE consumed=0)")
            self._db.execute(
                "UPDATE slices SET state=?, started_at=? WHERE id=?", (ACTIVE, self._clock(), nxt.id))
        return self.active()

    def complete(self) -> Slice:
        return self._close(DONE)

    def drop(self) -> Slice:
        return self._close(DROPPED)

    def release(self) -> Slice:
        """Return the active slice to the front of the queue (no reset refund)."""
        cur = self._require_active()
        self._db.execute(
            "UPDATE slices SET state=?, started_at=NULL WHERE id=?", (PENDING, cur.id))
        return cur

    def overrun(self) -> bool:
        cur = self.active()
        if cur is None:
            return False
        started = self._db.execute(
            "SELECT started_at FROM slices WHERE id=?", (cur.id,)).fetchone()[0]
        return self._clock() - started > cur.minutes * 60

    # ---- internals ---------------------------------------------------------
    def _close(self, state: str) -> Slice:
        cur = self._require_active()
        self._db.execute(
            "UPDATE slices SET state=?, ended_at=? WHERE id=?", (state, self._clock(), cur.id))
        return Slice(cur.id, cur.dump_id, cur.seq, cur.action, cur.minutes, state)

    def _require_active(self) -> Slice:
        cur = self.active()
        if cur is None:
            raise LockError("no active slice")
        return cur

    def _one(self, where: str, args: tuple) -> Optional[Slice]:
        row = self._db.execute(
            f"SELECT id, dump_id, seq, action, minutes, state FROM slices {where}", args).fetchone()
        return Slice(*row) if row else None

    def _tx(self):
        db = self._db

        class _T:
            def __enter__(self):
                db.execute("BEGIN")

            def __exit__(self, et, *_):
                db.execute("ROLLBACK" if et else "COMMIT")
                return False

        return _T()
