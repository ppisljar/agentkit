"""Core tests — no network, no Claude Code, no systemd required."""

from __future__ import annotations

import json

import pytest

from claudekit import AgentSpec, ConfigFileSpec, Kit, KitConfig, ServiceSpec
from claudekit import agent_run, configfiles, jobs, registry, reports


@pytest.fixture()
def kit(tmp_path):
    root = tmp_path / "app"
    (root / "data").mkdir(parents=True)
    (root / ".env").write_text("# comment\nFOO=bar\nSECRET=hunter2\n\n", encoding="utf-8")
    cfg = KitConfig(
        root=root,
        data_dir=root / "data",
        app_name="test",
        agents={
            "healthcheck": AgentSpec(
                name="healthcheck", label="Health check", description="d",
                system="sys", prompt="pr", schedule="daily"),
            "applydecisions": AgentSpec(
                name="applydecisions", label="Apply", description="d",
                system="sys", prompt="FOOTER", schedule="on approve"),
        },
        services={"backend": ServiceSpec(key="backend", unit="x.service", label="Backend")},
        config_files={"env": ConfigFileSpec(key="env", path=".env", label="Env",
                                            format="env", secret_keys=("SECRET",))},
        adapters={"scraper": ["/bin/echo", "scraped"]},
    )
    k = Kit(cfg)
    k.store.connect()
    return k


def test_schema_created(kit):
    names = {r["name"] for r in kit.store.query(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"ck_reports", "ck_report_items", "ck_agent_config",
            "ck_jobs", "ck_schedule_task"} <= names


def test_registry_override_and_reset(kit):
    assert registry.resolve(kit.cfg, kit.store, "healthcheck").system == "sys"
    registry.set_prompt(kit.store, "healthcheck", "NEW SYS", "NEW PROMPT")
    eff = registry.resolve(kit.cfg, kit.store, "healthcheck")
    assert (eff.system, eff.prompt) == ("NEW SYS", "NEW PROMPT")
    assert registry.describe(kit.cfg, kit.store)[0]["customized"] is True
    registry.reset(kit.store, "healthcheck")
    assert registry.resolve(kit.cfg, kit.store, "healthcheck").system == "sys"


def test_registry_unknown_agent(kit):
    with pytest.raises(KeyError):
        registry.resolve(kit.cfg, kit.store, "nope")


def test_extract_json_takes_last_block():
    text = 'thinking ```json\n{"a":1}\n``` more ```json\n{"status":"ok","summary":"s"}\n```'
    assert agent_run.extract_json(text) == {"status": "ok", "summary": "s"}


def test_extract_json_handles_none_and_bare():
    assert agent_run.extract_json("no json here") is None
    assert agent_run.extract_json('{"status":"ok"}') == {"status": "ok"}


def test_report_roundtrip_and_approval(kit):
    payload = {
        "status": "warn",
        "summary": "one thing is off",
        "findings": [{"area": "disk", "severity": "warn", "title": "low", "detail": "5%"}],
        "questions": ["should I prune old logs?"],
        "proposals": ["restart the scraper"],
    }
    rid = reports.save(kit.store, "healthcheck", payload, raw="raw", duration_sec=3)
    rep = reports.get(kit.store, rid)
    assert rep["status"] == "warn"
    assert rep["findings"][0]["area"] == "disk"
    assert len(rep["items"]) == 2

    q = [i for i in rep["items"] if i["kind"] == "question"][0]
    p = [i for i in rep["items"] if i["kind"] == "proposal"][0]

    reports.answer(kit.store, q["id"], "yes, prune them")
    reports.decide(kit.store, p["id"], approved=True)

    prompt, ids = reports.build_apply_prompt(kit.store, "FOOTER")
    assert set(ids) == {q["id"], p["id"]}
    assert "yes, prune them" in prompt and "restart the scraper" in prompt
    assert prompt.rstrip().endswith("FOOTER")

    reports.mark_done(kit.store, ids)
    assert reports.build_apply_prompt(kit.store, "FOOTER")[1] == []


def test_rejected_item_is_not_actionable(kit):
    rid = reports.save(kit.store, "healthcheck", {"proposals": ["dangerous"]})
    it = reports.get(kit.store, rid)["items"][0]
    reports.decide(kit.store, it["id"], approved=False, note="no")
    assert reports.build_apply_prompt(kit.store, "F")[1] == []


def test_jobs_lifecycle(kit):
    done = {}

    def work(job_id, params):
        done["id"] = job_id
        jobs.update(kit.store, job_id, status="done", result={"n": params["n"]})

    jid = jobs.run_bg(kit.store, "test", "label", {"n": 7}, work)
    for _ in range(100):
        j = jobs.get(kit.store, jid)
        if j["status"] != "running":
            break
        import time
        time.sleep(0.02)
    j = jobs.get(kit.store, jid)
    assert j["status"] == "done" and j["result"] == {"n": 7}


def test_job_records_exception(kit):
    def boom(job_id, params):
        raise RuntimeError("kaboom")

    jid = jobs.run_bg(kit.store, "test", "l", {}, boom)
    import time
    for _ in range(100):
        if jobs.get(kit.store, jid)["status"] != "running":
            break
        time.sleep(0.02)
    j = jobs.get(kit.store, jid)
    assert j["status"] == "error" and "kaboom" in j["error"]


def test_configfile_env_masking_and_roundtrip(kit):
    data = configfiles.read(kit.cfg, "env")
    kv = {e["key"]: e for e in data["entries"] if e.get("type") == "kv"}
    assert kv["FOO"]["value"] == "bar"
    assert kv["SECRET"]["value"] == configfiles.MASK      # secret never leaves the server

    # send the mask back unchanged -> stored secret must survive
    entries = data["entries"]
    for e in entries:
        if e.get("key") == "FOO":
            e["value"] = "baz"
    configfiles.write(kit.cfg, "env", entries=entries)

    text = (kit.cfg.root / ".env").read_text()
    assert "FOO=baz" in text
    assert "SECRET=hunter2" in text                        # preserved, not blanked
    assert "# comment" in text                             # comments preserved


def test_configfile_path_escape_refused(kit):
    kit.cfg.config_files["evil"] = ConfigFileSpec(key="evil", path="../../etc/passwd",
                                                  label="evil")
    with pytest.raises(ValueError):
        configfiles.read(kit.cfg, "evil")


def test_scheduler_registers_declared_tasks(kit):
    tasks = {t["task"]: t for t in kit.scheduler.tasks()}
    assert "scraper" in tasks and tasks["scraper"]["kind"] == "adapter"
    assert "healthcheck" in tasks and tasks["healthcheck"]["kind"] == "agent"
    # 'applydecisions' is not daily, so it must not be scheduled
    assert "applydecisions" not in tasks
    # nothing is enabled by default — a fresh install must not start scraping on its own
    assert all(t["enabled"] == 0 for t in tasks.values())


def test_scheduler_update_and_run_adapter(kit):
    kit.scheduler.update("scraper", enabled=1, interval_sec=60)
    assert kit.scheduler.get("scraper")["enabled"] == 1

    res = kit.scheduler.run_now("scraper")
    assert res["ok"]
    import time
    for _ in range(200):
        j = jobs.get(kit.store, res["job"])
        if j["status"] != "running":
            break
        time.sleep(0.02)
    j = jobs.get(kit.store, res["job"])
    assert j["status"] == "done", j
    assert "scraped" in (j["log"] or "")
    assert kit.scheduler.get("scraper")["last_status"] == "ok"


def test_service_action_denied_when_not_allowed(kit):
    from claudekit import services
    with pytest.raises(PermissionError):
        services.act(kit.cfg, "backend", "stop")           # spec allows restart/status only


def test_sudoers_snippet_is_minimal(kit):
    from claudekit import services
    snip = services.sudoers_snippet(kit.cfg, "ppisljar")
    assert "restart x.service" in snip
    assert "stop" not in snip                              # not an allowed action


def test_run_request_is_picked_up_by_due(kit):
    """A web process leaves a marker; the loop owner claims it exactly once."""
    kit.scheduler.request_run('scraper')
    assert kit.scheduler.get('scraper')['run_requested'] is not None

    due = kit.scheduler.due()
    assert 'scraper' in due
    # the marker is cleared when claimed, so a second tick does not re-run it
    assert kit.scheduler.get('scraper')['run_requested'] is None
    assert 'scraper' not in kit.scheduler.due()


def test_run_request_works_on_disabled_task(kit):
    """An explicit 'Run now' must work even when the task is not scheduled."""
    assert kit.scheduler.get('scraper')['enabled'] == 0
    kit.scheduler.request_run('scraper')
    assert 'scraper' in kit.scheduler.due()


def test_request_run_unknown_task(kit):
    with pytest.raises(KeyError):
        kit.scheduler.request_run('nope')


def test_alive_uses_heartbeat_across_processes(kit):
    """`alive` must work for a reader that shares only the database."""
    import time as _t
    from claudekit.scheduler import HEARTBEAT_KEY

    assert kit.scheduler.alive is False          # nothing has beaten yet
    kit.store.set_meta(HEARTBEAT_KEY, str(_t.time()))
    assert kit.scheduler.alive is True           # a fresh beat means someone owns the loop
    assert kit.scheduler.in_process is False     # ...but not this process
    kit.store.set_meta(HEARTBEAT_KEY, str(_t.time() - 3600))
    assert kit.scheduler.alive is False          # stale beat -> the owner is gone


def test_start_without_scheduler_leaves_loop_unclaimed(kit):
    kit.start(run_scheduler=False)
    assert kit.scheduler.in_process is False
    assert kit.scheduler.get('scraper') is not None   # tasks still registered


def _routes(kit, kind):
    """Build one adapter and return its route paths, so parity is checked mechanically."""
    if kind == 'fastapi':
        r = kit.fastapi_router()
        return {route.path for route in r.routes}
    bp = kit.flask_blueprint()
    from flask import Flask
    app = Flask(__name__)
    app.register_blueprint(bp)
    return {str(r.rule) for r in app.url_map.iter_rules() if r.endpoint != 'static'}


def test_both_http_adapters_build_and_agree(kit):
    """Both adapters must exist and expose the same surface.

    Guards the specific mistake of documenting an adapter that was never written.
    """
    pytest.importorskip('fastapi')
    pytest.importorskip('flask')

    fastapi_paths = _routes(kit, 'fastapi')
    flask_paths = _routes(kit, 'flask')

    # normalise the two frameworks' parameter syntaxes: {x} / {x:int} vs <x> / <int:x>
    import re
    def norm(paths):
        out = set()
        for p in paths:
            p = re.sub(r'\{[^}]*\}', '*', p)
            p = re.sub(r'<[^>]*>', '*', p)
            out.add(p.rstrip('/'))
        return out

    assert norm(fastapi_paths) == norm(flask_paths), (
        f"only in fastapi: {norm(fastapi_paths) - norm(flask_paths)}\n"
        f"only in flask:   {norm(flask_paths) - norm(fastapi_paths)}")
    assert len(fastapi_paths) > 15


# ---------------------------------------------------------------- shared system hints

def test_system_hints_appended_and_idempotent(kit):
    kit.cfg.system_hints = (" HINT-A", " HINT-B")
    s = registry.resolve(kit.cfg, kit.store, "healthcheck")
    assert s.system == "sys HINT-A HINT-B"
    # resolving twice must not double-append
    assert registry.resolve(kit.cfg, kit.store, "healthcheck").system == s.system


def test_system_hints_survive_a_user_prompt_override(kit):
    """The point of appending at resolve time: editing a prompt in the UI must not drop a
    host-wide rule."""
    kit.cfg.system_hints = (" HINT-A",)
    registry.set_prompt(kit.store, "healthcheck", "my own system prompt", None)
    assert registry.resolve(kit.cfg, kit.store, "healthcheck").system == "my own system prompt HINT-A"


def test_no_hints_agent_is_exempt(kit):
    kit.cfg.system_hints = (" HINT-A",)
    kit.cfg.agents["chat"] = AgentSpec(name="chat", label="Chat", description="d",
                                       system="sys", prompt="p", no_hints=True)
    assert registry.resolve(kit.cfg, kit.store, "chat").system == "sys"


def test_hint_already_present_is_not_duplicated(kit):
    kit.cfg.system_hints = (" HINT-A",)
    kit.cfg.agents["baked"] = AgentSpec(name="baked", label="B", description="d",
                                        system="sys HINT-A", prompt="p")
    assert registry.resolve(kit.cfg, kit.store, "baked").system == "sys HINT-A"


# ---------------------------------------------------------------- claude binary resolution

def test_claude_bin_found_outside_path(kit, tmp_path, monkeypatch):
    """A systemd unit pins a PATH that omits ~/.local/bin; the CLI must still be found."""
    fake_home = tmp_path / "home"
    (fake_home / ".local" / "bin").mkdir(parents=True)
    exe = fake_home / ".local" / "bin" / "claude"
    exe.write_text("#!/bin/sh\n", encoding="utf-8")
    exe.chmod(0o755)
    monkeypatch.setenv("PATH", "/nonexistent")
    monkeypatch.setenv("HOME", str(fake_home))
    assert kit.cfg.resolve_claude_bin() == str(exe)
    assert kit.cfg.claude_available()


def test_claude_bin_missing_reports_unavailable(kit, monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", "/nonexistent")
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    assert kit.cfg.resolve_claude_bin() is None
    assert not kit.cfg.claude_available()


def test_stall_tools_denied_by_default(kit):
    """An agent run is one `claude -p` turn — waiting for a callback strands the work."""
    for t in ("ScheduleWakeup", "Monitor", "CronCreate"):
        assert t in kit.cfg.deny_tools


# ---------------------------------------------------------------- agent scripts + post-run hook

def _wait(kit, job_id, want="done"):
    import time
    for _ in range(300):
        j = jobs.get(kit.store, job_id)
        if j["status"] != "running":
            return j
        time.sleep(0.02)
    return jobs.get(kit.store, job_id)


def test_agent_with_script_runs_the_script_not_the_model(kit, tmp_path):
    """AgentSpec.script owns the whole run — the kit must not also call Claude."""
    script = kit.cfg.root / "wrapper.py"
    script.write_text("print('wrapper ran')\n", encoding="utf-8")
    kit.cfg.agents["scripted"] = AgentSpec(
        name="scripted", label="Scripted", description="d", system="s", prompt="p",
        schedule="daily", script=["wrapper.py"])
    kit.scheduler.sync_tasks()
    res = kit.scheduler.run_now("scripted")
    j = _wait(kit, res["job"])
    assert j["status"] == "done", j
    assert "wrapper ran" in (j["log"] or "")
    assert kit.scheduler.get("scripted")["last_status"] == "ok"


def test_agent_script_failure_is_reported(kit):
    script = kit.cfg.root / "boom.py"
    script.write_text("import sys; sys.exit(3)\n", encoding="utf-8")
    kit.cfg.agents["boom"] = AgentSpec(name="boom", label="Boom", description="d",
                                       system="s", prompt="p", schedule="daily",
                                       script=["boom.py"])
    kit.scheduler.sync_tasks()
    j = _wait(kit, kit.scheduler.run_now("boom")["job"])
    assert j["status"] == "error", j
    assert kit.scheduler.get("boom")["last_status"] == "error"


def test_post_run_hook_fires_for_adapter(kit):
    seen = []
    kit.cfg.post_run = lambda task, status, info: seen.append((task, status, info))
    _wait(kit, kit.scheduler.run_now("scraper")["job"])
    assert seen and seen[0][0] == "scraper" and seen[0][1] == "ok", seen
    assert "duration_sec" in seen[0][2]


def test_post_run_hook_fires_for_agent_script(kit):
    seen = []
    kit.cfg.post_run = lambda task, status, info: seen.append((task, status))
    (kit.cfg.root / "w.py").write_text("print('x')\n", encoding="utf-8")
    kit.cfg.agents["scripted2"] = AgentSpec(name="scripted2", label="S", description="d",
                                            system="s", prompt="p", schedule="daily",
                                            script=["w.py"])
    kit.scheduler.sync_tasks()
    _wait(kit, kit.scheduler.run_now("scripted2")["job"])
    assert ("scripted2", "ok") in seen, seen


def test_broken_post_run_hook_does_not_fail_the_task(kit):
    def boom(task, status, info):
        raise RuntimeError("hook exploded")
    kit.cfg.post_run = boom
    j = _wait(kit, kit.scheduler.run_now("scraper")["job"])
    assert j["status"] == "done", j
    assert kit.scheduler.get("scraper")["last_status"] == "ok"
