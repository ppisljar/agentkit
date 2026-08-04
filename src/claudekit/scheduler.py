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

import logging
import threading
import time

from . import agent_run, jobs, registry, reports
from .config import KitConfig
from .store import Store

log = logging.getLogger("claudekit.scheduler")

DEFAULT_INTERVAL = 6 * 3600
# Short tick: it is two cheap SQLite reads, and it bounds how long a "Run now" from the web
# process waits before the daemon notices the request.
TICK_SEC = 2
HEARTBEAT_KEY = "scheduler_heartbeat"
# A heartbeat older than this means whoever was running the loop is gone.
HEARTBEAT_STALE_SEC = 15


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

    def run_all(self) -> dict:
        """Start every enabled, unpaused task now. Tasks already running are skipped, not queued
        twice. Returns what was started and what was skipped, so a caller can report honestly
        rather than implying it kicked off more than it did."""
        started, skipped = [], []
        for row in self.tasks():
            if not row.get("enabled") or row.get("paused"):
                continue
            res = self.run_now(row["task"])
            (started if res.get("ok") else skipped).append(row["task"])
        return {"ok": True, "started": started, "skipped": skipped}

    def global_interval(self) -> int | None:
        row = self.store.one("SELECT v FROM ck_schedule_meta WHERE k='interval_sec'")
        try:
            return int(row["v"]) if row else None
        except (TypeError, ValueError):
            return None

    def set_global_interval(self, sec: int) -> dict:
        """Set one crawl interval across every adapter.

        Deliberately a write-through rather than a fallback the way HomeFlix did it: a task whose
        interval is NULL never reschedules here, so 'inherits the global' would be a silent trap.
        Storing it too keeps the UI able to show what was last applied. Agents are untouched —
        they are scheduled by hour.
        """
        sec = max(60, int(sec))
        self._ensure_synced()
        self.store.execute("UPDATE ck_schedule_task SET interval_sec=? WHERE kind='adapter'", (sec,))
        self.store.execute("INSERT OR REPLACE INTO ck_schedule_meta (k, v) VALUES ('interval_sec', ?)",
                           (str(sec),))
        return {"ok": True, "interval_sec": sec}

    def _fire_pre_run(self, task: str):
        """Host hook run just before a task starts; its return is threaded to post_run.

        Kept advisory in the same way as post_run — a host that can't measure something shouldn't
        be able to stop the run that was about to happen.
        """
        hook = getattr(self.cfg, "pre_run", None)
        if not callable(hook):
            return None
        try:
            return hook(task)
        except Exception as e:  # noqa: BLE001
            log.warning("pre_run hook failed for %s: %s: %s", task, type(e).__name__, e)
            return None

    def count_items(self, task: str) -> int | None:
        """Host's current item count for `task`, or None if it doesn't count this one."""
        fn = getattr(self.cfg, "count_items", None)
        if not callable(fn):
            return None
        try:
            n = fn(task)
            return int(n) if n is not None else None
        except Exception as e:  # noqa: BLE001 — counting is advisory; never fail a run over it
            log.warning("count_items failed for %s: %s: %s", task, type(e).__name__, e)
            return None

    def _finish(self, task: str, status: str, started: float,
                result: str = "", error: str | None = None,
                new_count: int | None = None, pre=None) -> None:
        row = self.get(task) or {}
        nxt = None
        if row.get("kind") == "agent" and row.get("hour") is not None:
            nxt = _next_daily(int(row["hour"]))
        elif row.get("interval_sec"):
            nxt = _now() + int(row["interval_sec"])
        self.store.execute(
            """UPDATE ck_schedule_task SET last_run=?, last_duration_sec=?, last_status=?,
                      last_result=?, last_error=?, next_run=?, last_new_count=? WHERE task=?""",
            (started, int(_now() - started), status, result[:4000], error, nxt, new_count, task))
        self._fire_post_run(task, status, result, error, started, new_count, pre)

    def _fire_post_run(self, task: str, status: str, result: str, error: str | None,
                       started: float, new_count: int | None = None, pre=None) -> None:
        """Notify the host that a task finished, so it can trigger follow-up work (rebuild a static
        site after a scrape that found new items, reindex a catalog, …).

        Called from _finish, so it fires exactly once for every task type — adapter, agent and
        agent-script alike. A hook that raises is logged and swallowed: follow-up work failing must
        not mark the task itself as failed, or retroactively undo a scrape that genuinely worked.
        """
        hook = getattr(self.cfg, "post_run", None)
        if not callable(hook):
            return
        try:
            hook(task, status, {"result": result, "error": error, "started": started,
                                "duration_sec": int(_now() - started),
                                "new_count": new_count, "pre": pre})
        except Exception as e:  # noqa: BLE001 — a broken hook must not break the scheduler
            log.warning("post_run hook failed for %s: %s: %s", task, type(e).__name__, e)

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
        # Count around the run so the host can tell whether it actually produced anything. The kit
        # has no idea what an adapter scraped, so the host supplies the count.
        pre = self._fire_pre_run(task)
        before = self.count_items(task)
        rc = jobs.run_subprocess(self.store, job_id, [*argv, *extra], cwd=str(self.cfg.root),
                                 env=self.cfg.env())
        after = self.count_items(task)
        new_count = max(0, after - before) if (before is not None and after is not None) else None
        self._finish(task, "ok" if rc == 0 else "error", started,
                     error=None if rc == 0 else f"exit {rc}", new_count=new_count, pre=pre)

    def _run_agent_script(self, task: str, spec, job_id: int) -> None:
        """Run a host script that owns the whole agent run.

        Used when the run needs domain work around the model call — assembling prompt context from
        live data first, or deriving a factual report from the database afterwards (rather than
        trusting the model to describe what it did). The script calls agent_run.run() itself and
        writes its own report, so the kit only supervises the process.
        """
        argv = [self.cfg.python_bin, *spec.script] if isinstance(spec.script, (list, tuple)) \
            else [self.cfg.python_bin, spec.script]
        started = _now()
        pre = self._fire_pre_run(task)
        rc = jobs.run_subprocess(self.store, job_id, argv, cwd=str(self.cfg.root),
                                 env=self.cfg.env())
        self._finish(task, "ok" if rc == 0 else "error", started,
                     error=None if rc == 0 else f"exit {rc}", pre=pre)

    def _run_agent(self, task: str, job_id: int) -> None:
        started = _now()
        spec = registry.resolve(self.cfg, self.store, task)
        if spec.script:
            return self._run_agent_script(task, spec, job_id)
        pre = self._fire_pre_run(task)
        try:
            res = agent_run.run(self.cfg, spec)
        except agent_run.AgentError as e:
            jobs.update(self.store, job_id, status="error", error=str(e))
            self._finish(task, "error", started, error=str(e), pre=pre)
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
                     result=f"report {rid}", pre=pre)

    # ---------------------------------------------------------------- loop
    def request_run(self, task: str) -> dict:
        """Ask whoever owns the loop to start `task`.

        Used by the web process when the scheduler lives in a separate daemon: it cannot start the
        work itself (the child would die with the request), so it leaves a marker the daemon picks
        up on its next tick.
        """
        if not self.get(task):
            raise KeyError(f"unknown task: {task}")
        self.store.execute("UPDATE ck_schedule_task SET run_requested=? WHERE task=?",
                           (_now(), task))
        return {"ok": True, "task": task, "queued": True}

    def due(self) -> list[str]:
        """Tasks to start now: explicitly requested ones first, then those past their next_run."""
        self._ensure_synced()
        now = _now()
        out = []

        # Explicit requests run even when the task is disabled or paused — the human asked.
        for r in self.store.query(
                "SELECT task FROM ck_schedule_task WHERE run_requested IS NOT NULL"):
            self.store.execute("UPDATE ck_schedule_task SET run_requested=NULL WHERE task=?",
                               (r["task"],))
            if r["task"] not in self._running:
                out.append(r["task"])

        for r in self.store.query(
                "SELECT * FROM ck_schedule_task WHERE enabled=1 AND paused=0"):
            if r["task"] in self._running or r["task"] in out:
                continue
            nxt = r["next_run"]
            if nxt is None or float(nxt) <= now:
                out.append(r["task"])
        return out

    def _beat(self) -> None:
        self.store.set_meta(HEARTBEAT_KEY, str(_now()))

    def _loop(self) -> None:
        n = 0
        while not self._stop.is_set():
            try:
                self._beat()
                # Re-syncing every tick would be wasteful at a 2s cadence; a host adding adapters
                # at runtime is rare, so check roughly every 30s.
                if n % 15 == 0:
                    self.sync_tasks()
                n += 1
                for task in self.due():
                    self.run_now(task)
            except Exception:  # noqa: BLE001 — a scheduler must never die on one bad tick
                pass
            self._stop.wait(TICK_SEC)

    def run_forever(self) -> None:
        """Run the loop in the calling thread — the entry point for the standalone daemon."""
        self.sync_tasks()
        self._stop.clear()
        self._loop()

    def start(self) -> None:
        """Run the loop in a background thread inside the host process."""
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
        """True if a loop is running — in this process or another one.

        Falls back to the heartbeat so a web process can report the daemon's health without
        sharing memory with it.
        """
        if self._thread and self._thread.is_alive():
            return True
        beat = self.store.get_meta(HEARTBEAT_KEY)
        try:
            return beat is not None and (_now() - float(beat)) < HEARTBEAT_STALE_SEC
        except (TypeError, ValueError):
            return False

    @property
    def in_process(self) -> bool:
        return bool(self._thread and self._thread.is_alive())
