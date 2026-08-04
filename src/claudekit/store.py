"""SQLite store for the generic platform tables.

These are the tables HomeFlix's app.db kept alongside its domain tables; here they live in their
own database so a host app's schema and the kit's stay independent (a host can still point
`KitConfig.db_path` at its existing db — the CREATE TABLE IF NOT EXISTS statements coexist).

WAL mode, because the web process and the scheduler daemon read/write concurrently.
"""

from __future__ import annotations

import sqlite3
import threading

from .config import KitConfig

SCHEMA = """
-- agent runs: findings/questions/proposals surfaced to the human
CREATE TABLE IF NOT EXISTS ck_reports (
  id INTEGER PRIMARY KEY, created REAL, agent TEXT, status TEXT,
  summary TEXT, detail TEXT, findings TEXT, duration_sec INTEGER, ok INTEGER
);
CREATE INDEX IF NOT EXISTS ix_ck_reports_created ON ck_reports(created DESC);

-- one row per question/proposal a report raised; the human answers or approves here
CREATE TABLE IF NOT EXISTS ck_report_items (
  id INTEGER PRIMARY KEY, report_id INTEGER, created REAL,
  kind TEXT,                        -- 'question' | 'proposal'
  text TEXT,
  status TEXT DEFAULT 'open',       -- open | answered | approved | rejected | done
  answer TEXT, answered_at REAL,
  FOREIGN KEY (report_id) REFERENCES ck_reports(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS ix_ck_items_status ON ck_report_items(status);
CREATE INDEX IF NOT EXISTS ix_ck_items_report ON ck_report_items(report_id);

-- Follow-up conversation on a report: the human asks the agent something about what it reported,
-- and the reply lands back here. A report is the START of a line of enquiry, not the end of it —
-- previously the only way to ask "why did that happen?" was to go and run the agent by hand,
-- losing the connection to the report that prompted the question.
CREATE TABLE IF NOT EXISTS ck_report_messages (
  id INTEGER PRIMARY KEY, report_id INTEGER, created REAL,
  role TEXT,                        -- 'user' | 'agent'
  text TEXT,
  agent TEXT,                       -- which agent answered (may differ from the report's author)
  session TEXT,                     -- the run's session id, so the NEXT follow-up can resume it
  job_id INTEGER,                   -- ck_jobs row while it runs, so the UI can poll and show a log
  status TEXT DEFAULT 'done',       -- running | done | error
  findings TEXT,                    -- the reply's json block, parsed: same shape as ck_reports
  FOREIGN KEY (report_id) REFERENCES ck_reports(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS ix_ck_msgs_report ON ck_report_messages(report_id, id);

-- per-agent prompt overrides (defaults live in code; the UI edits these)
CREATE TABLE IF NOT EXISTS ck_agent_config (
  agent TEXT PRIMARY KEY, system TEXT, prompt TEXT, hour INTEGER, updated REAL
);

-- on-demand background jobs, started non-blocking and polled by the UI
CREATE TABLE IF NOT EXISTS ck_jobs (
  id INTEGER PRIMARY KEY, kind TEXT, status TEXT, created REAL, updated REAL,
  label TEXT, params TEXT, result TEXT, error TEXT, log TEXT
);
CREATE INDEX IF NOT EXISTS ix_ck_jobs_kind ON ck_jobs(kind, created DESC);

-- scheduler: global settings + one row per runnable adapter (scraper) or scheduled agent
CREATE TABLE IF NOT EXISTS ck_schedule_meta (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS ck_schedule_task (
  task TEXT PRIMARY KEY,
  kind TEXT,                        -- 'adapter' | 'agent'
  enabled INTEGER DEFAULT 0, paused INTEGER DEFAULT 0,
  interval_sec INTEGER, hour INTEGER,
  args TEXT,
  last_run REAL, last_duration_sec INTEGER, last_status TEXT,
  last_result TEXT, last_error TEXT, next_run REAL,
  -- items the run added (host-supplied via KitConfig.count_items); drives "+N new" and
  -- lets a post_run hook skip expensive follow-up work when a run changed nothing
  last_new_count INTEGER,
  -- set by the web process to ask the scheduler (possibly another process) to run this now
  run_requested REAL
);
"""

# Columns added after the first release. CREATE TABLE IF NOT EXISTS cannot add a column to an
# existing table, so they are ALTERed in on connect.
_ADDED_COLUMNS = {
    "ck_schedule_task": {"run_requested": "REAL", "last_new_count": "INTEGER"},
    # The run that produced this report. Needed to offer "continue that conversation" on a
    # follow-up: the session id used to survive only in ck_jobs.result for kit-run agents, and not
    # at all for wrapper-script agents, so there was nothing to resume from.
    "ck_reports": {"session": "TEXT"},
    "ck_report_messages": {"findings": "TEXT"},
}


class Store:
    """Thread-local connections over one SQLite file."""

    def __init__(self, cfg: KitConfig) -> None:
        self.cfg = cfg
        self._local = threading.local()
        self._init_lock = threading.Lock()
        self._ready = False

    def connect(self) -> sqlite3.Connection:
        con = getattr(self._local, "con", None)
        if con is None:
            self.cfg.db_path.parent.mkdir(parents=True, exist_ok=True)
            con = sqlite3.connect(str(self.cfg.db_path), check_same_thread=False, timeout=30)
            con.execute("PRAGMA journal_mode=WAL")
            con.execute("PRAGMA synchronous=NORMAL")
            con.execute("PRAGMA foreign_keys=ON")
            con.row_factory = sqlite3.Row
            self._local.con = con
        if not self._ready:
            with self._init_lock:
                if not self._ready:
                    con.executescript(SCHEMA)
                    self._add_missing_columns(con)
                    con.commit()
                    self._ready = True
        return con

    @staticmethod
    def _add_missing_columns(con: sqlite3.Connection) -> None:
        for table, cols in _ADDED_COLUMNS.items():
            have = {r["name"] for r in con.execute(f"PRAGMA table_info({table})")}
            for name, decl in cols.items():
                if name not in have:
                    con.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")

    # -- cross-process heartbeat ---------------------------------------------
    # `alive` cannot be a thread handle once the scheduler may live in another process, so the
    # running scheduler stamps a timestamp and readers judge freshness.
    def set_meta(self, key: str, value: str) -> None:
        self.execute("INSERT OR REPLACE INTO ck_schedule_meta (k, v) VALUES (?,?)", (key, value))

    def get_meta(self, key: str) -> str | None:
        r = self.one("SELECT v FROM ck_schedule_meta WHERE k=?", (key,))
        return r["v"] if r else None

    # -- small helpers so callers don't repeat cursor boilerplate -------------
    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        return self.connect().execute(sql, params).fetchall()

    def one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        return self.connect().execute(sql, params).fetchone()

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        con = self.connect()
        with con:
            return con.execute(sql, params)

    def close(self) -> None:
        con = getattr(self._local, "con", None)
        if con is not None:
            con.close()
            self._local.con = None
