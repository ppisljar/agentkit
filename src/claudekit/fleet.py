"""Fleet — one report/approval loop across MANY claudeKit projects.

Every project on a box runs its own agents and files its own reports. Checking them means opening
each project's web UI in turn, which stops scaling at about the third project. This module merges
them: one list of reports, one list of things waiting on you, with the project each belongs to
carried on every row.

**It reads the other projects' SQLite databases directly** rather than proxying their HTTP APIs.
Three reasons:

* not every project necessarily exposes ``/api/kit`` over HTTP,
* reports are exactly what you want to read when a project's web app is *down*, and
* writes are safe anyway: claudeKit's cross-process contract is already "the web process only
  writes to the database; the scheduler daemon polls it". Answering or approving is an UPDATE on
  ``ck_report_items``; asking a project to carry out its approved items is setting
  ``run_requested`` on its ``ck_schedule_task`` row (see ``Scheduler.request_run``). Both are
  writes the project's *own* web process performs. Nothing here runs an agent.

Connections are short-lived and read-only wherever possible (``file:...?mode=ro``), because these
databases are live and being written by other processes. Never hold a transaction open across a
request.

Rows are addressed by a **fleet id**: ``"<project key>:<local id>"`` (e.g. ``homeflix:23``). It
survives a JSON round-trip, reads well in a UI ("report #homeflix:23") and, unlike an offset-encoded
integer, cannot silently collide when the project list is reordered.

Wiring a host up::

    from claudekit.fleet import Fleet
    fleet = Fleet.from_json("projects.json")
    app.include_router(fleet_router(fleet))     # see claudekit.http.fleet_router

Nothing else in the kit imports this module, and this module imports nothing that touches a host's
KitConfig — a fleet host has no agents, no scheduler and no config of its own.
"""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from . import reports as _reports

#: Separates the project key from the project-local row id in a fleet id.
SEP = ":"


class UnknownProject(KeyError):
    """No project with that key is configured."""


class ProjectUnavailable(RuntimeError):
    """The project's database could not be opened or read.

    Raised only for operations that cannot degrade gracefully (reading one report, writing a
    decision). Listing deliberately does NOT raise: one broken project must not blank the page,
    so it becomes a visibly-degraded row instead — see `Fleet.status`.
    """


@dataclass(frozen=True)
class Project:
    """One claudeKit-using project the dashboard aggregates.

    `db` is the project's kit database. Where it lives is up to the host — a project may keep the
    kit tables in its own application database (HomeFlix: ``data/app.db``) or in a dedicated one
    (``.kit/claudekit.db``) — which is precisely why the project list is data rather than a
    convention.
    """

    key: str
    label: str
    db: Path
    #: Where the project's own UI lives, so a row can link back to it. Optional.
    url: str = ""
    #: Accent colour for the project's badge. Optional; the UI falls back to a neutral tone.
    color: str = ""
    #: Task to ask the project to run when one of its items is approved/answered. This is the
    #: project's own generic apply agent unless it declares otherwise.
    apply_task: str = "applydecisions"
    #: {raising agent -> task that carries out ITS items}, mirroring `AgentSpec.decisions_task`.
    #: The fleet cannot import another project's KitConfig to discover these, so a project whose
    #: agents handle their own decisions repeats the mapping here.
    decisions_tasks: dict[str, str] = field(default_factory=dict)
    #: Set false to keep a project in the file but out of the dashboard.
    enabled: bool = True

    @classmethod
    def from_dict(cls, d: dict) -> "Project":
        key = str(d.get("key") or "").strip()
        if not key:
            raise ValueError("project entry has no 'key'")
        if SEP in key:
            raise ValueError(f"project key must not contain {SEP!r}: {key!r}")
        db = str(d.get("db") or "").strip()
        if not db:
            raise ValueError(f"project {key!r} has no 'db'")
        path = Path(db).expanduser()
        if not path.is_absolute():
            # A relative path would resolve against the dashboard's cwd, which is neither the
            # project's root nor stable under systemd. Better to refuse than to read the wrong file.
            raise ValueError(f"project {key!r}: 'db' must be an absolute path, got {db!r}")
        return cls(
            key=key,
            label=str(d.get("label") or key),
            db=path,
            url=str(d.get("url") or ""),
            color=str(d.get("color") or ""),
            apply_task=str(d.get("apply_task") or "applydecisions"),
            decisions_tasks={str(k): str(v) for k, v in (d.get("decisions_tasks") or {}).items()},
            enabled=bool(d.get("enabled", True)),
        )

    def tag(self) -> dict:
        """The fields every merged row carries so the UI can label and colour it."""
        return {"project": self.key, "project_label": self.label,
                "project_url": self.url, "project_color": self.color}


# --------------------------------------------------------------------------- store adapters
#
# `claudekit.reports` speaks to a `Store` — an object with query/one/execute. Giving it one backed
# by a foreign database is what lets the fleet reuse the report reading and decision logic verbatim
# instead of re-implementing (and drifting from) the same SQL.


class _RoStore:
    """`Store`-shaped, read-only, over one already-open connection."""

    def __init__(self, con: sqlite3.Connection) -> None:
        self._con = con

    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        return self._con.execute(sql, params).fetchall()

    def one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        return self._con.execute(sql, params).fetchone()

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:  # pragma: no cover - guard
        raise PermissionError("this fleet connection is read-only")


class _RwStore(_RoStore):
    """Writable variant. Each `execute` is its own committed transaction, deliberately: another
    process owns this database and a long-lived transaction would block its scheduler."""

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._con:
            return self._con.execute(sql, params)


@contextmanager
def _open(project: Project, *, write: bool = False) -> Iterator[_RoStore]:
    """Short-lived connection to one project's kit database.

    Read-only unless `write`, so a listing pass cannot modify a live project's data even by
    accident. `mode=ro` also refuses to CREATE the file, which turns a wrong path in projects.json
    into an error instead of an empty database that silently reports "no reports".
    """
    uri = f"file:{project.db}?mode=ro" if not write else f"file:{project.db}?mode=rw"
    try:
        con = sqlite3.connect(uri, uri=True, timeout=15)
    except sqlite3.Error as e:
        raise ProjectUnavailable(f"{project.key}: cannot open {project.db}: {e}") from e
    con.row_factory = sqlite3.Row
    try:
        yield (_RwStore(con) if write else _RoStore(con))
    except sqlite3.Error as e:
        raise ProjectUnavailable(f"{project.key}: {e}") from e
    finally:
        con.close()


# --------------------------------------------------------------------------- fleet ids


def split_id(fid: str) -> tuple[str, int]:
    """``"homeflix:23"`` -> ``("homeflix", 23)``. Raises ValueError on anything else."""
    text = str(fid)
    key, _, local = text.rpartition(SEP)
    if not key or not local.isdigit():
        raise ValueError(f"not a fleet id: {fid!r} (expected '<project>{SEP}<id>')")
    return key, int(local)


def make_id(key: str, local_id) -> str:
    return f"{key}{SEP}{int(local_id)}"


def _tag_item(project: Project, item: dict) -> dict:
    out = dict(item)
    out["local_id"] = out["id"]
    out["id"] = make_id(project.key, out["id"])
    if out.get("report_id") is not None:
        out["report_id"] = make_id(project.key, out["report_id"])
    out.update(project.tag())
    return out


def _tag_report(project: Project, rep: dict) -> dict:
    out = dict(rep)
    out["local_id"] = out["id"]
    out["id"] = make_id(project.key, out["id"])
    out.update(project.tag())
    if out.get("items"):
        out["items"] = [_tag_item(project, it) for it in out["items"]]
    return out


# --------------------------------------------------------------------------- the fleet


class Fleet:
    """The configured projects, and every operation the dashboard needs over them."""

    def __init__(self, projects: list[Project]) -> None:
        seen: dict[str, Project] = {}
        for p in projects:
            if p.key in seen:
                raise ValueError(f"duplicate project key: {p.key!r}")
            seen[p.key] = p
        self._projects = seen
        self.source: Path | None = None

    # ---------------------------------------------------------------- construction
    @classmethod
    def from_list(cls, entries: list[dict]) -> "Fleet":
        return cls([Project.from_dict(e) for e in entries])

    @classmethod
    def from_json(cls, path) -> "Fleet":
        """Load the project list from a JSON file: a bare list, or ``{"projects": [...]}``."""
        p = Path(path)
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data = data.get("projects") or []
        if not isinstance(data, list):
            raise ValueError(f"{p}: expected a list of projects")
        fleet = cls.from_list(data)
        fleet.source = p
        return fleet

    def reload(self) -> "Fleet":
        """Re-read `source`. Lets a host pick up a new project without a restart."""
        if self.source is None:
            raise ValueError("this fleet was not loaded from a file")
        fresh = Fleet.from_json(self.source)
        self._projects = fresh._projects
        return self

    # ---------------------------------------------------------------- lookup
    @property
    def all(self) -> list[Project]:
        return [p for p in self._projects.values() if p.enabled]

    def get(self, key: str) -> Project:
        p = self._projects.get(str(key))
        if p is None or not p.enabled:
            raise UnknownProject(f"unknown project: {key}")
        return p

    def _selected(self, project: str | None) -> list[Project]:
        return [self.get(project)] if project else self.all

    # ---------------------------------------------------------------- status
    def status(self) -> list[dict]:
        """One row per project, including the ones that are broken.

        A project whose database is missing, unreadable or not a kit database gets ``ok: false``
        and an ``error`` rather than raising — the whole point of the dashboard is to still work
        when something is wrong.
        """
        out = []
        for p in self.all:
            row = {**p.tag(), "key": p.key, "label": p.label, "db": str(p.db),
                   "url": p.url, "color": p.color, "ok": True, "error": None,
                   "reports": 0, "open_items": 0, "pending_action": 0, "last_report": None}
            try:
                with _open(p) as store:
                    row["reports"] = int(store.one("SELECT COUNT(*) c FROM ck_reports")["c"])
                    row["open_items"] = int(store.one(
                        "SELECT COUNT(*) c FROM ck_report_items WHERE status='open'")["c"])
                    row["pending_action"] = len(_reports.actionable(store))
                    last = store.one("SELECT MAX(created) m FROM ck_reports")
                    row["last_report"] = last["m"] if last else None
            except (ProjectUnavailable, sqlite3.Error, OSError, TypeError) as e:
                row["ok"] = False
                row["error"] = str(e)
            out.append(row)
        return out

    # ---------------------------------------------------------------- reading
    def reports(self, limit: int = 30, project: str | None = None,
                agent: str | None = None) -> list[dict]:
        """Merged reports, newest first.

        `limit` is applied per project and again to the merged result, so a chatty project cannot
        crowd a quiet one out of the newest N.
        """
        merged: list[dict] = []
        for p in self._selected(project):
            try:
                with _open(p) as store:
                    rows = _reports.recent(store, limit=limit, agent=agent)
            except (ProjectUnavailable, sqlite3.Error, OSError):
                continue        # degraded; `status()` is where the failure is reported
            merged += [_tag_report(p, r) for r in rows]
        merged.sort(key=lambda r: r.get("created") or 0, reverse=True)
        return merged[:limit]

    def open_items(self, project: str | None = None) -> list[dict]:
        """Everything still waiting on the human, across the fleet, newest report first."""
        merged: list[dict] = []
        for p in self._selected(project):
            try:
                with _open(p) as store:
                    rows = _reports.open_items(store)
            except (ProjectUnavailable, sqlite3.Error, OSError):
                continue
            merged += [_tag_item(p, it) for it in rows]
        merged.sort(key=lambda i: i.get("report_created") or 0, reverse=True)
        return merged

    def report(self, fid: str) -> dict | None:
        """One report with its findings and items, by fleet id."""
        key, local = split_id(fid)
        p = self.get(key)
        with _open(p) as store:
            rep = _reports.get(store, local)
        return _tag_report(p, rep) if rep else None

    # ---------------------------------------------------------------- writing
    def answer(self, fid: str, text: str) -> dict | None:
        """Answer a question (or note a proposal) in the project that owns it."""
        return self._write(fid, lambda store, local: _reports.answer(store, local, text))

    def decide(self, fid: str, approved: bool, note: str = "") -> dict | None:
        """Approve or reject a proposal in the project that owns it."""
        return self._write(fid, lambda store, local: _reports.decide(store, local, approved, note))

    def _write(self, fid: str, op) -> dict | None:
        key, local = split_id(fid)
        p = self.get(key)
        with _open(p, write=True) as store:
            if not store.one("SELECT 1 FROM ck_report_items WHERE id=?", (local,)):
                return None
            it = op(store, local)
            if it is None:
                return None
            # Ask the owning project to carry the decision out, exactly as its own web process
            # would (claudekit.http.flask_bp._trigger_for) — including its condition: a REJECTED
            # proposal has nothing to carry out, and waking another project's agent to do nothing
            # is worse here than in-app, because it is someone else's machine time.
            task = (self._request_apply(store, p, self._agent_of(store, local))
                    if it.get("status") in ("approved", "answered") else None)
        return {**_tag_item(p, it), "applying": task}

    @staticmethod
    def _agent_of(store: _RoStore, item_id: int) -> str | None:
        row = store.one("SELECT r.agent a FROM ck_report_items i "
                        "LEFT JOIN ck_reports r ON r.id = i.report_id WHERE i.id=?", (item_id,))
        return row["a"] if row else None

    def _task_for(self, project: Project, agent: str | None) -> str:
        return project.decisions_tasks.get(agent or "", project.apply_task)

    @staticmethod
    def _request_run(store: _RwStore, task: str) -> str:
        """Set `run_requested` on a task row, materialising the row if the project has never
        scheduled it.

        An 'on approve' agent has no schedule row until something asks for it — `Scheduler._ensure`
        creates it the same way, disabled and with no hour, so it becomes addressable without ever
        firing on a timer.
        """
        if not store.one("SELECT 1 FROM ck_schedule_task WHERE task=?", (task,)):
            store.execute(
                """INSERT INTO ck_schedule_task (task, kind, enabled, paused, args, last_status)
                   VALUES (?, 'agent', 0, 0, '[]', 'never')""", (task,))
        store.execute("UPDATE ck_schedule_task SET run_requested=? WHERE task=?",
                      (time.time(), task))
        return task

    def _request_apply(self, store: _RwStore, project: Project, agent: str | None) -> str | None:
        try:
            return self._request_run(store, self._task_for(project, agent))
        except sqlite3.Error:       # a project whose schema predates run_requested, say
            return None

    def apply(self, project: str | None = None) -> dict:
        """Ask every project with outstanding decisions to carry them out.

        Unlike the single-project route this starts no job of its own — it cannot, because the
        agent belongs to the other project and only that project's scheduler may run it. It leaves
        a run request per project instead, which is the same mechanism the project's own UI uses,
        and reports what it queued so the caller can say so honestly rather than implying work is
        already under way here.
        """
        queued: list[dict] = []
        failed: list[dict] = []
        for p in self._selected(project):
            try:
                with _open(p, write=True) as store:
                    pending = _reports.actionable(store)
                    if not pending:
                        continue
                    by_task: dict[str, list[int]] = {}
                    for it in pending:
                        by_task.setdefault(self._task_for(p, it.get("agent")), []).append(
                            int(it["id"]))
                    for task, ids in by_task.items():
                        self._request_run(store, task)
                        queued.append({"project": p.key, "project_label": p.label,
                                       "task": task, "items": ids})
            except (ProjectUnavailable, sqlite3.Error, OSError) as e:
                failed.append({"project": p.key, "error": str(e)})
        if not queued:
            return {"ok": True, "queued": [], "failed": failed,
                    "skipped": "nothing approved or answered yet"}
        n = sum(len(q["items"]) for q in queued)
        where = ", ".join(sorted({q["project_label"] for q in queued}))
        return {"ok": True, "queued": queued, "failed": failed,
                "note": f"Queued {n} item{'s' if n != 1 else ''} in {where} — "
                        "each project's scheduler picks the run up on its next tick."}

    # ---------------------------------------------------------------- misc
    def actionable(self, project: str | None = None) -> list[dict]:
        """Approved/answered items nobody has carried out yet, across the fleet."""
        out: list[dict] = []
        for p in self._selected(project):
            try:
                with _open(p) as store:
                    out += [_tag_item(p, it) for it in _reports.actionable(store)]
            except (ProjectUnavailable, sqlite3.Error, OSError):
                continue
        return out

    def counts(self) -> dict:
        """Headline numbers for a badge: totals across every reachable project."""
        rows = self.status()
        return {
            "projects": len(rows),
            "degraded": sum(0 if r["ok"] else 1 for r in rows),
            "reports": sum(r["reports"] for r in rows),
            "open_items": sum(r["open_items"] for r in rows),
            "pending_action": sum(r["pending_action"] for r in rows),
        }


__all__ = [
    "Fleet", "Project", "ProjectUnavailable", "UnknownProject",
    "SEP", "make_id", "split_id",
]
