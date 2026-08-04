"""Ask an agent about a report it wrote.

A report used to be the end of the exchange: the agent said its piece and the only way to ask
"why?" was to go and run the agent by hand, with the report pasted in and the connection to it
lost. This turns a report into the start of a conversation — you ask, the same agent answers, and
the whole thread stays attached to the report that prompted it.

Two things the caller chooses:

* WHICH agent. Defaults to the one that wrote the report, but any declared agent can be asked —
  useful when the answer belongs to a different specialism than the finding.
* WHETHER TO CONTINUE its session. Continuing forks the original conversation (see
  agent_run.run), so the agent still remembers what it did and why. Starting fresh is the honest
  choice when the question is really a new task, or when the old session is long gone — and in
  that case the full report is prepended to the prompt so the agent is never asked about
  something it cannot see.

Follow-ups run with the agent's NORMAL powers, not read-only: a question like "why did this fail"
usually wants the fix, not a description of one. The reply records what it did, so a run that
edited files says so.
"""

from __future__ import annotations

import json
import re

from . import agent_run, jobs, registry, reports
from .store import Store

# A resumed run already has the report in its context; a fresh one does not, so it gets the whole
# thing. Capped because `detail` holds the agent's entire final message and can be very large.
_MAX_DETAIL = 24_000


def report_as_text(rep: dict) -> str:
    """The report rendered for an agent that was NOT part of the run that produced it."""
    lines = [f"REPORT #{rep.get('id')} — agent '{rep.get('agent')}', status '{rep.get('status')}'",
             "", f"SUMMARY: {rep.get('summary') or '(none)'}"]
    findings = rep.get("findings")
    if isinstance(findings, str):
        try:
            findings = json.loads(findings)
        except Exception:  # noqa: BLE001
            findings = [findings]
    if findings:
        lines += ["", "FINDINGS:"] + [f"  - {f}" for f in findings]
    detail = (rep.get("detail") or "")[:_MAX_DETAIL]
    if detail:
        lines += ["", "FULL REPORT:", detail]
    return "\n".join(lines)


_TRAILING_JSON = re.compile(r"\n*```json\s*(\{.*?\})\s*```\s*$", re.S)


def strip_report_block(text: str) -> str:
    """Drop the trailing ```json report block from a conversational reply.

    Scheduled agents are required to end every run with one fenced json block, and they keep doing
    it when answering a question — so a two-paragraph answer arrived with a wall of escaped JSON
    stapled to the end, rendered verbatim in the thread. That block is the REPORT contract; in a
    conversation it is noise, and the same content is already in the prose above it.

    Only a block at the very END is removed, and only if it actually parses — so an answer that
    deliberately shows you some JSON keeps it.
    """
    m = _TRAILING_JSON.search(text or "")
    if not m:
        return text
    try:
        json.loads(m.group(1))
    except Exception:  # noqa: BLE001 — not a report block, leave the reply alone
        return text
    return (text[:m.start()]).rstrip() or text


def build_prompt(rep: dict, question: str, *, resuming: bool) -> str:
    if resuming:
        return ("A follow-up question about the report you just filed (you are resuming that same "
                f"conversation):\n\n{question}")
    return (f"{report_as_text(rep)}\n\n"
            "---\n"
            "The owner has a question about the report above. You did not necessarily write it, "
            "and you are NOT resuming that run — everything you know about it is what is quoted "
            f"here.\n\nQUESTION:\n{question}")


def ask(cfg, store: Store, report_id: int, question: str, *, agent: str | None = None,
        resume: bool = True, get_report=None) -> dict:
    """Start a follow-up run. Returns {ok, message_id, job, agent, resumed} immediately.

    The reply row is inserted 'running' before the agent starts so the thread shows the question
    the moment it is asked, rather than swallowing it for the minutes an agent takes.
    """
    rep = (get_report or reports.get)(store, report_id)
    if not rep:
        return {"ok": False, "error": f"no such report: {report_id}"}
    question = (question or "").strip()
    if not question:
        return {"ok": False, "error": "empty question"}

    name = agent or rep.get("agent")
    if not name or name not in cfg.agents:
        return {"ok": False, "error": f"unknown agent: {name}"}
    spec = registry.resolve(cfg, store, name)

    # Only resume a session that (a) was asked for, (b) exists, and (c) belongs to the SAME agent —
    # forking another agent's conversation would hand it a transcript written under a different
    # system prompt, which reads as that agent's own history and is worse than starting clean.
    sid = reports.last_session(store, report_id) if resume else None
    if sid and name != rep.get("agent"):
        sid = None
    prompt = build_prompt(rep, question, resuming=bool(sid))

    reports.add_message(store, report_id, "user", question, agent=name)
    msg_id = reports.add_message(store, report_id, "agent", "", agent=name, status="running")

    def _work(job_id: int, _params: dict) -> None:
        try:
            res = agent_run.run(cfg, spec, prompt=prompt, resume=sid)
            ok = res["returncode"] == 0
            reports.finish_message(store, msg_id,
                                   strip_report_block(res["result"]) or "(no output)",
                                   session=res["session"], status="done" if ok else "error")
            jobs.update(store, job_id, status="done" if ok else "error",
                        result={"message": msg_id, "session": res["session"]},
                        log=(res["result"] or "")[-20000:])
        except Exception as e:  # noqa: BLE001 — the thread must show the failure, not hang on 'running'
            reports.finish_message(store, msg_id, f"Follow-up failed: {e}", status="error")
            jobs.update(store, job_id, status="error", error=str(e))
            raise

    # `task` is what jobs.running_tasks() reads to decide which scheduler task is busy, and it is
    # how the UI knows an agent is working. Without it a follow-up ran completely invisibly: the
    # agent showed idle on the Settings page while it was editing files, so nothing warned you off
    # touching the same ones.
    job = jobs.run_bg(store, "agent", f"followup:{name}#{report_id}",
                      {"task": name, "report": report_id, "message": msg_id}, _work)
    store.execute("UPDATE ck_report_messages SET job_id=? WHERE id=?", (job, msg_id))
    return {"ok": True, "message_id": msg_id, "job": job, "agent": name, "resumed": bool(sid)}
