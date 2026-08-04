"""Flask adapter — the same routes as the FastAPI one, for hosts on Flask.

Kept deliberately parallel to `fastapi_router.py`: same paths, same payloads, same status codes,
so a frontend can talk to either without knowing which is behind it.
"""

from __future__ import annotations

import contextlib
import time

from flask import Blueprint, jsonify, request

from .. import agent_run, configfiles, followup, jobs, registry, reports, services


def build_blueprint(kit, prefix: str = "/api/kit") -> Blueprint:
    bp = Blueprint("claudekit", __name__, url_prefix=prefix)
    cfg, store, sched = kit.cfg, kit.store, kit.scheduler

    def _err(code: int, msg: str):
        return jsonify({"detail": msg}), code

    def _guard(fn, *a, **kw):
        """Return (value, None) or (None, error_response) — Flask has no exception-handler layer
        as convenient as FastAPI's, so callers check explicitly."""
        try:
            return fn(*a, **kw), None
        except KeyError as e:
            return None, _err(404, str(e))
        except PermissionError as e:
            return None, _err(403, str(e))
        except (ValueError, FileNotFoundError) as e:
            return None, _err(400, str(e))

    def _body() -> dict:
        return request.get_json(silent=True) or {}

    def _start(task: str):
        """Start a task whichever process owns the scheduler (see the FastAPI adapter)."""
        if sched.in_process:
            return _guard(sched.run_now, task)

        before = {j["id"] for j in jobs.recent(store, limit=10)}
        res, err = _guard(sched.request_run, task)
        if err:
            return None, err
        deadline = time.time() + 6
        while time.time() < deadline:
            time.sleep(0.4)
            for j in jobs.recent(store, limit=10):
                if j["id"] not in before and (j.get("params") or {}).get("task") == task:
                    return {"ok": True, "task": task, "job": j["id"]}, None
        return {**res, "scheduler_alive": sched.alive,
                "note": "queued — the scheduler will pick it up"
                        if sched.alive else "scheduler daemon is not running"}, None

    # ------------------------------------------------------------------ health
    @bp.get("/health")
    def health():
        return jsonify({
            "ok": True,
            "app": cfg.app_name,
            "root": str(cfg.root),
            "scheduler": sched.alive,
            "scheduler_mode": "in-process" if sched.in_process else "daemon",
            "claude": cfg.claude_available(),
            "agents": len(cfg.agents),
            "adapters": len(cfg.adapters),
        })

    # ------------------------------------------------------------------ agents
    @bp.get("/agents")
    def list_agents():
        return jsonify(registry.describe(cfg, store))

    @bp.post("/agents/<name>/prompt")
    def set_prompt(name):
        _, err = _guard(registry.resolve, cfg, store, name)
        if err:
            return err
        b = _body()
        registry.set_prompt(store, name, b.get("system"), b.get("prompt"))
        return jsonify({"ok": True, "agent": name})

    @bp.post("/agents/<name>/reset")
    def reset_agent(name):
        _, err = _guard(registry.resolve, cfg, store, name)
        if err:
            return err
        registry.reset(store, name)
        return jsonify({"ok": True, "agent": name})

    @bp.post("/agents/<name>/hour")
    def set_hour(name):
        _, err = _guard(registry.resolve, cfg, store, name)
        if err:
            return err
        hour = registry.set_hour(store, name, int(_body().get("hour", 4)))
        sched.update(name, hour=hour)
        return jsonify({"ok": True, "agent": name, "hour": hour})

    @bp.post("/agents/<name>/run")
    def run_agent(name):
        _, err = _guard(registry.resolve, cfg, store, name)
        if err:
            return err
        sched._ensure(name, "agent", hour=registry.get_hour(cfg, store, name))
        res, err = _start(name)
        return err or jsonify(res)

    # -------------------------------------------------------------- transcripts
    @bp.get("/agent_history")
    def history():
        return jsonify(agent_run.list_history(cfg, request.args.get("agent")))

    @bp.get("/agent_history/<agent>/<path:filename>")
    def transcript(agent, filename):
        res, err = _guard(agent_run.parse_transcript, cfg, agent, filename)
        return err or jsonify(res)

    # ----------------------------------------------------------------- reports
    @bp.get("/reports")
    def list_reports():
        return jsonify(reports.recent(store, limit=int(request.args.get("limit", 30)),
                                      agent=request.args.get("agent")))

    @bp.get("/reports/open")
    def open_items():
        return jsonify(reports.open_items(store))

    @bp.get("/reports/<int:rid>")
    def get_report(rid):
        rep = reports.get(store, rid)
        return jsonify(rep) if rep else _err(404, "no such report")

    def _trigger_for(it) -> str | None:
        """Start the task that should carry out this decision, and return its name.

        Recording a decision used to be the whole of it: the host's own route triggered an apply
        run, but once the UI moved to these routes nothing did, so approvals sat untouched until
        someone remembered the 'Apply now' button. Requested through the scheduler rather than run
        here, because the loop usually lives in a separate daemon.
        """
        if not it or it.get("status") not in ("approved", "answered"):
            return None
        spec = cfg.agents.get(it.get("agent") or "")
        task = (spec.decisions_task if spec and spec.decisions_task else "applydecisions")
        try:
            sched.request_run(task)
            return task
        except KeyError:            # host declares no such task — leave it for the manual button
            return None

    @bp.get("/reports/<int:rid>/thread")
    def report_thread(rid):
        return jsonify(reports.thread(store, rid))

    @bp.post("/reports/<int:rid>/ask")
    def report_ask(rid):
        """Ask an agent about this report. Body: {prompt, agent?, resume?}."""
        b = _body()
        res = followup.ask(cfg, store, rid, str(b.get("prompt", "")),
                           agent=b.get("agent") or None,
                           resume=b.get("resume", True) is not False)
        return jsonify(res) if res.get("ok") else _err(400, res.get("error", "failed"))

    @bp.post("/reports/item/<int:item_id>/answer")
    def answer_item(item_id):
        it = reports.answer(store, item_id, str(_body().get("answer", "")))
        if not it:
            return _err(404, "no such item")
        return jsonify({**it, "applying": _trigger_for(reports.item_with_agent(store, item_id))})

    @bp.post("/reports/item/<int:item_id>/decide")
    def decide_item(item_id):
        b = _body()
        it = reports.decide(store, item_id, bool(b.get("approved")), str(b.get("note", "")))
        if not it:
            return _err(404, "no such item")
        return jsonify({**it, "applying": _trigger_for(reports.item_with_agent(store, item_id))})

    @bp.post("/reports/apply")
    def apply_decisions():
        name = "applydecisions"
        if name not in cfg.agents:
            return _err(400, "host declares no 'applydecisions' agent")
        spec = registry.resolve(cfg, store, name)
        # Only the items this agent owns. Anything raised by an agent that handles its own
        # decisions is started separately, so the generic apply agent is never handed a finding
        # from a domain it knows nothing about.
        routed = reports.route_decisions(cfg, store)
        prompt, ids = reports.build_apply_prompt(store, spec.prompt, routed.get(name, []))
        others = {t: [i["id"] for i in v] for t, v in routed.items() if t != name}
        for task in others:
            with contextlib.suppress(KeyError):
                sched.request_run(task)
        if not ids:
            return jsonify({"ok": True, "skipped": "nothing approved for this agent",
                            "delegated": others})

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
        return jsonify({"ok": True, "job": job, "items": ids})

    # --------------------------------------------------------------- scheduler
    @bp.get("/schedule")
    def schedule():
        return jsonify({"alive": sched.alive, "tasks": sched.tasks(),
                        "interval_sec": sched.global_interval()})

    @bp.post("/schedule/run")
    def run_all():
        """No task named = run every enabled one (HomeFlix's 'crawl everything now')."""
        return jsonify(sched.run_all())

    @bp.post("/schedule/interval")
    def set_interval():
        return jsonify(sched.set_global_interval(int(_body().get("sec", 21600))))

    @bp.post("/schedule/<task>")
    def update_task(task):
        b = _body()
        row, err = _guard(sched.update, task,
                          **{k: b.get(k) for k in
                             ("enabled", "paused", "interval_sec", "hour", "args")})
        if err:
            return err
        return jsonify(row) if row else _err(404, "no such task")

    @bp.post("/schedule/<task>/run")
    def run_task(task):
        res, err = _start(task)
        return err or jsonify(res)

    # -------------------------------------------------------------------- jobs
    @bp.get("/jobs")
    def list_jobs():
        return jsonify(jobs.recent(store, request.args.get("kind"),
                                   int(request.args.get("limit", 20))))

    @bp.get("/jobs/<int:job_id>")
    def get_job(job_id):
        j = jobs.get(store, job_id)
        return jsonify(j) if j else _err(404, "no such job")

    # ---------------------------------------------------------------- services
    @bp.get("/services")
    def list_services():
        return jsonify(services.describe(cfg))

    @bp.post("/services/<key>/<action>")
    def service_action(key, action):
        fn = services.restart_self_safe if action == "restart" else services.act
        res, err = (_guard(fn, cfg, key) if action == "restart"
                    else _guard(fn, cfg, key, action))
        return err or jsonify(res)

    # ------------------------------------------------------------ config files
    @bp.get("/config")
    def list_config():
        return jsonify(configfiles.describe(cfg))

    @bp.get("/config/<key>")
    def read_config(key):
        res, err = _guard(configfiles.read, cfg, key)
        return err or jsonify(res)

    @bp.put("/config/<key>")
    def write_config(key):
        b = _body()
        res, err = _guard(configfiles.write, cfg, key,
                          content=b.get("content"), entries=b.get("entries"))
        return err or jsonify(res)

    return bp
