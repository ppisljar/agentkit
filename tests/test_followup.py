"""Follow-up conversations on a report — asking the agent that wrote it a question."""

from __future__ import annotations

import json
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


def test_a_running_followup_shows_the_agent_as_busy(kit, monkeypatch):  # noqa: F811
    """A follow-up is a run of that agent, so the schedule must say the agent is running.

    It used to be invisible: the job carried no `task`, which is what jobs.running_tasks() reads,
    so the Settings page showed the agent idle while it was editing files — nothing warned anyone
    off touching the same ones.
    """
    seen = {}

    def _slow(cfg, spec, prompt=None, timeout=None, resume=None):
        seen["running"] = {t["task"]: t["running"] for t in kit.scheduler.tasks()}
        return {"stdout": "", "returncode": 0, "session": "s2", "transcript": None,
                "duration_sec": 1, "result": "done"}

    monkeypatch.setattr(agent_run, "run", _slow)
    rid = _report(kit, agent="healthcheck")
    _settle(kit, followup.ask(kit.cfg, kit.store, rid, "q")["message_id"])

    assert seen["running"]["healthcheck"] is True          # busy DURING the run
    after = {t["task"]: t["running"] for t in kit.scheduler.tasks()}
    assert after["healthcheck"] is False                    # and idle once it finishes


def test_reply_block_is_parsed_not_discarded(kit, monkeypatch):  # noqa: F811
    """The reply's json block is a real report payload, not noise: its questions and proposals
    must reach the same answer/approve loop as a scheduled run's, and its findings must survive.
    Dropping it silently loses a question the agent asked you."""
    def _reply(cfg, spec, prompt=None, timeout=None, resume=None):
        return {"stdout": "", "returncode": 0, "session": "s", "transcript": None,
                "duration_sec": 1,
                "result": 'Because the fetch was ratio-locked.\n\n'
                          '```json\n{"status":"warn","summary":"x",'
                          '"findings":[{"area":"downloads","title":"locked"}],'
                          '"questions":["contact the tracker?"],'
                          '"proposals":["drop sihq"]}\n```'}

    monkeypatch.setattr(agent_run, "run", _reply)
    rid = _report(kit)
    row = _settle(kit, followup.ask(kit.cfg, kit.store, rid, "why?")["message_id"])

    assert row["text"] == "Because the fetch was ratio-locked."     # raw fence out of the prose
    assert json.loads(row["findings"])[0]["title"] == "locked"      # findings kept on the message
    # and the question/proposal are answerable items on the report, like any other
    items = {i["kind"]: i for i in kit.store.query(
        "SELECT * FROM ck_report_items WHERE report_id=? AND status='open'", (rid,))}
    assert items["question"]["text"] == "contact the tracker?"
    assert items["proposal"]["text"] == "drop sihq"


def test_thread_findings_come_back_as_a_list(kit, monkeypatch):  # noqa: F811
    """findings is stored as a JSON string; thread() must parse it. Returning the raw string handed
    the UI something it iterated over, and the failed render took down the entire page."""
    def _reply(cfg, spec, prompt=None, timeout=None, resume=None):
        return {"stdout": "", "returncode": 0, "session": "s", "transcript": None,
                "duration_sec": 1,
                "result": 'ok\n\n```json\n{"findings":[{"area":"a","title":"t"}]}\n```'}

    monkeypatch.setattr(agent_run, "run", _reply)
    rid = _report(kit)
    _settle(kit, followup.ask(kit.cfg, kit.store, rid, "q")["message_id"])

    msgs = reports.thread(kit.store, rid)
    for m in msgs:                       # EVERY row, including the user turn that has none
        assert isinstance(m["findings"], list), f"{m['role']}: {type(m['findings'])}"
    assert msgs[1]["findings"][0]["title"] == "t"


def test_reply_items_belong_to_the_reply_not_the_report(kit, monkeypatch):  # noqa: F811
    """A question raised mid-conversation must be answerable WHERE IT WAS ASKED.

    Tagged only with the report, it was indistinguishable from one the original run raised: it
    surfaced in the page-level "needs your input" block above the report, with no visible link to
    the answer it came from, and appeared a second time in the report's own item list.
    """
    def _reply(cfg, spec, prompt=None, timeout=None, resume=None):
        return {"stdout": "", "returncode": 0, "session": "s", "transcript": None,
                "duration_sec": 1,
                "result": 'done\n\n```json\n{"status":"warn","summary":"could not finish",'
                          '"findings":[{"area":"a","title":"t"}],'
                          '"questions":["contact the tracker?"]}\n```'}

    monkeypatch.setattr(agent_run, "run", _reply)
    rid = _report(kit, agent="healthcheck")          # the report's OWN run raises one too
    reports.add_items(kit.store, rid, {"questions": ["an original question"]})
    mid = followup.ask(kit.cfg, kit.store, rid, "why?")["message_id"]
    _settle(kit, mid)

    reply = reports.thread(kit.store, rid)[1]
    assert [i["text"] for i in reply["items"]] == ["contact the tracker?"]
    assert reply["rstatus"] == "warn"                 # the verdict survives, not just the prose
    assert reply["summary"] == "could not finish"
    assert reply["findings"][0]["title"] == "t"

    # the report card shows ONLY its own — the reply renders its own, so no duplication
    own = [i["text"] for i in reports.get(kit.store, rid)["items"]]
    assert own == ["an original question"]

    # ...but it is still a real open item, so the page-level inbox still surfaces it
    assert "contact the tracker?" in [i["text"] for i in reports.open_items(kit.store)]


def test_a_deliberate_json_answer_is_kept(kit, monkeypatch):  # noqa: F811
    """Only a trailing block that PARSES is dropped — an answer that shows you JSON on purpose,
    or explains some, must survive."""
    def _reply(cfg, spec, prompt=None, timeout=None, resume=None):
        return {"stdout": "", "returncode": 0, "session": "s", "transcript": None,
                "duration_sec": 1,
                "result": 'Here is the shape:\n\n```json\n{"a": 1}\n```\n\nand that is why.'}

    monkeypatch.setattr(agent_run, "run", _reply)
    rid = _report(kit)
    row = _settle(kit, followup.ask(kit.cfg, kit.store, rid, "shape?")["message_id"])
    assert "```json" in row["text"] and "and that is why." in row["text"]


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


def test_transcript_is_recorded_on_the_report(kit):  # noqa: F811
    """The .jsonl is already captured and already served; only the link to the report was missing,
    so there was no way to get from a report to the conversation that produced it."""
    rid = reports.save(kit.store, "healthcheck", {"summary": "s"},
                       session="sid-1", transcript="20260805-101010-sid-1.jsonl")
    assert reports.get(kit.store, rid)["transcript"] == "20260805-101010-sid-1.jsonl"


def test_running_agents_appear_as_provisional_reports(kit):  # noqa: F811
    """An agent working for ten minutes was invisible on the page that exists to show what agents
    are doing — a report only exists once the run FINISHES."""
    from claudekit import jobs
    jid = jobs.create(kit.store, "agent", "agent:healthcheck", {"task": "healthcheck"})

    live = reports.running_runs(kit.store)
    assert [r["agent"] for r in live] == ["healthcheck"]
    assert live[0]["running"] is True and live[0]["job"] == jid

    jobs.update(kit.store, jid, status="done")
    assert reports.running_runs(kit.store) == []


def test_an_orphaned_run_is_not_advertised_as_live(kit):  # noqa: F811
    """A job left 'running' by a crashed process must not sit on the reports page forever."""
    from claudekit import jobs
    jid = jobs.create(kit.store, "agent", "agent:healthcheck", {"task": "healthcheck"})
    kit.store.execute("UPDATE ck_jobs SET updated=? WHERE id=?",
                      (time.time() - jobs.STALE_SEC - 60, jid))
    assert reports.running_runs(kit.store) == []
