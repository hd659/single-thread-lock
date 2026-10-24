"""Terminal aperture: one slice visible, backlog sealed, 60s ground-state reset gate.

Commands
--------
  stl dump "text"     sink thoughts to disk (stdin if text omitted); prints only a receipt
  stl reset           60-second ground-state countdown; Ctrl-C aborts and records nothing
  stl next            open the aperture: expose exactly one 10-minute slice
  stl focus           reset + next in one go
  stl show            re-render the active slice (never anything else)
  stl done|drop|release
  stl status          lock state; the backlog stays sealed (no items, no count by default)

Anti-peek lock: no command prints a pending slice. `status` shows the backlog only as
"sealed" unless `--count` is passed, and even then it is just an integer.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from typing import Callable, List, Optional, TextIO

from .vault import RESET_SECONDS, LockError, Slice, Vault

DEFAULT_DB = os.path.join(os.path.expanduser("~"), ".single_thread", "vault.db")
BAR = 30

EXIT_OK, EXIT_LOCKED, EXIT_ABORT = 0, 2, 130


def db_path(explicit: Optional[str] = None) -> str:
    path = explicit or os.environ.get("STL_DB") or DEFAULT_DB
    if path != ":memory:":
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    return path


def render_aperture(s: Slice, elapsed: float = 0.0) -> str:
    """Single-slice frame. Contains the active action and its budget, nothing else."""
    budget = s.minutes * 60
    left = budget - elapsed
    filled = min(BAR, int(BAR * max(0.0, elapsed) / budget))
    clock = f"{int(left // 60):02d}:{int(left % 60):02d}" if left >= 0 else f"+{int(-left // 60):02d}:{int(-left % 60):02d}"
    width = max(len(s.action), BAR + 10) + 4
    lines = [
        "+" + "-" * width + "+",
        "|  " + s.action.ljust(width - 2) + "|",
        "|  " + f"[{'#' * filled}{'.' * (BAR - filled)}] {clock}".ljust(width - 2) + "|",
        "+" + "-" * width + "+",
        "  done | drop | release",
    ]
    if left < 0:
        lines.append("  over budget: finish this slice or `release` it.")
    return "\n".join(lines)


def run_reset(v: Vault, out: TextIO, sleep: Callable[[float], None] = time.sleep,
              seconds: int = RESET_SECONDS) -> bool:
    """Blocking countdown. No slice content is shown while resetting."""
    if v.active():
        raise LockError("finish, drop or release the active slice before resetting")
    started = v.begin_reset()
    out.write("ground-state reset: feet on floor, exhale, eyes off screen.\n")
    try:
        for remaining in range(seconds, 0, -1):
            out.write(f"\r  {remaining:3d}s ")
            out.flush()
            sleep(1)
    except KeyboardInterrupt:
        out.write("\nreset aborted; nothing recorded.\n")
        return False
    ok = v.end_reset(started)
    out.write("\rgrounded. aperture unlocked for one slice.\n" if ok else
              "\rreset too short; try again.\n")
    return ok


def _elapsed(v: Vault, s: Slice) -> float:
    row = v._db.execute("SELECT started_at FROM slices WHERE id=?", (s.id,)).fetchone()
    return v._clock() - row[0] if row and row[0] is not None else 0.0


def _open(v: Vault, out: TextIO) -> int:
    s = v.acquire()
    if s is None:
        out.write("vault empty. nothing to do; that is allowed.\n")
        return EXIT_OK
    out.write(render_aperture(s) + "\n")
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="stl", description="single-aperture execution lock")
    p.add_argument("--db", help=f"vault path (env STL_DB, default {DEFAULT_DB})")
    sub = p.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("dump", help="sink thoughts to disk")
    d.add_argument("text", nargs="*")
    sub.add_parser("reset", help="60s ground-state reset")
    sub.add_parser("next", help="expose one slice")
    sub.add_parser("focus", help="reset then expose one slice")
    sub.add_parser("show", help="re-render the active slice")
    for name in ("done", "drop", "release"):
        sub.add_parser(name)
    st = sub.add_parser("status", help="lock state (backlog sealed)")
    st.add_argument("--count", action="store_true", help="reveal pending count (integer only)")
    return p


def main(argv: Optional[List[str]] = None, *, vault: Optional[Vault] = None,
         out: TextIO = sys.stdout, stdin: TextIO = sys.stdin,
         sleep: Callable[[float], None] = time.sleep) -> int:
    args = build_parser().parse_args(argv)
    v = vault or Vault(db_path(args.db))
    try:
        return _dispatch(args, v, out, stdin, sleep)
    except LockError as e:
        out.write(f"locked: {e}\n")
        return EXIT_LOCKED
    finally:
        if vault is None:
            v.close()


def _dispatch(args, v: Vault, out: TextIO, stdin: TextIO, sleep) -> int:
    cmd = args.cmd
    if cmd == "dump":
        text = " ".join(args.text) if args.text else stdin.read()
        n = v.dump(text)
        out.write(f"sunk {n} slice(s) to disk. RAM released.\n" if n else "nothing to sink.\n")
        return EXIT_OK
    if cmd == "reset":
        return EXIT_OK if run_reset(v, out, sleep) else EXIT_ABORT
    if cmd == "next":
        return _open(v, out)
    if cmd == "focus":
        if not v.grounded() and not run_reset(v, out, sleep):
            return EXIT_ABORT
        return _open(v, out)
    if cmd == "show":
        s = v.active()
        if s is None:
            raise LockError("aperture closed; run `stl focus`")
        out.write(render_aperture(s, _elapsed(v, s)) + "\n")
        return EXIT_OK
    if cmd in ("done", "drop", "release"):
        s = getattr(v, "complete" if cmd == "done" else cmd)()
        out.write(f"{cmd}: {s.action}\naperture closed. next slice needs a fresh reset.\n")
        return EXIT_OK
    if cmd == "status":
        s = v.active()
        out.write(f"aperture: {'OPEN' if s else 'closed'}\n")
        out.write(f"grounded: {'yes' if v.grounded() else 'no'}\n")
        out.write(f"backlog:  {v.pending_count() if args.count else 'sealed'}\n")
        if s and v.overrun():
            out.write("overrun:  yes\n")
        return EXIT_OK
    raise AssertionError(cmd)  # pragma: no cover


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
