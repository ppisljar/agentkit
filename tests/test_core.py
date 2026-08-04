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
    for _ in range(750):
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


# ---------------------------------------------------------------- new-item counting

def test_count_items_records_delta_and_reaches_post_run(kit):
    """HomeFlix's '+N new' and its rebuild-only-if-something-changed gate."""
    counts = iter([10, 13])
    kit.cfg.count_items = lambda task: next(counts)
    seen = []
    kit.cfg.post_run = lambda task, status, info: seen.append(info)
    _wait(kit, kit.scheduler.run_now("scraper")["job"])
    assert kit.scheduler.get("scraper")["last_new_count"] == 3
    assert seen[0]["new_count"] == 3


def test_count_items_absent_leaves_new_count_null(kit):
    _wait(kit, kit.scheduler.run_now("scraper")["job"])
    assert kit.scheduler.get("scraper")["last_new_count"] is None


def test_broken_count_items_does_not_fail_the_run(kit):
    def boom(task):
        raise RuntimeError("count exploded")
    kit.cfg.count_items = boom
    j = _wait(kit, kit.scheduler.run_now("scraper")["job"])
    assert j["status"] == "done", j
    assert kit.scheduler.get("scraper")["last_new_count"] is None


def test_count_never_goes_negative(kit):
    counts = iter([10, 4])          # a run that pruned rows must not report -6
    kit.cfg.count_items = lambda task: next(counts)
    _wait(kit, kit.scheduler.run_now("scraper")["job"])
    assert kit.scheduler.get("scraper")["last_new_count"] == 0


# ---------------------------------------------------------------- report overrides

def test_report_explicit_fields_override_payload(kit):
    """A wrapper script that knows the truth better than the model does."""
    rid = reports.save(kit.store, "sldubs",
                       {"status": "ok", "summary": "model said this", "findings": ["a"]},
                       raw="raw text", status="warn", summary="Queued 4 (3 torrent, 1 provider)",
                       detail="QUEUED THIS RUN (4): ...", findings=[])
    r = reports.get(kit.store, rid)
    assert r["status"] == "warn"
    assert r["summary"] == "Queued 4 (3 torrent, 1 provider)"
    assert r["detail"].startswith("QUEUED THIS RUN")
    assert r["findings"] == []


def test_report_without_overrides_still_reads_the_payload(kit):
    rid = reports.save(kit.store, "healthcheck",
                       {"status": "ok", "summary": "s", "findings": [{"title": "t"}]},
                       raw="raw")
    r = reports.get(kit.store, rid)
    assert (r["status"], r["summary"], r["detail"]) == ("ok", "s", "raw")
    assert len(r["findings"]) == 1


# ---------------------------------------------------------------- pre/post hook pair

def test_pre_run_result_is_threaded_to_post_run(kit):
    """pre+post is the general form; count_items is just the common case with a schema home."""
    kit.cfg.pre_run = lambda task: {"before": 41}
    seen = []
    kit.cfg.post_run = lambda task, status, info: seen.append(info)
    _wait(kit, kit.scheduler.run_now("scraper")["job"])
    assert seen[0]["pre"] == {"before": 41}


def test_pre_run_fires_for_agent_scripts_too(kit):
    kit.cfg.pre_run = lambda task: f"pre:{task}"
    seen = []
    kit.cfg.post_run = lambda task, status, info: seen.append(info["pre"])
    (kit.cfg.root / "p.py").write_text("print('ok')\n", encoding="utf-8")
    kit.cfg.agents["scripted3"] = AgentSpec(name="scripted3", label="S", description="d",
                                            system="s", prompt="p", schedule="daily",
                                            script=["p.py"])
    kit.scheduler.sync_tasks()
    _wait(kit, kit.scheduler.run_now("scripted3")["job"])
    assert "pre:scripted3" in seen, seen


def test_broken_pre_run_hook_does_not_fail_the_run(kit):
    def boom(task):
        raise RuntimeError("pre exploded")
    kit.cfg.pre_run = boom
    seen = []
    kit.cfg.post_run = lambda task, status, info: seen.append(info["pre"])
    j = _wait(kit, kit.scheduler.run_now("scraper")["job"])
    assert j["status"] == "done", j
    assert seen == [None]


# ---------------------------------------------------------------- flask adapter BEHAVIOUR
# Route parity is asserted above, but that only compares URL rules. These drive real requests
# through the blueprint — the adapter HomeFlix's whole admin UI would depend on.

@pytest.fixture()
def client(kit):
    pytest.importorskip('flask')
    from flask import Flask
    app = Flask(__name__)
    app.register_blueprint(kit.flask_blueprint())
    app.config.update(TESTING=True)
    return app.test_client()


def test_flask_health(client, kit):
    r = client.get('/api/kit/health')
    assert r.status_code == 200
    d = r.get_json()
    assert d["ok"] and d["app"] == "test"
    assert d["agents"] == len(kit.cfg.agents) and d["adapters"] == 1


def test_flask_agents_list_and_prompt_roundtrip(client):
    r = client.get('/api/kit/agents')
    assert r.status_code == 200
    names = {a["name"] for a in r.get_json()}
    assert "healthcheck" in names

    assert client.post('/api/kit/agents/healthcheck/prompt',
                       json={"system": "S2", "prompt": "P2"}).status_code == 200
    got = [a for a in client.get('/api/kit/agents').get_json() if a["name"] == "healthcheck"][0]
    assert got["system"].startswith("S2") and got["prompt"] == "P2" and got["customized"]

    assert client.post('/api/kit/agents/healthcheck/reset').status_code == 200
    got = [a for a in client.get('/api/kit/agents').get_json() if a["name"] == "healthcheck"][0]
    assert not got["customized"]


def test_flask_unknown_agent_is_404_everywhere(client):
    for path in ('/api/kit/agents/nope/prompt', '/api/kit/agents/nope/reset',
                 '/api/kit/agents/nope/hour', '/api/kit/agents/nope/run'):
        r = client.post(path, json={})
        assert r.status_code == 404, (path, r.status_code)
        assert "detail" in r.get_json()


def test_flask_set_hour_clamps_and_persists(client):
    r = client.post('/api/kit/agents/healthcheck/hour', json={"hour": 99})
    assert r.status_code == 200 and r.get_json()["hour"] == 23
    r = client.post('/api/kit/agents/healthcheck/hour', json={"hour": 6})
    assert r.get_json()["hour"] == 6


def test_flask_reports_flow(client, kit):
    rid = reports.save(kit.store, "healthcheck",
                       {"status": "ok", "summary": "s", "questions": ["q1"],
                        "proposals": ["p1"], "findings": [{"title": "f"}]}, raw="raw")
    assert client.get('/api/kit/reports').get_json()[0]["id"] == rid
    rep = client.get(f'/api/kit/reports/{rid}').get_json()
    assert rep["summary"] == "s" and rep["open_items"] == 2

    items = client.get('/api/kit/reports/open').get_json()
    q = [i for i in items if i["kind"] == "question"][0]
    p = [i for i in items if i["kind"] == "proposal"][0]

    assert client.post(f'/api/kit/reports/item/{q["id"]}/answer',
                       json={"answer": "because"}).get_json()["status"] == "answered"
    assert client.post(f'/api/kit/reports/item/{p["id"]}/decide',
                       json={"approved": True}).get_json()["status"] == "approved"
    assert client.get(f'/api/kit/reports/{rid}').get_json()["open_items"] == 0


def test_flask_missing_report_and_item_are_404(client):
    assert client.get('/api/kit/reports/9999').status_code == 404
    assert client.post('/api/kit/reports/item/9999/answer', json={"answer": "x"}).status_code == 404
    assert client.post('/api/kit/reports/item/9999/decide', json={"approved": True}).status_code == 404


def test_flask_apply_with_nothing_approved_is_skipped(client):
    d = client.post('/api/kit/reports/apply').get_json()
    assert d["ok"] and d.get("skipped")


def test_flask_schedule_list_and_update(client):
    d = client.get('/api/kit/schedule').get_json()
    assert "tasks" in d and any(t["task"] == "scraper" for t in d["tasks"])
    row = client.post('/api/kit/schedule/scraper', json={"enabled": 1, "interval_sec": 900}).get_json()
    assert row["enabled"] == 1 and row["interval_sec"] == 900
    assert client.post('/api/kit/schedule/nope', json={"enabled": 1}).status_code == 404


def test_flask_jobs_endpoints(client, kit):
    jid = jobs.run_bg(kit.store, "test", "t", {}, lambda job_id, p: None)
    assert any(j["id"] == jid for j in client.get('/api/kit/jobs').get_json())
    assert client.get(f'/api/kit/jobs/{jid}').get_json()["id"] == jid
    assert client.get('/api/kit/jobs/999999').status_code == 404


def test_flask_services_and_denied_action(client):
    assert client.get('/api/kit/services').get_json()[0]["key"] == "backend"
    # 'stop' is not in the declared allowlist -> refused, not attempted
    assert client.post('/api/kit/services/backend/stop').status_code in (400, 403)
    assert client.post('/api/kit/services/nosuch/restart').status_code == 404


def test_flask_config_read_write_and_masking(client):
    assert client.get('/api/kit/config').get_json()[0]["key"] == "env"
    d = client.get('/api/kit/config/env').get_json()
    kv = [e for e in d["entries"] if e.get("type") == "kv"]
    secret = [e for e in kv if e["key"] == "SECRET"][0]
    assert secret["value"] == configfiles.MASK

    # A write REPLACES the file from the entries given, and _render_env only emits entries that
    # carry `type` — so a client must round-trip what it read rather than invent bare pairs.
    entries = client.get('/api/kit/config/env').get_json()["entries"]
    for e in entries:
        if e.get("type") == "kv" and e["key"] == "FOO":
            e["value"] = "baz"
    r = client.put('/api/kit/config/env', json={"entries": entries})
    assert r.status_code == 200
    d = client.get('/api/kit/config/env').get_json()
    kv = [e for e in d["entries"] if e.get("type") == "kv"]
    assert [e for e in kv if e["key"] == "FOO"][0]["value"] == "baz"
    assert client.get('/api/kit/config/nope').status_code == 404


def test_flask_transcript_of_missing_run_is_404(client):
    assert client.get('/api/kit/agent_history').status_code == 200
    # Both adapters map FileNotFoundError to 400 (_guard). Debatable for a missing file, but it
    # is the shared contract — pinned here so a change to one adapter can't drift from the other.
    assert client.get('/api/kit/agent_history/healthcheck/nope.jsonl').status_code == 400
