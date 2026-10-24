import io

import pytest

from single_thread.cli import EXIT_ABORT, EXIT_LOCKED, EXIT_OK, main, render_aperture, run_reset
from single_thread.vault import RESET_SECONDS, Slice, Vault


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


@pytest.fixture
def clk():
    return Clock()


@pytest.fixture
def v(clk):
    vault = Vault(clock=clk)
    yield vault
    vault.close()


@pytest.fixture
def stl(v, clk):
    def run(*argv, stdin=""):
        out = io.StringIO()
        code = main(list(argv), vault=v, out=out, stdin=io.StringIO(stdin), sleep=clk.sleep)
        return code, out.getvalue()
    return run


def test_dump_prints_receipt_not_items(stl):
    code, out = stl("dump", "secret alpha; secret beta; secret gamma")
    assert code == EXIT_OK and "3 slice" in out
    assert "secret" not in out


def test_dump_from_stdin(stl, v):
    code, out = stl("dump", stdin="a\nb")
    assert code == EXIT_OK and v.pending_count() == 2


def test_next_without_reset_is_locked(stl):
    stl("dump", "a")
    code, out = stl("next")
    assert code == EXIT_LOCKED and "reset" in out


def test_reset_counts_down_full_window(stl, clk, v):
    t0 = clk.t
    code, out = stl("reset")
    assert code == EXIT_OK and clk.t - t0 == RESET_SECONDS
    assert v.grounded() and "grounded" in out


def test_reset_interrupt_records_nothing(v, clk):
    calls = []

    def sleep(s):
        calls.append(s)
        if len(calls) == 5:
            raise KeyboardInterrupt
        clk.sleep(s)

    out = io.StringIO()
    assert run_reset(v, out, sleep) is False
    assert not v.grounded() and "aborted" in out.getvalue()


def test_focus_exposes_exactly_one_slice(stl):
    stl("dump", "write intro; email TA; wash dishes")
    code, out = stl("focus")
    assert code == EXIT_OK
    assert "write intro" in out
    assert "email TA" not in out and "wash dishes" not in out


def test_anti_peek_status_and_show(stl, clk):
    stl("dump", "first; hidden second")
    stl("focus")
    for argv in (("status",), ("show",), ("status", "--count")):
        _, out = stl(*argv)
        assert "hidden second" not in out
    _, out = stl("status")
    assert "sealed" in out and "OPEN" in out
    _, out = stl("status", "--count")
    assert "backlog:  1" in out


def test_second_aperture_locked_even_after_reset(stl):
    stl("dump", "a; b")
    stl("focus")
    code, out = stl("reset")
    assert code == EXIT_LOCKED
    code, out = stl("next")
    assert code == EXIT_LOCKED and "b" not in out.split("locked:")[0]


def test_done_closes_and_requires_fresh_reset(stl):
    stl("dump", "a; b")
    stl("focus")
    code, out = stl("done")
    assert code == EXIT_OK and "done: a" in out
    assert stl("next")[0] == EXIT_LOCKED
    code, out = stl("focus")
    assert "| " in out and " b " in out


def test_release_and_drop(stl, v):
    stl("dump", "a; b")
    stl("focus")
    assert "release: a" in stl("release")[1]
    assert v.pending_count() == 2
    stl("focus")
    assert "drop: a" in stl("drop")[1]
    assert v.pending_count() == 1


def test_close_ops_without_aperture_are_locked(stl):
    for cmd in ("done", "drop", "release", "show"):
        assert stl(cmd)[0] == EXIT_LOCKED


def test_empty_vault_focus(stl):
    code, out = stl("focus")
    assert code == EXIT_OK and "empty" in out


def test_show_tracks_elapsed_and_overrun(stl, clk):
    stl("dump", "quick (2m)")
    stl("focus")
    clk.sleep(60)
    assert "01:00" in stl("show")[1]
    clk.sleep(61)
    _, out = stl("show")
    assert "+00:01" in out and "over budget" in out
    assert "overrun:  yes" in stl("status")[1]


def test_render_bar_bounds():
    s = Slice(1, 1, 0, "x", 10, "active")
    assert "[" + "." * 30 + "] 10:00" in render_aperture(s, 0)
    assert "[" + "#" * 30 + "]" in render_aperture(s, 10_000)


def test_file_db_persists_via_env(tmp_path, monkeypatch, clk):
    db = tmp_path / "nested" / "v.db"
    monkeypatch.setenv("STL_DB", str(db))
    assert main(["dump", "persist me"], out=io.StringIO()) == EXIT_OK
    out = io.StringIO()
    main(["status", "--count"], out=out)
    assert db.exists() and "backlog:  1" in out.getvalue()
