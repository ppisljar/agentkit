"""FastAPI adapter — mounts the whole kit under one prefix.

Deliberately thin: every handler translates a request into a core call and returns its result.
Errors from the core map onto status codes here so the core never imports a web framework.
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Query
from fastapi.responses import JSONResponse

from .. import agent_run, configfiles, jobs, registry, reports, services


def build_router(kit, prefix: str = "/api/kit") -> APIRouter:
    r = APIRouter(prefix=prefix, tags=["claudekit"])
    cfg, store, sched = kit.cfg, kit.store, kit.scheduler

    def _guard(fn, *a, **kw):
        """Map core exceptions to HTTP codes; everything else is a genuine 500."""
        try:
            return fn(*a, **kw)
        except KeyError as e:
            raise HTTPException(404, str(e)) from e
        except PermissionError as e:
            raise HTTPException(403, str(e)) from e
        except (ValueError, FileNotFoundError) as e:
            raise HTTPException(400, str(e)) from e

    def _start(task: str) -> dict:
        """Start a task, whichever process owns the scheduler.

        In-process we start it directly and get a job id straight away. With a separate daemon we
        leave a request and wait briefly for it to pick the task up, so the UI still receives a job
        id to poll in the common case instead of having to guess.
        """
        if sched.in_process:
            return _guard(sched.run_now, task)

        before = {j["id"] for j in jobs.recent(store, limit=10)}
        res = _guard(sched.request_run, task)
        deadline = time.time() + 6
        while time.time() < deadline:
            time.sleep(0.4)
            for j in jobs.recent(store, limit=10):
                if j["id"] not in before and (j.get("params") or {}).get("task") == task:
                    return {"ok": True, "task": task, "job": j["id"]}
        # The daemon may simply be down; say so rather than reporting a silent success.
        return {**res, "scheduler_alive": sched.alive,
                "note": "queued — the scheduler will pick it up"
                        if sched.alive else "scheduler daemon is not running"}

    # ------------------------------------------------------------------ health
    @r.get("/health")
    def health() -> dict:
        return {
            "ok": True,
            "app": cfg.app_name,
            "root": str(cfg.root),
            "scheduler": sched.alive,
            "scheduler_mode": "in-process" if sched.in_process else "daemon",
            "claude": cfg.claude_available(),
            "agents": len(cfg.agents),
            "adapters": len(cfg.adapters),
        }

    # ------------------------------------------------------------------ agents
    @r.get("/agents")
    def list_agents() -> list[dict]:
        return registry.describe(cfg, store)

    @r.post("/agents/{name}/prompt")
    def set_prompt(name: str, body: dict = Body(...)) -> dict:
        _guard(registry.resolve, cfg, store, name)          # 404 for unknown agent
        registry.set_prompt(store, name, body.get("system"), body.get("prompt"))
        return {"ok": True, "agent": name}

    @r.post("/agents/{name}/reset")
    def reset_agent(name: str) -> dict:
        _guard(registry.resolve, cfg, store, name)
        registry.reset(store, name)
        return {"ok": True, "agent": name}

    @r.post("/agents/{name}/hour")
    def set_hour(name: str, body: dict = Body(...)) -> dict:
        _guard(registry.resolve, cfg, store, name)
        hour = registry.set_hour(store, name, int(body.get("hour", 4)))
        sched.update(name, hour=hour)
        return {"ok": True, "agent": name, "hour": hour}

    @r.post("/agents/{name}/run")
    def run_agent(name: str) -> dict:
        _guard(registry.resolve, cfg, store, name)
        sched._ensure(name, "agent", hour=registry.get_hour(cfg, store, name))
        return _start(name)

    # -------------------------------------------------------------- transcripts
    @r.get("/agent_history")
    def history(agent: str | None = Query(None)) -> list[dict]:
        return agent_run.list_history(cfg, agent)

    @r.get("/agent_history/{agent}/{filename}")
    def transcript(agent: str, filename: str) -> dict:
        return _guard(agent_run.parse_transcript, cfg, agent, filename)

    # ----------------------------------------------------------------- reports
    @r.get("/reports")
    def list_reports(limit: int = 30, agent: str | None = None) -> list[dict]:
        return reports.recent(store, limit=limit, agent=agent)

    @r.get("/reports/open")
    def open_items() -> list[dict]:
        return reports.open_items(store)

    @r.get("/reports/{rid}")
    def get_report(rid: int) -> dict:
        rep = reports.get(store, rid)
        if not rep:
            raise HTTPException(404, "no such report")
        return rep

    @r.post("/reports/item/{item_id}/answer")
    def answer_item(item_id: int, body: dict = Body(...)) -> dict:
        it = reports.answer(store, item_id, str(body.get("answer", "")))
        if not it:
            raise HTTPException(404, "no such item")
        return it

    @r.post("/reports/item/{item_id}/decide")
    def decide_item(item_id: int, body: dict = Body(...)) -> dict:
        it = reports.decide(store, item_id, bool(body.get("approved")),
                            str(body.get("note", "")))
        if not it:
            raise HTTPException(404, "no such item")
        return it

    @r.post("/reports/apply")
    def apply_decisions() -> Any:
        """Hand every approved/answered item to the apply agent."""
        name = "applydecisions"
        if name not in cfg.agents:
            raise HTTPException(400, "host declares no 'applydecisions' agent")
        spec = registry.resolve(cfg, store, name)
        prompt, ids = reports.build_apply_prompt(store, spec.prompt)
        if not ids:
            return JSONResponse({"ok": True, "skipped": "nothing approved"}, status_code=200)

        def _work(job_id: int, _params: dict) -> None:
            res = agent_run.run(cfg, spec, prompt=prompt)
            payload = agent_run.extract_json(res["result"])
            rid = reports.save(store, name, payload, raw=res["result"],
                               duration_sec=res["duration_sec"], ok=res["returncode"] == 0)
            done = [a.get("item_id") for a in ((payload or {}).get("actions") or [])
                    if a.get("done") and a.get("item_id") is not None]
            reports.mark_done(store, done or ids)
            jobs.update(store, job_id, status="done",
                        result={"report": rid, "items": done or ids},
                        log=res["result"][-20000:])

        job = jobs.run_bg(store, "agent", f"agent:{name}", {"items": ids}, _work)
        return {"ok": True, "job": job, "items": ids}

    # --------------------------------------------------------------- scheduler
    @r.get("/schedule")
    def schedule() -> dict:
        return {"alive": sched.alive, "tasks": sched.tasks()}

    @r.post("/schedule/{task}")
    def update_task(task: str, body: dict = Body(...)) -> dict:
        row = sched.update(task, **{k: body.get(k) for k in
                                    ("enabled", "paused", "interval_sec", "hour", "args")})
        if not row:
            raise HTTPException(404, "no such task")
        return row

    @r.post("/schedule/{task}/run")
    def run_task(task: str) -> dict:
        return _start(task)

    # -------------------------------------------------------------------- jobs
    @r.get("/jobs")
    def list_jobs(kind: str | None = None, limit: int = 20) -> list[dict]:
        return jobs.recent(store, kind, limit)

    @r.get("/jobs/{job_id}")
    def get_job(job_id: int) -> dict:
        j = jobs.get(store, job_id)
        if not j:
            raise HTTPException(404, "no such job")
        return j

    # ---------------------------------------------------------------- services
    @r.get("/services")
    def list_services() -> list[dict]:
        return services.describe(cfg)

    @r.post("/services/{key}/{action}")
    def service_action(key: str, action: str) -> dict:
        if action == "restart":
            return _guard(services.restart_self_safe, cfg, key)
        return _guard(services.act, cfg, key, action)

    # ------------------------------------------------------------ config files
    @r.get("/config")
    def list_config() -> list[dict]:
        return configfiles.describe(cfg)

    @r.get("/config/{key}")
    def read_config(key: str) -> dict:
        return _guard(configfiles.read, cfg, key)

    @r.put("/config/{key}")
    def write_config(key: str, body: dict = Body(...)) -> dict:
        return _guard(configfiles.write, cfg, key,
                      content=body.get("content"), entries=body.get("entries"))

    return r
