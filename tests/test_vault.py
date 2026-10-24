import sqlite3

import pytest

from single_thread.vault import (
    RESET_SECONDS, SLICE_MINUTES, LockError, Vault, decompose,
)


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def tick(self, s):
        self.t += s


@pytest.fixture
def clk():
    return Clock()


@pytest.fixture
def v(clk):
    vault = Vault(clock=clk)
    yield vault
    vault.close()


def ground(v, clk, seconds=RESET_SECONDS):
    s = v.begin_reset()
    clk.tick(seconds)
    return v.end_reset(s)


# ---- decomposer ----------------------------------------------------------
def test_decompose_splits_on_separators():
    out = decompose("open laptop; write intro then email TA\nstretch")
    assert [a for a, _ in out] == ["open laptop", "write intro", "email TA", "stretch"]
    assert all(m == SLICE_MINUTES for _, m in out)


def test_decompose_honours_estimates_and_chops_long_tasks():
    out = decompose("read paper (25m). fix bug (3 min)")
    assert out == [
        ("read paper [part 1/3]", 10),
        ("read paper [part 2/3]", 10),
        ("read paper [part 3/3]", 5),
        ("fix bug", 3),
    ]


def test_decompose_hours_and_cjk():
    out = decompose("整理笔记 (1h)然后洗碗")
    assert len(out) == 7 and out[-1] == ("洗碗", 10)
    assert all(m <= SLICE_MINUTES for _, m in out)


def test_decompose_ignores_blank_noise():
    assert decompose("  ;\n\n - . ") == []
    with pytest.raises(ValueError):
        decompose("x", budget=0)


# ---- vault invariants ----------------------------------------------------
def test_backlog_is_not_listable(v):
    assert v.dump("a; b; c") == 3
    assert v.pending_count() == 3
    public = {n for n in dir(v) if not n.startswith("_")}
    assert not public & {"list", "all", "backlog", "items", "slices"}


def test_acquire_requires_ground_state(v, clk):
    v.dump("a; b")
    with pytest.raises(LockError):
        v.acquire()
    assert ground(v, clk, RESET_SECONDS - 1) is False
    with pytest.raises(LockError):
        v.acquire()
    assert ground(v, clk) is True
    assert v.acquire().action == "a"


def test_single_aperture_and_fifo(v, clk):
    v.dump("first; second")
    v.dump("third")
    ground(v, clk)
    s = v.acquire()
    ground(v, clk)
    with pytest.raises(LockError):
        v.acquire()
    assert v.complete().state == "done"
    assert v.acquire().action == "second"
    v.drop()
    ground(v, clk)
    assert v.acquire().action == "third"
    v.complete()
    ground(v, clk)
    assert v.acquire() is None
    assert v.pending_count() == 0 and s.seq == 0


def test_reset_is_consumed_per_acquire(v, clk):
    v.dump("a; b")
    ground(v, clk)
    v.acquire()
    v.complete()
    with pytest.raises(LockError):
        v.acquire()


def test_release_returns_slice_to_front(v, clk):
    v.dump("a; b")
    ground(v, clk)
    v.acquire()
    assert v.release().action == "a"
    assert v.active() is None and v.pending_count() == 2
    ground(v, clk)
    assert v.acquire().action == "a"


def test_overrun_detection(v, clk):
    v.dump("quick (2m)")
    assert v.overrun() is False
    ground(v, clk)
    v.acquire()
    clk.tick(120)
    assert v.overrun() is False
    clk.tick(1)
    assert v.overrun() is True


def test_close_without_active_raises(v):
    for op in (v.complete, v.drop, v.release):
        with pytest.raises(LockError):
            op()


def test_db_enforces_invariants(v):
    v.dump("a; b")
    with pytest.raises(sqlite3.IntegrityError):
        v._db.execute("UPDATE slices SET state='active'")
    with pytest.raises(sqlite3.IntegrityError):
        v._db.execute("UPDATE slices SET minutes=11 WHERE seq=0")


def test_persistence_across_sessions(tmp_path, clk):
    db = str(tmp_path / "vault.db")
    a = Vault(db, clock=clk)
    a.dump("persisted thought")
    ground(a, clk)
    a.close()
    b = Vault(db, clock=clk)
    assert b.pending_count() == 1 and b.grounded()
    assert b.acquire().action == "persisted thought"
    b.close()


def test_empty_dump_creates_nothing(v):
    assert v.dump("") == 0
    assert v.pending_count() == 0
