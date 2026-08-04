"""The report / approval loop — how an agent talks to the human.

A scheduled agent (health check, improvements) ends its run with one fenced ```json block:

    {"status": "ok|warn|fail",
     "summary": "short paragraph a human can skim",
     "findings": [{"area": "...", "severity": "info|warn|fail", "title": "...",
                   "detail": "...", "action": "none|fixed"}],
     "questions": ["something it needs decided"],
     "proposals": ["a risky change it did NOT make but suggests"]}

Questions and proposals become rows the human answers or approves in the UI. Approved items are
then handed to the `applydecisions` agent, which may act *only* on them. That split is the whole
safety model: the investigating agent is read-only and cannot act on its own conclusions.
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
         session: str | None = None) -> int:
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
                                   duration_sec, ok, session)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (time.time(), agent, status, summary, raw[:200_000],
         json.dumps(findings), int(duration_sec), 1 if ok else 0, session),
    )
    rid = int(cur.lastrowid)

    now = time.time()
    for kind, key in (("question", "questions"), ("proposal", "proposals")):
        for entry in (payload.get(key) or []):
            text = entry if isinstance(entry, str) else json.dumps(entry)
            if text and text.strip():
                store.execute(
                    """INSERT INTO ck_report_items (report_id, created, kind, text, status)
                       VALUES (?,?,?,?,'open')""",
                    (rid, now, kind, text.strip()))
    return rid


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
    d["items"] = items(store, rid)
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
    """The follow-up conversation on a report, oldest first."""
    return [dict(r) for r in store.query(
        "SELECT * FROM ck_report_messages WHERE report_id=? ORDER BY id", (report_id,))]


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
                   status: str = "done") -> None:
    """Fill in an agent reply once its run returns (it is inserted 'running' so the thread shows
    the question immediately rather than swallowing it for the minutes the agent takes)."""
    store.execute(
        "UPDATE ck_report_messages SET text=?, session=COALESCE(?, session), status=? WHERE id=?",
        (text, session, status, int(msg_id)))


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


def route_decisions(cfg, store) -> dict[str, list[dict]]:
    """{task -> items it should carry out} for everything currently awaiting action.

    An agent that declares `decisions_task` handles its own items there; everything else falls to
    'applydecisions'. Without this every decision went to the apply agent regardless of who raised
    it, so a finding about a download pipeline was handed to an agent with none of that context.
    """
    out: dict[str, list[dict]] = {}
    for it in actionable(store):
        spec = cfg.agents.get(it.get("agent") or "")
        task = (spec.decisions_task if spec and spec.decisions_task else "applydecisions")
        out.setdefault(task, []).append(it)
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
