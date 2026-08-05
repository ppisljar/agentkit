"""The report / approval loop — how an agent talks to the human.

A scheduled agent (health check, improvements) ends its run with one fenced ```json block:

    {"status": "ok|warn|fail",
     "summary": "short paragraph a human can skim",
     "findings": [{"area": "...", "severity": "info|warn|fail", "title": "...",
                   "detail": "...", "action": "none|fixed"}],
     "questions": ["something it needs decided"],
     "proposals": ["a risky change it did NOT make but suggests"]}

Questions and proposals become rows the human answers or approves in the UI. A decision is then
carried out by the agent that RAISED it (see decisions_handler) — it wrote the finding and knows
the domain — acting *only* on the approved items. The approval step is the safety boundary: an
agent may investigate freely but cannot act on its own conclusions until a human says so.

Agents that cannot do this — script-based ones, whose script owns the run — fall back to the
generic `applydecisions` agent, as does anything that names it explicitly.
"""

from __future__ import annotations

import json
import time

from .store import Store

KINDS = ("question", "proposal")


def save(store: Store, agent: str, payload: dict | None, *, raw: str = "",
         duration_sec: int = 0, ok: bool = True,
         status: str | None = None, summary: str | None = None,
         detail: str | None = None, findings: list | None = None,
         session: str | None = None, transcript: str | None = None) -> int:
    """Persist one agent run. `payload` is the parsed json block (None if the agent produced none).

    The explicit keywords override whatever the payload says. That matters for wrapper-script
    agents (`AgentSpec.script`), which often know the truth better than the model does — e.g. one
    that diffs the database before and after can report exactly what its run changed, and mark a
    run that returned no structured block as 'warn' rather than letting it pass as clean.
    """
    payload = payload or {}
    status = str(status if status is not None
                 else (payload.get("status") or ("ok" if ok else "fail")))
    summary = str(summary if summary is not None
                  else (payload.get("summary") or payload.get("report_summary") or ""))
    findings = findings if findings is not None else (payload.get("findings") or [])
    raw = detail if detail is not None else raw

    cur = store.execute(
        """INSERT INTO ck_reports (created, agent, status, summary, detail, findings,
                                   duration_sec, ok, session, transcript)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (time.time(), agent, status, summary, raw[:200_000],
         json.dumps(findings), int(duration_sec), 1 if ok else 0, session, transcript),
    )
    rid = int(cur.lastrowid)

    add_items(store, rid, payload)
    return rid


def add_items(store: Store, rid: int, payload: dict | None,
              message_id: int | None = None) -> int:
    """Turn a payload's questions/proposals into open items on report `rid`. Returns how many.

    Shared with the follow-up path: an agent answering a question still ends with its json block,
    and that block can raise NEW questions and proposals. Those are as real as the ones a scheduled
    run raises and must reach the same answer/approve loop — dropping them would silently lose a
    question the agent asked you.

    `message_id` records WHICH reply raised it. Without that link an item raised mid-conversation
    is indistinguishable from one the original run raised, so it can only be rendered detached from
    the exchange that produced it — which is exactly how it went wrong: a question asked in reply to
    a follow-up surfaced in the page-level inbox, above the report, with no visible connection to
    the answer it came from.
    """
    now = time.time()
    n = 0
    for kind, key in (("question", "questions"), ("proposal", "proposals")):
        for entry in ((payload or {}).get(key) or []):
            text = entry if isinstance(entry, str) else json.dumps(entry)
            if text and text.strip():
                store.execute(
                    """INSERT INTO ck_report_items (report_id, created, kind, text, status,
                                                    message_id)
                       VALUES (?,?,?,?,'open',?)""",
                    (rid, now, kind, text.strip(), message_id))
                n += 1
    return n


def _report_row(r) -> dict:
    d = dict(r)
    if d.get("findings"):
        try:
            d["findings"] = json.loads(d["findings"])
        except Exception:  # noqa: BLE001
            d["findings"] = []
    d.pop("detail", None)          # the full transcript is big; fetched only via get()
    return d


def recent(store: Store, limit: int = 30, agent: str | None = None) -> list[dict]:
    if agent:
        rows = store.query(
            "SELECT * FROM ck_reports WHERE agent=? ORDER BY created DESC LIMIT ?", (agent, limit))
    else:
        rows = store.query("SELECT * FROM ck_reports ORDER BY created DESC LIMIT ?", (limit,))
    out = []
    for r in rows:
        d = _report_row(r)
        d["open_items"] = _count_open(store, d["id"])
        out.append(d)
    return out


def _count_open(store: Store, rid: int) -> int:
    r = store.one("SELECT COUNT(*) c FROM ck_report_items WHERE report_id=? AND status='open'",
                  (rid,))
    return int(r["c"]) if r else 0


def get(store: Store, rid: int) -> dict | None:
    r = store.one("SELECT * FROM ck_reports WHERE id=?", (rid,))
    if not r:
        return None
    d = dict(r)
    if d.get("findings"):
        try:
            d["findings"] = json.loads(d["findings"])
        except Exception:  # noqa: BLE001
            d["findings"] = []
    # Only the items this report's OWN run raised. Ones a follow-up reply raised are rendered by
    # the thread, next to the answer that raised them; listing them here too showed the same
    # question twice in one card, detached from its context in one of the two places.
    d["items"] = report_items(store, rid, own_only=True)
    # recent() carries this, so the detail view lacking it was an asymmetry a consumer would trip
    # on — "the list told me 2 open, the report itself doesn't say".
    d["open_items"] = _count_open(store, rid)
    return d


def items(store: Store, rid: int | None = None, status: str | None = None) -> list[dict]:
    sql, params = "SELECT * FROM ck_report_items WHERE 1=1", []
    if rid is not None:
        sql += " AND report_id=?"; params.append(rid)
    if status:
        sql += " AND status=?"; params.append(status)
    sql += " ORDER BY id"
    return [dict(r) for r in store.query(sql, tuple(params))]


def running_runs(store: Store) -> list[dict]:
    """Agent runs happening RIGHT NOW, shaped like provisional reports.

    A report only exists once a run finishes, so an agent working for ten minutes was invisible on
    the page that exists to show what agents are doing — you had to go to Settings to find out
    anything was happening at all. These are rendered at the top of the list and replaced by the
    real report when it lands.

    Read from the job rows rather than any in-memory state, so it is true across the web/daemon
    process split, and filtered through jobs._row so a run orphaned by a crash stops being
    advertised as live.
    """
    from . import jobs
    out = []
    for j in jobs.recent(store, "agent", 25):
        if j.get("status") != "running":
            continue
        agent = (j.get("params") or {}).get("task")
        if not agent:
            continue
        out.append({"agent": agent, "started": j.get("created"), "job": j.get("id"),
                    "label": j.get("label"), "running": True})
    out.sort(key=lambda r: r["started"] or 0, reverse=True)
    return out


def open_items(store: Store) -> list[dict]:
    """Everything still waiting on the human, newest report first."""
    rows = store.query(
        """SELECT i.*, r.agent, r.created AS report_created
           FROM ck_report_items i JOIN ck_reports r ON r.id = i.report_id
           WHERE i.status='open' ORDER BY r.created DESC, i.id""")
    return [dict(r) for r in rows]


def answer(store: Store, item_id: int, text: str) -> dict | None:
    """Answer a question (or attach a note to a proposal)."""
    store.execute(
        "UPDATE ck_report_items SET status='answered', answer=?, answered_at=? WHERE id=?",
        (text, time.time(), item_id))
    return item(store, item_id)


def decide(store: Store, item_id: int, approved: bool, note: str = "") -> dict | None:
    store.execute(
        "UPDATE ck_report_items SET status=?, answer=?, answered_at=? WHERE id=?",
        ("approved" if approved else "rejected", note, time.time(), item_id))
    return item(store, item_id)


def thread(store: Store, report_id: int) -> list[dict]:
    """The follow-up conversation on a report, oldest first.

    `findings` is stored as a JSON string and MUST be parsed here, the way _report_row does for a
    report's own. Returning the raw string handed the UI something it iterated over — one crashed
    render took down the whole page.
    """
    by_msg: dict[int, list[dict]] = {}
    for it in store.query("SELECT * FROM ck_report_items WHERE report_id=? AND "
                          "message_id IS NOT NULL ORDER BY id", (report_id,)):
        by_msg.setdefault(int(it["message_id"]), []).append(dict(it))

    out = []
    for r in store.query("SELECT * FROM ck_report_messages WHERE report_id=? ORDER BY id",
                         (report_id,)):
        d = dict(r)
        if d.get("findings"):
            try:
                d["findings"] = json.loads(d["findings"])
            except Exception:  # noqa: BLE001
                d["findings"] = []
        if not isinstance(d.get("findings"), list):
            d["findings"] = []
        # the questions/proposals THIS reply raised, so they can be answered where they were asked
        d["items"] = by_msg.get(int(d["id"]), [])
        out.append(d)
    return out


def report_items(store: Store, report_id: int, own_only: bool = False) -> list[dict]:
    """Items on a report. `own_only` excludes ones raised by a follow-up reply, which the thread
    renders itself — otherwise the same question appears twice in one report card."""
    sql = "SELECT * FROM ck_report_items WHERE report_id=?"
    if own_only:
        sql += " AND message_id IS NULL"
    return [dict(r) for r in store.query(sql + " ORDER BY id", (report_id,))]


def add_message(store: Store, report_id: int, role: str, text: str, *, agent: str | None = None,
                session: str | None = None, job_id: int | None = None,
                status: str = "done") -> int:
    cur = store.execute(
        """INSERT INTO ck_report_messages (report_id, created, role, text, agent, session,
                                           job_id, status)
           VALUES (?,?,?,?,?,?,?,?)""",
        (int(report_id), time.time(), role, text, agent, session, job_id, status))
    return int(cur.lastrowid)


def finish_message(store: Store, msg_id: int, text: str, *, session: str | None = None,
                   status: str = "done", findings: list | None = None,
                   rstatus: str | None = None, summary: str | None = None) -> None:
    """Fill in an agent reply once its run returns (it is inserted 'running' so the thread shows
    the question immediately rather than swallowing it for the minutes the agent takes)."""
    store.execute(
        "UPDATE ck_report_messages SET text=?, session=COALESCE(?, session), status=?, "
        "findings=?, rstatus=?, summary=? WHERE id=?",
        (text, session, status, json.dumps(findings) if findings else None,
         rstatus, summary, int(msg_id)))


def last_session(store: Store, report_id: int) -> str | None:
    """The session to resume for the NEXT follow-up: the most recent reply's, else the report's own.

    Threading onto the latest reply rather than always the original run means a back-and-forth
    stays one conversation instead of repeatedly branching off the first answer.
    """
    r = store.one("SELECT session FROM ck_report_messages WHERE report_id=? AND role='agent' "
                  "AND session IS NOT NULL ORDER BY id DESC LIMIT 1", (int(report_id),))
    if r and r["session"]:
        return r["session"]
    r = store.one("SELECT session FROM ck_reports WHERE id=?", (int(report_id),))
    return r["session"] if r else None


def item(store: Store, item_id: int) -> dict | None:
    r = store.one("SELECT * FROM ck_report_items WHERE id=?", (item_id,))
    return dict(r) if r else None


def item_with_agent(store: Store, item_id: int) -> dict | None:
    """`item()` plus `agent` — which agent raised it, needed to route the decision back to it."""
    r = store.one("SELECT i.*, r.agent AS agent FROM ck_report_items i "
                  "LEFT JOIN ck_reports r ON r.id = i.report_id WHERE i.id=?", (item_id,))
    return dict(r) if r else None


def actionable(store: Store, agent: str | None = None) -> list[dict]:
    """Approved proposals and answered questions nobody has carried out yet.

    Each row carries `agent` — which agent RAISED it — so decisions can go back to the agent that
    understands them rather than all landing on the generic apply agent (see route_decisions).
    Pass `agent` to get only that one's items.
    """
    sql = ("SELECT i.*, r.agent AS agent FROM ck_report_items i "
           "LEFT JOIN ck_reports r ON r.id = i.report_id "
           "WHERE i.status IN ('approved','answered')")
    params: tuple = ()
    if agent is not None:
        sql += " AND r.agent = ?"
        params = (agent,)
    return [dict(r) for r in store.query(sql + " ORDER BY i.id", params)]


APPLY_AGENT = "applydecisions"


def decisions_handler(cfg, agent: str | None) -> str:
    """Which task carries out a decision on an item `agent` raised.

    BY DEFAULT, the agent that raised it. It wrote the finding, it knows the domain, and it is
    usually the thing that has to change — handing "your download resolver picked the wrong
    release" to a general-purpose apply agent means that agent must rediscover the whole pipeline
    before it can act safely, and it answers as a stranger to its own report.

    Two escapes:
      * `AgentSpec.decisions_task` names something else explicitly — a dedicated maintenance
        variant, or "applydecisions" to opt out entirely.
      * A SCRIPT-based agent cannot handle its own decisions unless it says so. Its script owns the
        whole run and ignores the prompt we would hand it, so approving something would silently
        re-run the script's normal job (queueing downloads, crawling) instead of applying anything.
        Those fall back to the apply agent unless they declare a decisions_task.
    """
    spec = cfg.agents.get(agent or "")
    if not spec:
        return APPLY_AGENT
    if spec.decisions_task:
        return spec.decisions_task
    if spec.script and not spec.decisions_argv:
        # Its script owns the whole run and would ignore any prompt we hand it, so without a
        # decisions mode of its own an "apply" would just re-run its normal job.
        return APPLY_AGENT
    return spec.name


def route_decisions(cfg, store) -> dict[str, list[dict]]:
    """{task -> items it should carry out} for everything currently awaiting action."""
    out: dict[str, list[dict]] = {}
    for it in actionable(store):
        out.setdefault(decisions_handler(cfg, it.get("agent")), []).append(it)
    return out


def mark_done(store: Store, item_ids: list[int]) -> None:
    for i in item_ids:
        store.execute("UPDATE ck_report_items SET status='done' WHERE id=?", (int(i),))


def build_apply_prompt(store: Store, footer: str, items: list[dict] | None = None) -> tuple[str, list[int]]:
    """Render the approved/answered items into a prompt for the apply agent.

    Returns (prompt, ids). An empty id list means there is nothing to do — callers should skip
    running the agent entirely rather than invoking it with no work. Pass `items` to render only
    a routed subset (see route_decisions) instead of everything outstanding.
    """
    pend = actionable(store) if items is None else items
    if not pend:
        return "", []
    lines = ["The owner has reviewed the latest report(s). Carry out ONLY the items below.", ""]
    for it in pend:
        verb = "APPROVED PROPOSAL" if it["kind"] == "proposal" else "ANSWERED QUESTION"
        lines.append(f"[item_id={it['id']}] {verb}: {it['text']}")
        if it.get("answer"):
            lines.append(f"    owner's note/answer: {it['answer']}")
        lines.append("")
    lines.append(footer)
    return "\n".join(lines), [int(it["id"]) for it in pend]
