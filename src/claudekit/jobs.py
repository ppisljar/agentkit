"""On-demand background jobs: started non-blocking, polled by the UI.

Used for anything the user kicks off from a page and then watches — a scraper run, an agent run,
a rebuild. A `running` job whose thread died (process restart, crash) is reported as
`interrupted` rather than hanging in `running` forever.
"""

from __future__ import annotations

import json
import subprocess
import threading
import time
from collections.abc import Callable

from .store import Store

STALE_SEC = 30 * 60
MAX_LOG = 64_000        # keep the tail of a job's output, not an unbounded blob


def create(store: Store, kind: str, label: str = "", params: dict | None = None) -> int:
    now = time.time()
    cur = store.execute(
        "INSERT INTO ck_jobs (kind, status, created, updated, label, params) VALUES (?,?,?,?,?,?)",
        (kind, "running", now, now, label, json.dumps(params or {})),
    )
    return int(cur.lastrowid)


def update(store: Store, job_id: int, status: str | None = None, result: dict | None = None,
           error: str | None = None, log: str | None = None) -> None:
    sets, vals = ["updated=?"], [time.time()]
    if status is not None:
        sets.append("status=?"); vals.append(status)
    if result is not None:
        sets.append("result=?"); vals.append(json.dumps(result))
    if error is not None:
        sets.append("error=?"); vals.append(error)
    if log is not None:
        sets.append("log=?"); vals.append(log[-MAX_LOG:])
    vals.append(job_id)
    store.execute(f"UPDATE ck_jobs SET {', '.join(sets)} WHERE id=?", tuple(vals))


def _row(r) -> dict:
    d = dict(r)
    for k in ("params", "result"):
        if d.get(k):
            try:
                d[k] = json.loads(d[k])
            except Exception:  # noqa: BLE001
                pass
    if d.get("status") == "running" and (time.time() - (d.get("updated") or 0)) > STALE_SEC:
        d["status"] = "interrupted"
    return d


def get(store: Store, job_id: int) -> dict | None:
    r = store.one("SELECT * FROM ck_jobs WHERE id=?", (job_id,))
    return _row(r) if r else None


def recent(store: Store, kind: str | None = None, limit: int = 20) -> list[dict]:
    if kind:
        rows = store.query("SELECT * FROM ck_jobs WHERE kind=? ORDER BY created DESC LIMIT ?",
                           (kind, limit))
    else:
        rows = store.query("SELECT * FROM ck_jobs ORDER BY created DESC LIMIT ?", (limit,))
    return [_row(r) for r in rows]


def running(store: Store, kind: str) -> dict | None:
    """The live job for `kind`, if any (used to stop the UI starting a second one)."""
    for j in recent(store, kind, 5):
        if j["status"] == "running":
            return j
    return None


def running_tasks(store: Store) -> set[str]:
    """Scheduler task names with a live job right now, read from the DB rather than from memory.

    A Scheduler only knows about the runs IT started (`_running`), which is wrong whenever the loop
    lives in a separate process from the web app — the standard split deployment. There the web
    process asks for a run by setting `run_requested`, the daemon picks it up, and the web process
    then has no idea anything is in flight, so every task looks idle. The job row is the one piece
    of run state both processes share, so ask it.

    Applies the same stale->interrupted rule as the rest of this module, so a job orphaned by a
    crashed daemon stops being reported as running instead of pinning a task forever.
    """
    out: set[str] = set()
    for r in store.query("SELECT * FROM ck_jobs WHERE status='running' "
                         "ORDER BY created DESC LIMIT 200"):
        j = _row(r)
        if j.get("status") != "running":
            continue
        task = (j.get("params") or {}).get("task")
        if task:
            out.add(str(task))
    return out


def run_bg(store: Store, kind: str, label: str, params: dict,
           fn: Callable[[int, dict], None]) -> int:
    """Create a job and run `fn(job_id, params)` in a daemon thread. Returns the id immediately."""
    job_id = create(store, kind, label, params)

    def _wrap():
        try:
            fn(job_id, params)
            j = get(store, job_id)
            if j and j["status"] == "running":      # fn didn't set a terminal status
                update(store, job_id, status="done")
        except Exception as e:  # noqa: BLE001
            update(store, job_id, status="error", error=str(e))

    threading.Thread(target=_wrap, daemon=True, name=f"ckjob-{kind}-{job_id}").start()
    return job_id


def run_subprocess(store: Store, job_id: int, argv: list[str], cwd: str,
                   env: dict | None = None, timeout: int = 3600,
                   on_finish=None) -> int:
    """Run `argv`, streaming its output into the job's log. Returns the exit code.

    Output is streamed rather than captured at the end so a long scrape shows progress while it
    runs; only the tail is persisted (see MAX_LOG).

    `on_finish(rc)` runs BEFORE the job is marked done. Anything a caller records about the run —
    the scheduler writing the task's last_status, say — has to be in place by the time the job
    reports finished, because "job done" is the signal every watcher waits on. Doing it afterwards
    leaves a window where the job says done and the task still shows its previous status.
    """
    buf: list[str] = []
    last_flush = 0.0
    try:
        p = subprocess.Popen(argv, cwd=cwd, env=env, text=True,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    except OSError as e:
        update(store, job_id, status="error", error=f"could not start: {e}")
        return 127

    deadline = time.time() + timeout
    try:
        for line in p.stdout or []:
            buf.append(line)
            if len(buf) > 4000:
                del buf[: len(buf) - 4000]
            now = time.time()
            if now - last_flush > 2:
                update(store, job_id, log="".join(buf))
                last_flush = now
            if now > deadline:
                p.kill()
                update(store, job_id, status="error", error="timed out", log="".join(buf))
                return 124
        p.wait(timeout=max(1, int(deadline - time.time())))
    except subprocess.TimeoutExpired:
        p.kill()
        update(store, job_id, status="error", error="timed out", log="".join(buf))
        return 124

    rc = p.returncode or 0
    if on_finish:
        on_finish(rc)
    update(store, job_id,
           status=("done" if rc == 0 else "error"),
           error=(None if rc == 0 else f"exited {rc}"),
           log="".join(buf))
    return rc
