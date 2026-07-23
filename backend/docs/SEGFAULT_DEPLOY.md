# Segfault diagnostics + fix — deploy notes

## What happened

The backend died with a bare **exit 139 (SIGSEGV)** at the market open, with
**no Python traceback** in the logs. Root cause (RCA): the trade-chart renderer
`app/services/trade_chart.py::generate_trade_chart` drives matplotlib's
**stateful, non-thread-safe pyplot API** (`plt.subplots` / `fig.savefig` /
`plt.close`). It is invoked from `send_signal_email` via `asyncio.to_thread`,
so when several account-signal watcher tasks fire at once (open fan-out),
multiple worker threads execute `plt.*` simultaneously. Concurrent mutation of
matplotlib's process-global figure-manager (`Gcf`) and its non-reentrant C/Agg
backend corrupts state and takes the whole process down — no Python-level
exception, hence no traceback.

## Code changes staged in this worktree (already applied, no deploy yet)

1. **`app/services/trade_chart.py`** — module-level `_RENDER_LOCK =
   threading.Lock()`; the entire render (`plt.subplots` -> `fig.savefig` ->
   `plt.close`, in a `finally`) now runs inside `with _RENDER_LOCK:`. Only one
   thread renders at a time; the Agg backend stays forced (`matplotlib.use("Agg")`
   before the pyplot import, unchanged). This is the only chart render path in
   the codebase, and the other caller (`engines/options/theta_scanner.py`) also
   goes through `generate_trade_chart`, so the single lock covers every
   concurrent matplotlib site.

2. **`app/main.py`** — `import faulthandler; faulthandler.enable(all_threads=True)`
   at the very top, before any heavy import. On the next fatal signal this dumps
   a C-level, **all-threads** Python stack to **stderr**, which Docker captures
   into the container log stream (`docker logs`) — no extra wiring needed.

3. **`tests/test_chart_threadsafe.py`** — renders 64 charts across
   `ThreadPoolExecutor(8)`; asserts every call returns valid PNG bytes and no
   worker raises. Email-free and deterministic.

## Environment changes to make AT DEPLOY TIME (NOT done here — do NOT edit prod
## `.env` or `docker-compose` while the market is open)

`PYTHONFAULTHANDLER=1` enables faulthandler from interpreter startup — even
earlier than the `main.py` line, so it also covers a crash during import of
`main.py` itself. Belt-and-suspenders alongside the in-code `enable()`.

### `backend/.env` — add this line

```
PYTHONFAULTHANDLER=1
```

### `docker-compose` backend service — add under `environment:`

```yaml
    environment:
      - PYTHONFAULTHANDLER=1
```

(Add to the existing `environment:` block for the backend service; keep the
other vars in place. `PYTHONFAULTHANDLER=1` is any-non-empty-value = on.)

After adding these, the change only takes effect on the next backend
container recreate — schedule that for a maintenance window, not mid-session.

## How to confirm the fix after deploy

- `docker logs <backend> 2>&1 | grep -i faulthandler` — nothing expected under
  normal operation; on a real fatal signal you'll see `Fatal Python error:` +
  a per-thread `Current thread 0x...` stack, which pinpoints the faulting frame.
- No recurrence of exit 139 at the open under a multi-subscriber fan-out.
