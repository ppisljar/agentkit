"""Follow-up conversations on a report — asking the agent that wrote it a question."""

from __future__ import annotations

import time

import pytest

from claudekit import agent_run, followup, reports
from claudekit.config import AgentSpec

from test_core import client, kit  # noqa: F401 — pytest fixtures


def _report(kit, agent="healthcheck", session=None, **kw):  # noqa: F811
    return reports.save(kit.store, agent, {"summary": "s", **kw}, session=session,
                        detail="the full detail body")


def _settle(kit, msg_id, tries=60):  # noqa: F811
    """run_bg works in a thread; wait for the reply row to leave 'running'."""
    for _ in range(tries):
        row = kit.store.one("SELECT * FROM ck_report_messages WHERE id=?", (msg_id,))
        if row and row["status"] != "running":
            return dict(row)
        time.sleep(0.05)
    raise AssertionError("follow-up never settled")


@pytest.fixture()
def fake_run(monkeypatch):  # noqa: F811
    """Capture what would have been sent to the CLI instead of spawning it."""
    calls = []

    def _run(cfg, spec, prompt=None, timeout=None, resume=None):
        calls.append({"agent": spec.name, "prompt": prompt, "resume": resume,
                      "system": spec.system})
        return {"stdout": "", "returncode": 0, "session": "new-sid",
                "transcript": None, "duration_sec": 1, "result": "because X happened"}

    monkeypatch.setattr(agent_run, "run", _run)
    return calls


def test_session_is_persisted_on_the_report(kit):  # noqa: F811
    """Without this there is nothing to continue: the id used to live only in ck_jobs.result."""
    rid = _report(kit, session="sess-abc")
    assert reports.get(kit.store, rid)["session"] == "sess-abc"
    assert reports.last_session(kit.store, rid) == "sess-abc"


def test_ask_resumes_the_reports_own_session(kit, fake_run):  # noqa: F811
    rid = _report(kit, session="sess-abc")
    res = followup.ask(kit.cfg, kit.store, rid, "why did that happen?")
    assert res["ok"] and res["resumed"] is True
    _settle(kit, res["message_id"])
    assert fake_run[0]["resume"] == "sess-abc"
    # a resumed agent already has the report in context, so it is NOT re-sent
    assert "FULL REPORT" not in fake_run[0]["prompt"]
    assert "why did that happen?" in fake_run[0]["prompt"]


def test_fresh_run_gets_the_whole_report(kit, fake_run):  # noqa: F811
    """Starting a new session must still hand over the report — the owner asked for that
    explicitly, and an agent asked about something it cannot see would be guessing."""
    rid = _report(kit, session="sess-abc", findings=["[warn] disk: nearly full"])
    res = followup.ask(kit.cfg, kit.store, rid, "what should I do?", resume=False)
    assert res["resumed"] is False
    _settle(kit, res["message_id"])
    p = fake_run[0]["prompt"]
    assert fake_run[0]["resume"] is None
    assert "FULL REPORT" in p and "the full detail body" in p
    assert "[warn] disk: nearly full" in p
    assert "what should I do?" in p


def test_thread_records_both_sides(kit, fake_run):  # noqa: F811
    rid = _report(kit, session="s1")
    res = followup.ask(kit.cfg, kit.store, rid, "why?")
    _settle(kit, res["message_id"])
    t = reports.thread(kit.store, rid)
    assert [m["role"] for m in t] == ["user", "agent"]
    assert t[0]["text"] == "why?"
    assert t[1]["text"] == "because X happened"
    assert t[1]["status"] == "done"


def test_next_followup_continues_the_latest_reply(kit, fake_run):  # noqa: F811
    """A back-and-forth should stay ONE conversation, not repeatedly fork off the first answer."""
    rid = _report(kit, session="s1")
    _settle(kit, followup.ask(kit.cfg, kit.store, rid, "first")["message_id"])
    assert fake_run[0]["resume"] == "s1"
    _settle(kit, followup.ask(kit.cfg, kit.store, rid, "second")["message_id"])
    assert fake_run[1]["resume"] == "new-sid"      # the reply's session, not the report's


def test_asking_a_different_agent_never_resumes(kit, fake_run):  # noqa: F811
    """Forking another agent's transcript would hand it a conversation written under a different
    system prompt, which reads as its own history."""
    from dataclasses import replace
    kit.cfg.agents["other"] = replace(kit.cfg.agents["healthcheck"], name="other", system="OTHER")
    rid = _report(kit, agent="healthcheck", session="s1")
    res = followup.ask(kit.cfg, kit.store, rid, "q", agent="other")
    assert res["agent"] == "other" and res["resumed"] is False
    _settle(kit, res["message_id"])
    assert fake_run[0]["resume"] is None
    assert fake_run[0]["system"] == "OTHER"        # ran as the OTHER agent
    assert "FULL REPORT" in fake_run[0]["prompt"]  # so it must be given the report


def test_question_is_visible_immediately(kit, monkeypatch):  # noqa: F811
    """The reply row is inserted 'running' so a minutes-long agent doesn't swallow the question."""
    started = {}

    def _slow(cfg, spec, prompt=None, timeout=None, resume=None):
        started["seen"] = reports.thread(kit.store, started["rid"])
        return {"stdout": "", "returncode": 0, "session": "s2", "transcript": None,
                "duration_sec": 1, "result": "done"}

    monkeypatch.setattr(agent_run, "run", _slow)
    started["rid"] = rid = _report(kit)
    res = followup.ask(kit.cfg, kit.store, rid, "visible?")
    _settle(kit, res["message_id"])
    roles = [(m["role"], m["status"]) for m in started["seen"]]
    assert roles == [("user", "done"), ("agent", "running")]


def test_failure_surfaces_in_the_thread(kit, monkeypatch):  # noqa: F811
    """A crashed follow-up must not leave the thread stuck on 'running' forever."""
    def _boom(cfg, spec, prompt=None, timeout=None, resume=None):
        raise RuntimeError("claude exploded")

    monkeypatch.setattr(agent_run, "run", _boom)
    rid = _report(kit)
    res = followup.ask(kit.cfg, kit.store, rid, "q")
    row = _settle(kit, res["message_id"])
    assert row["status"] == "error"
    assert "claude exploded" in row["text"]


def test_bad_input_is_rejected(kit, fake_run):  # noqa: F811
    rid = _report(kit)
    assert followup.ask(kit.cfg, kit.store, 9999, "q")["ok"] is False
    assert followup.ask(kit.cfg, kit.store, rid, "   ")["ok"] is False
    assert followup.ask(kit.cfg, kit.store, rid, "q", agent="nope")["ok"] is False
    assert not fake_run


def test_http_ask_and_thread(client, kit, fake_run):  # noqa: F811
    rid = _report(kit, session="s1")
    r = client.post(f'/api/kit/reports/{rid}/ask', json={"prompt": "why?"})
    assert r.status_code == 200, r.get_data(as_text=True)
    _settle(kit, r.get_json()["message_id"])
    t = client.get(f'/api/kit/reports/{rid}/thread').get_json()
    assert [m["role"] for m in t] == ["user", "agent"]
