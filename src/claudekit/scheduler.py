"""Scheduler for adapters (scrapers) and agents.

Two kinds of task share one table:

* **adapter** — a host-declared argv (a scraper CLI). Runs on a fixed interval.
* **agent**   — a Claude Code agent. Runs daily at a chosen hour, and its JSON output is turned
  into a report.

Both can also be triggered on demand; every run becomes a `ck_jobs` row so the UI shows live
progress and keeps the output.

The loop runs as a daemon thread inside the host process. That is simpler than HomeFlix's separate
daemon, at the cost of a restart interrupting an in-flight run — jobs left behind are reported as
`interrupted` rather than hanging (see `jobs.STALE_SEC`). Hosts that need crawls to survive a
restart should run this module in its own process.
"""

from __future__ import annotations

import threading
import time

from . import agent_run, jobs, registry, reports
from .config import KitConfig
from .store import Store

DEFAULT_INTERVAL = 6 * 3600
TICK_SEC = 30


def _now() -> float:
    return time.time()


def _next_daily(hour: int, after: float | None = None) -> float:
    """Epoch seconds of the next occurrence of `hour` local time."""
    base = after or _now()
    lt = time.localtime(base)
    candidate = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, hour, 0, 0, 0, 0, -1))
    if candidate <= base:
        candidate += 86400
    return candidate


class Scheduler:
    def __init__(self, cfg: KitConfig, store: Store) -> None:
        self.cfg = cfg
        self.store = store
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._running: set[str] = set()
        self._lock = threading.Lock()

    # ---------------------------------------------------------------- task rows
    def sync_tasks(self) -> None:
        """Ensure a row exists for every declared adapter and agent. Never overwrites user state."""
        for name in self.cfg.adapters:
            self._ensure(name, "adapter", interval_sec=DEFAULT_INTERVAL)
        for name, spec in self.cfg.agents.items():
            if spec.schedule == "daily":
                self._ensure(name, "agent", hour=registry.get_hour(self.cfg, self.store, name))
        self._synced = True

    def _ensure_synced(self) -> None:
        """Materialise declared tasks on first access.

        Without this, reading or updating a task before the loop's first tick silently addresses a
        row that does not exist yet — an UPDATE affecting zero rows looks like success.
        """
        if not getattr(self, "_synced", False):
            self.sync_tasks()

    def _ensure(self, task: str, kind: str, interval_sec: int | None = None,
                hour: int | None = None) -> None:
        if self.store.one("SELECT 1 FROM ck_schedule_task WHERE task=?", (task,)):
            return
        self.store.execute(
            """INSERT INTO ck_schedule_task (task, kind, enabled, paused, interval_sec, hour,
                                             args, last_status, next_run)
               VALUES (?,?,0,0,?,?,?,'never',?)""",
            (task, kind, interval_sec, hour, "[]",
             _next_daily(hour) if hour is not None else None))

    def tasks(self) -> list[dict]:
        self._ensure_synced()
        out = []
        for r in self.store.query("SELECT * FROM ck_schedule_task ORDER BY kind, task"):
            d = dict(r)
            d["running"] = d["task"] in self._running
            if d["kind"] == "agent":
                spec = self.cfg.agents.get(d["task"])
                d["label"] = spec.label if spec else d["task"]
                d["description"] = spec.description if spec else ""
            else:
                d["label"] = d["task"]
                d["argv"] = self.cfg.adapters.get(d["task"], [])
            out.append(d)
        return out

    def update(self, task: str, **fields) -> dict | None:
        self._ensure_synced()
        if not self.store.one("SELECT 1 FROM ck_schedule_task WHERE task=?", (task,)):
            raise KeyError(f"unknown task: {task}")
        allowed = {"enabled", "paused", "interval_sec", "hour", "args"}
        sets, vals = [], []
        for k, v in fields.items():
            if k not in allowed or v is None:
                continue
            sets.append(f"{k}=?")
            vals.append(int(v) if k in ("enabled", "paused", "interval_sec", "hour") else v)
        if not sets:
            return self.get(task)
        vals.append(task)
        self.store.execute(f"UPDATE ck_schedule_task SET {', '.join(sets)} WHERE task=?", tuple(vals))
        if "hour" in fields and fields["hour"] is not None:
            self.store.execute("UPDATE ck_schedule_task SET next_run=? WHERE task=?",
                               (_next_daily(int(fields["hour"])), task))
        return self.get(task)

    def get(self, task: str) -> dict | None:
        self._ensure_synced()
        r = self.store.one("SELECT * FROM ck_schedule_task WHERE task=?", (task,))
        return dict(r) if r else None

    # ---------------------------------------------------------------- running
    def run_now(self, task: str) -> dict:
        """Start `task` immediately in the background. Refuses if it is already running."""
        row = self.get(task)
        if not row:
            raise KeyError(f"unknown task: {task}")
        with self._lock:
            if task in self._running:
                return {"ok": False, "task": task, "error": "already running"}
            self._running.add(task)

        kind = row["kind"]
        label = f"{kind}:{task}"

        def _work(job_id: int, _params: dict) -> None:
            try:
                if kind == "adapter":
                    self._run_adapter(task, job_id)
                else:
                    self._run_agent(task, job_id)
            finally:
                with self._lock:
                    self._running.discard(task)

        job_id = jobs.run_bg(self.store, kind, label, {"task": task}, _work)
        return {"ok": True, "task": task, "job": job_id}

    def _finish(self, task: str, status: str, started: float,
                result: str = "", error: str | None = None) -> None:
        row = self.get(task) or {}
        nxt = None
        if row.get("kind") == "agent" and row.get("hour") is not None:
            nxt = _next_daily(int(row["hour"]))
        elif row.get("interval_sec"):
            nxt = _now() + int(row["interval_sec"])
        self.store.execute(
            """UPDATE ck_schedule_task SET last_run=?, last_duration_sec=?, last_status=?,
                      last_result=?, last_error=?, next_run=? WHERE task=?""",
            (started, int(_now() - started), status, result[:4000], error, nxt, task))

    def _run_adapter(self, task: str, job_id: int) -> None:
        argv = list(self.cfg.adapters.get(task) or [])
        if not argv:
            jobs.update(self.store, job_id, status="error", error="no argv declared")
            self._finish(task, "error", _now(), error="no argv declared")
            return
        row = self.get(task) or {}
        extra = []
        try:
            import json as _json
            extra = _json.loads(row.get("args") or "[]")
        except Exception:  # noqa: BLE001
            extra = []
        started = _now()
        rc = jobs.run_subprocess(self.store, job_id, [*argv, *extra], cwd=str(self.cfg.root),
                                 env=self.cfg.env())
        self._finish(task, "ok" if rc == 0 else "error", started,
                     error=None if rc == 0 else f"exit {rc}")

    def _run_agent(self, task: str, job_id: int) -> None:
        started = _now()
        spec = registry.resolve(self.cfg, self.store, task)
        try:
            res = agent_run.run(self.cfg, spec)
        except agent_run.AgentError as e:
            jobs.update(self.store, job_id, status="error", error=str(e))
            self._finish(task, "error", started, error=str(e))
            return

        text = res["result"]
        payload = agent_run.extract_json(text)
        ok = res["returncode"] == 0
        rid = reports.save(self.store, task, payload, raw=text,
                           duration_sec=res["duration_sec"], ok=ok)
        jobs.update(self.store, job_id,
                    status="done" if ok else "error",
                    result={"report": rid, "transcript": res["transcript"],
                            "session": res["session"]},
                    error=None if ok else f"claude exited {res['returncode']}",
                    log=text[-20000:])
        self._finish(task, (payload or {}).get("status", "ok" if ok else "error"), started,
                     result=f"report {rid}")

    # ---------------------------------------------------------------- loop
    def due(self) -> list[str]:
        self._ensure_synced()
        now = _now()
        out = []
        for r in self.store.query(
                "SELECT * FROM ck_schedule_task WHERE enabled=1 AND paused=0"):
            if r["task"] in self._running:
                continue
            nxt = r["next_run"]
            if nxt is None:
                out.append(r["task"])
            elif float(nxt) <= now:
                out.append(r["task"])
        return out

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.sync_tasks()   # re-sync each tick: a host may add adapters at runtime
                for task in self.due():
                    self.run_now(task)
            except Exception:  # noqa: BLE001 — a scheduler must never die on one bad tick
                pass
            self._stop.wait(TICK_SEC)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self.sync_tasks()
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="claudekit-scheduler")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    @property
    def alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())
