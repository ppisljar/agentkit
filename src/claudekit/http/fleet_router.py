"""FastAPI adapter for `claudekit.fleet` — the multi-project reports API.

Deliberately shaped like the single-project router in `fastapi_router.py`: same paths under the
prefix, same payloads. That is what lets the kit's own `KitReports` React component talk to a fleet
host with nothing but a different `base` — the only difference a client sees is that ids are fleet
ids ("homeflix:23" rather than 23) and every row carries `project*` fields.

Extra routes a fleet has and a single project does not:

    GET  {prefix}/projects   the configured projects, including any that are broken
    GET  {prefix}/counts     headline totals for a badge

`POST {prefix}/reports/apply` differs in kind, not shape: a fleet cannot run another project's
agent, so it queues a run request per project and returns what it queued (see `Fleet.apply`).
"""

from __future__ import annotations

from fastapi import APIRouter, Body, HTTPException, Query

from ..fleet import Fleet, ProjectUnavailable, UnknownProject


def build_router(fleet: Fleet, prefix: str = "/api/fleet") -> APIRouter:
    r = APIRouter(prefix=prefix, tags=["fleet"])

    def _guard(fn, *a, **kw):
        """Map fleet errors onto status codes so the core never imports a web framework."""
        try:
            return fn(*a, **kw)
        except UnknownProject as e:
            raise HTTPException(404, str(e)) from e
        except ProjectUnavailable as e:
            # The dashboard is up, the project's database is not. 502 says which of the two.
            raise HTTPException(502, str(e)) from e
        except ValueError as e:
            raise HTTPException(400, str(e)) from e

    # ------------------------------------------------------------------ health
    @r.get("/health")
    def health() -> dict:
        return {"ok": True, **fleet.counts()}

    @r.get("/projects")
    def projects() -> list[dict]:
        return fleet.status()

    @r.get("/counts")
    def counts() -> dict:
        return fleet.counts()

    # ----------------------------------------------------------------- reports
    @r.get("/reports")
    def list_reports(limit: int = 30, project: str | None = None,
                     agent: str | None = Query(None)) -> list[dict]:
        return _guard(fleet.reports, limit=limit, project=project, agent=agent)

    # Declared BEFORE /reports/{fid} so "open" is not swallowed as a fleet id.
    @r.get("/reports/open")
    def open_items(project: str | None = None) -> list[dict]:
        return _guard(fleet.open_items, project=project)

    @r.post("/reports/apply")
    def apply_decisions(project: str | None = None) -> dict:
        return _guard(fleet.apply, project=project)

    @r.get("/reports/{fid}")
    def get_report(fid: str) -> dict:
        rep = _guard(fleet.report, fid)
        if not rep:
            raise HTTPException(404, "no such report")
        return rep

    @r.post("/reports/item/{fid}/answer")
    def answer_item(fid: str, body: dict = Body(...)) -> dict:
        it = _guard(fleet.answer, fid, str(body.get("answer", "")))
        if not it:
            raise HTTPException(404, "no such item")
        return it

    @r.post("/reports/item/{fid}/decide")
    def decide_item(fid: str, body: dict = Body(...)) -> dict:
        it = _guard(fleet.decide, fid, bool(body.get("approved")), str(body.get("note", "")))
        if not it:
            raise HTTPException(404, "no such item")
        return it

    return r
