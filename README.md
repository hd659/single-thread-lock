# single-thread-lock

A single-aperture execution lock, built as the opposite of a todo list. It is for people who think in networks and freeze up when a todo list shows them fifty future tasks at once.

Most todo apps keep the whole backlog on screen, which keeps it loaded in working memory too. This tool hides the backlog. You get one physical micro-slice of 10 minutes or less, and only after a 60-second ground-state reset.

## Model

```
 brain dump --> [ sealed vault (SQLite) ] --60s reset--> ( one slice ) --> done | drop | release
                  write-only, never listed                  <= 10 min
```

The invariants are enforced in code and in the SQLite schema:

1. **Write-only backlog.** No API or command lists pending slices. `status` reports the backlog as `sealed`. `--count` shows an integer and nothing more.
2. **Single aperture.** A partial unique index allows at most one `active` slice.
3. **Ground-state gate.** Every `next` uses up one completed reset of 60 seconds or longer. A reset interrupted with Ctrl-C is not recorded.
4. **Micro-slice budget.** A dump is split on newlines, `;`, sentence ends, `then`, `然后` and `接着`. Estimates such as `(25m)` or `(1h)` are cut into parts of 10 minutes or less. A `CHECK` constraint enforces the limit.

## Usage

```bash
python -m single_thread.cli dump "read paper (25m); email TA then stretch"
python -m single_thread.cli focus        # 60s reset, then exactly one slice
python -m single_thread.cli show         # active slice + progress bar / overrun
python -m single_thread.cli done         # or: drop | release
python -m single_thread.cli status       # aperture / grounded / backlog: sealed
```

`dump` with no text argument reads from stdin, so `pbpaste | python -m single_thread.cli dump` works. The vault lives at `~/.single_thread/vault.db`. Override it with `--db PATH` or `STL_DB`.

Exit codes: `0` ok, `2` locked (an invariant would be broken), `130` reset aborted.

## Layout

| File | Role |
| --- | --- |
| `single_thread/vault.py` | SQLite vault, decomposer, lock invariants |
| `single_thread/cli.py` | terminal aperture, reset timer, anti-peek rendering |
| `tests/` | PyTest suite with an injected clock (no real sleeps) |

## Test

```bash
python -m pytest -q
```
