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
