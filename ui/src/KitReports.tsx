/**
 * Reports — where agents talk to you.
 *
 * An agent investigates read-only and records what it found, what it needs decided (questions)
 * and what it would like to do but didn't (proposals). You answer and approve here; "Apply
 * approved" then hands *only* those items to the apply agent.
 *
 * The same component serves one app or a whole fleet. Point `base` at an app's /api/kit and it
 * behaves exactly as it always has; point it at a `claudekit.fleet` API and pass `projects`, and
 * it grows a project filter along the top and labels every row with the project it came from.
 * Everything fleet-related is opt-in: with no `projects` prop nothing about the page changes.
 */

import React from 'react'
import { ApplyResult, Finding, Job, KitApi, Report, ReportItem, RowId, RunningRun, ThreadMessage, fmtAgo } from './api'
import { Badge, Button, Card, Empty, ErrorNote, LogBox, Markdown, Spinner, cx } from './ui'

/** A project the reports may belong to. `key` is what the API filters on. */
export type ProjectTag = { key: string; label: string; color?: string; url?: string }

export function KitReports({ base = '/api/kit', agentLabels, projects, title, subtitle,
                            onRunNow, runNowLabel, inlineDetail }: {
  base?: string
  /** Optional agent-id -> display name, so rows read "Daily health check" rather than "selfcheck".
   *  Left to the host because the kit doesn't know which of an app's agents deserve friendlier
   *  names, and the ids are what the API speaks. */
  agentLabels?: Record<string, string>
  /** Fleet mode: the projects behind this API. Filtering happens server-side (?project=), so the
   *  newest-N limit stays correct instead of thinning out whichever project is chattiest. */
  projects?: ProjectTag[]
  title?: React.ReactNode
  subtitle?: React.ReactNode
  /** Kick off a run from this page. The kit has no opinion about WHICH agent a host considers
   *  "the" report producer — HomeFlix means selfcheck, another app might mean something else — so
   *  the host supplies the action and the label, and the button simply isn't rendered without it. */
  onRunNow?: () => Promise<unknown> | void
  runNowLabel?: string
  /** Expand a report IN PLACE instead of replacing the list with a detail page. An accordion keeps
   *  the other reports visible while you read one, which matters when you are comparing runs or
   *  working down a list; the page view suits a fleet, where a report can be long and belongs to
   *  one project at a time. Hosts choose. */
  inlineDetail?: boolean
}) {
  const api = React.useMemo(() => new KitApi(base), [base])
  const [reports, setReports] = React.useState<Report[]>([])
  const [open, setOpen] = React.useState<Report | null>(null)
  const [pending, setPending] = React.useState<ReportItem[]>([])
  const [liveRuns, setLiveRuns] = React.useState<RunningRun[]>([])
  const [err, setErr] = React.useState<string | null>(null)
  const [notice, setNotice] = React.useState<string | null>(null)
  const [applyJob, setApplyJob] = React.useState<number | null>(null)
  const [applying, setApplying] = React.useState(false)
  const [job, setJob] = React.useState<Job | null>(null)
  const [project, setProject] = React.useState<string | null>(null)
  const byKey = React.useMemo(
    () => Object.fromEntries((projects || []).map((p) => [p.key, p])), [projects])

  const load = React.useCallback(async () => {
    try {
      const [r, p, live] = await Promise.all([
        api.reports(30, project), api.openItems(project),
        api.runningRuns().catch(() => [] as RunningRun[]),   // a fleet api may not offer it
      ])
      setReports(r)
      setPending(p)
      setLiveRuns(live)
    } catch (e: any) { setErr(e.message) }
  }, [api, project])

  React.useEffect(() => { load() }, [load])
  // Only poll while an agent is actually working — a run takes minutes, and an idle page has
  // nothing to refresh for.
  React.useEffect(() => {
    if (!liveRuns.length) return
    const t = setInterval(load, 5000)
    return () => clearInterval(t)
  }, [liveRuns.length, load])

  // poll the apply agent while it works
  React.useEffect(() => {
    if (!applyJob) return
    let alive = true
    let timer: any
    const tick = async () => {
      try {
        const j = await api.job(applyJob)
        if (!alive) return
        setJob(j)
        if (j.status === 'running') timer = setTimeout(tick, 2000)
        else { load(); if (open) api.report(open.id).then(setOpen).catch(() => {}) }
      } catch { if (alive) timer = setTimeout(tick, 4000) }
    }
    tick()
    return () => { alive = false; clearTimeout(timer) }
  }, [api, applyJob, load])

  const refreshOpen = async (id: RowId) => {
    try { setOpen(await api.report(id)) } catch (e: any) { setErr(e.message) }
    load()
  }

  const actionable = pending.length

  const [running, setRunning] = React.useState(false)
  const runNow = async () => {
    if (!onRunNow) return
    setRunning(true)
    try {
      await onRunNow()
      // An agent takes minutes; reload so its "running" state and eventual report appear without
      // the reader having to guess when to refresh.
      setTimeout(load, 2000)
    } catch (e: any) {
      setErr(e.message)
    } finally {
      setRunning(false)
    }
  }

  const apply = async () => {
    setApplying(true)
    setNotice(null)
    try {
      const r: ApplyResult = await api.applyDecisions(project)
      if (r.skipped) setErr('Nothing is approved or answered yet — decide on an item first.')
      else if (r.job) setApplyJob(r.job)
      // A fleet host cannot run another project's agent; it leaves each project a run request and
      // says so. Without this the button would look like it had done nothing at all.
      else if (r.note) setNotice(r.note)
      if (r.failed?.length) setErr(r.failed.map((f) => `${f.project}: ${f.error}`).join('\n'))
    } catch (e: any) { setErr(e.message) }
    finally { setApplying(false) }
  }

  return (
    <div className="mx-auto max-w-5xl px-4 py-6">
      <div className="mb-5 flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-2xl font-bold text-gray-900">{title || 'Reports'}</h1>
          <p className="mt-1 text-sm text-gray-500">
            {subtitle || 'What the agents found, and anything waiting on you.'}
          </p>
        </div>
        <div className="flex items-center gap-2">
          {actionable > 0 && <Badge tone="open">{actionable} needs your input</Badge>}
          {onRunNow && (
            <Button onClick={runNow} loading={running}>{runNowLabel || 'Run now'}</Button>
          )}
          <Button variant="primary" onClick={apply} loading={applying}>Apply approved</Button>
        </div>
      </div>

      {projects && projects.length > 0 && (
        <ProjectFilter projects={projects} value={project}
          onChange={(k) => { setProject(k); setOpen(null) }} />
      )}

      <ErrorNote error={err} onDismiss={() => setErr(null)} />
      <Notice text={notice} onDismiss={() => setNotice(null)} />

      {job && (
        <Card className="mb-4" title="Apply agent"
          right={<Badge tone={job.status}>{job.status}</Badge>}>
          {job.status === 'running' && (
            <div className="mb-2 flex items-center gap-2 text-sm text-gray-600">
              <Spinner /> carrying out the approved items — this can take a few minutes
            </div>
          )}
          {job.result?.report && (
            <p className="mb-2 text-sm text-green-700">Done — wrote report #{job.result.report}.</p>
          )}
          <LogBox text={job.log} />
        </Card>
      )}

      {open && !inlineDetail ? (
        <ReportDetail api={api} base={base} report={open} agentLabels={agentLabels} project={byKey[open.project!]}
          onBack={() => { setOpen(null); load() }}
          onChanged={() => refreshOpen(open.id)} onError={setErr} />
      ) : (
        <>
          {pending.length > 0 && (
            <Card className="mb-5" tone="attention" title={`Needs your input (${pending.length})`}
              subtitle="Answer a question or approve a proposal, then press “Apply approved”.">
              <div className="space-y-3">
                {pending.map((it) => (
                  <ItemRow key={it.id} api={api} item={it} project={byKey[it.project!]}
                    onChanged={load} onError={setErr} />
                ))}
              </div>
            </Card>
          )}

          {/* Runs in flight. A report only exists once a run FINISHES, so without these the page
              that exists to show what agents are doing said nothing at all for the ten minutes one
              was working. Replaced by the real report when it lands. */}
          {liveRuns.length > 0 && (
            <div className="mb-2 space-y-2">
              {liveRuns.map((r) => (
                <div key={r.job}
                  className="flex items-center gap-2.5 rounded-xl border border-blue-400/40 bg-blue-400/10 px-3.5 py-2.5">
                  <Spinner />
                  <span className="shrink-0 text-[13px] text-gray-500">
                    {agentLabels?.[r.agent] || r.agent}
                  </span>
                  <span className="min-w-0 flex-1 truncate text-[13.5px] font-semibold text-gray-900">
                    running…
                  </span>
                  <span className="shrink-0 whitespace-nowrap text-[11.5px] text-gray-400">
                    started {fmtAgo(r.started)}
                  </span>
                </div>
              ))}
            </div>
          )}

          {reports.length === 0 && liveRuns.length === 0 && (
            <Empty>No reports yet. Run an agent from Settings → Agents.</Empty>
          )}

          <div className="space-y-2">
            {reports.map((r) => {
            const expanded = !!inlineDetail && open?.id === r.id
            return (
              <div key={r.id} className="overflow-hidden rounded-xl border border-gray-200 bg-white shadow-sm transition hover:border-gray-300">
              {/* ONE LINE. A run list is scanned, not read: status, who, what, how long ago —
                  with the summary truncated rather than wrapped, so ten runs fit on a screen
                  instead of three. Everything else lives in the expanded body. */}
              <button onClick={() => (expanded ? setOpen(null) : refreshOpen(r.id))}
                className="flex w-full items-center gap-2.5 px-3.5 py-2.5 text-left hover:bg-gray-50">
                <ProjectPill project={byKey[r.project!]} />
                <span className="shrink-0 text-[15px]" title={r.status}>{RUN_ICON[r.status] || '•'}</span>
                <span className="shrink-0 text-[13px] text-gray-500">
                  {agentLabels?.[r.agent] || r.agent}
                </span>
                <span className="min-w-0 flex-1 truncate text-[13.5px] font-semibold text-gray-900">
                  {r.summary || '(no summary)'}
                </span>
                {!!r.open_items && (
                  <span className="shrink-0 rounded-md bg-amber-100 px-1.5 py-0.5 text-[11px] font-bold text-amber-800">
                    {r.open_items} open
                  </span>
                )}
                <span className="shrink-0 whitespace-nowrap text-[11.5px] text-gray-400">
                  {fmtAgo(r.created)}
                </span>
                <span className="shrink-0 text-gray-400">{expanded ? '▾' : '▸'}</span>
              </button>
              {expanded && open && (
                <div className="border-t border-gray-200 px-4 pb-3.5 pt-1">
                  <ReportDetail api={api} base={base} report={open} agentLabels={agentLabels}
                    project={byKey[open.project!]} inline
                    onBack={() => setOpen(null)}
                    onChanged={() => refreshOpen(open.id)} onError={setErr} />
                </div>
              )}
              </div>
            )})}
          </div>
        </>
      )}
    </div>
  )
}

// Severity icons. Findings arrive in TWO shapes — a host may store them pre-rendered as strings
// with a "[warn] area: title" prefix, or as dicts with a `severity` field — so both are handled or
// half of them keep their bracketed tag. pass/done/skipped are included because agents emit them.
const SEV_ICON: Record<string, string> = {
  info: 'ℹ️', warn: '⚠️', fail: '❌', pass: '✅', done: '✅', skipped: '⏭️',
}
const SEV_RE = /^\[([a-z]+)\]\s*/i
// How a RUN ended, as shown on each report row.
const RUN_ICON: Record<string, string> = { ok: '✅', warn: '⚠️', fail: '❌', error: '❌' }

/** A finding as one line, for the compact list under a follow-up reply. The full card treatment
 *  is reserved for a report's own findings, above. The icon keeps its word as a tooltip: an icon
 *  alone is not readable by a screen reader, and severity is the part you cannot afford to lose. */
function FindingLine({ f }: { f: Finding | string }) {
  let sev: string | undefined
  let text: string
  if (typeof f === 'string') {
    const m = SEV_RE.exec(f)
    sev = m?.[1]?.toLowerCase()
    // an unrecognised tag stays visible rather than being silently eaten
    text = sev && SEV_ICON[sev] ? f.slice(m![0].length) : f
    if (sev && !SEV_ICON[sev]) sev = undefined
  } else {
    sev = (f.severity || '').toLowerCase() || undefined
    text = [[f.area && `${f.area}:`, f.title].filter(Boolean).join(' '), f.detail]
      .filter(Boolean).join(' — ')
    if (f.action === 'fixed') text += ' (fixed)'
  }
  return (
    <>
      {sev && SEV_ICON[sev] && <span className="mr-1.5" title={sev}>{SEV_ICON[sev]}</span>}
      {text}
    </>
  )
}

function ReportDetail({ api, base, report, onBack, onChanged, onError, agentLabels, project, inline }: {
  api: KitApi; base: string; report: Report; onBack: () => void; onChanged: () => void
  onError: (e: string) => void
  agentLabels?: Record<string, string>
  project?: ProjectTag
  /** Rendered inside its own row (accordion) rather than as a page: the row above already gives
   *  the way back, so a "← All reports" link would be a dead control. */
  inline?: boolean
}) {
  // In fleet mode the id is namespaced ("homeflix:23"); show the project as a pill and the
  // number the project itself would show, rather than repeating the key inside the heading.
  const shown = report.local_id ?? report.id
  return (
    <div className="space-y-4">
      {!inline && (
        <button onClick={onBack} className="text-sm text-blue-600 hover:underline">← All reports</button>
      )}

      {!inline && (
        <Card title={`${agentLabels?.[report.agent] || report.agent} · report #${shown}`}
          subtitle={`${fmtAgo(report.created)} · ${report.duration_sec}s`}
          right={<div className="flex items-center gap-2">
            <ProjectPill project={project} />
            <Badge tone={report.status}>{report.status}</Badge>
          </div>}>
          {report.summary && <p className="text-sm text-gray-700">{report.summary}</p>}
          {project?.url && (
            <p className="mt-2 text-xs">
              <a href={project.url} className="text-blue-600 hover:underline">
                open {project.label} ↗
              </a>
            </p>
          )}
        </Card>
      )}

      {(report.findings || []).length > 0 && (
        <Card title="Findings">
          <ul className="list-disc space-y-1.5 pl-5">
            {/* A list, not a stack of panels: findings are read top to bottom, and a border around
                each one turns five of them into five boxes to parse. A finding is EITHER a dict or
                an already-rendered "[warn] area: ..." string — hosts differ, so both are handled;
                reading dict fields off a string yields undefined for every one and used to render
                a lone "info" badge with no text. */}
            {report.findings.map((f, i) => (
              <li key={i} className="text-[12.5px] leading-relaxed text-gray-700">
                <FindingLine f={f} />
              </li>
            ))}
          </ul>
        </Card>
      )}

      {(report.items || []).length > 0 && (
        <Card title="Questions & proposals"
          subtitle="Approved proposals and answered questions are carried out by the apply agent.">
          <div className="space-y-3">
            {report.items!.map((it) => (
              <ItemRow key={it.id} api={api} item={it} onChanged={onChanged} onError={onError} />
            ))}
            {/* no project pill here: the whole detail view is already one project's report */}
          </div>
        </Card>
      )}

      <FollowUp api={api} report={report} project={project} onError={onError}
        agentLabels={agentLabels} />

      {report.transcript && (
        <a className="mt-2.5 inline-block text-xs text-blue-600 hover:underline"
           href={`${base}/agent_history/${encodeURIComponent(report.agent)}/${encodeURIComponent(report.transcript)}`}
           target="_blank" rel="noreferrer">
          Conversation history ↗
        </a>
      )}

      {report.detail && (
        <details className="mt-2.5">
          <summary className="cursor-pointer text-xs text-gray-400 hover:text-gray-600">
            Full report
          </summary>
          <LogBox text={report.detail} className="mt-2 max-h-[21rem]" />
        </details>
      )}
    </div>
  )
}

/**
 * Ask the agent about its own report.
 *
 * Two choices the answer depends on. WHICH agent — the one that wrote the report by default,
 * since it is the one that knows. WHETHER TO CONTINUE its session — continuing means it still
 * remembers what it did; starting fresh sends the whole report along instead, so it is never
 * asked about something it cannot see.
 *
 * In fleet mode this talks to the OWNING PROJECT's api, not the fleet's: the fleet has every
 * project's database but none of their agents, roots or prompts, so only the project itself can
 * run the follow-up. Same origin behind the reverse proxy, so it is a plain fetch.
 */
function FollowUp({ api, report, project, onError, agentLabels }: {
  api: KitApi; report: Report; project?: ProjectTag
  onError: (e: string) => void
  agentLabels?: Record<string, string>
}) {
  const askBase = project?.url ? project.url.replace(/\/$/, '') + '/api/kit' : undefined
  const rid = project ? (report.local_id ?? report.id) : report.id
  const [thread, setThread] = React.useState<ThreadMessage[]>([])
  const [text, setText] = React.useState('')
  const [agent, setAgent] = React.useState(report.agent)
  const [resume, setResume] = React.useState(true)
  const [busy, setBusy] = React.useState(false)
  const [agents, setAgents] = React.useState<string[]>([])

  const load = React.useCallback(async () => {
    try { setThread(await api.thread(rid, askBase)) } catch { /* not fatal — the form still works */ }
  }, [api, rid, askBase])

  React.useEffect(() => { load() }, [load])
  React.useEffect(() => {
    // Only poll while a reply is actually in flight; agents take minutes.
    if (!thread.some((m) => m.status === 'running')) return
    const t = setInterval(load, 3000)
    return () => clearInterval(t)
  }, [thread, load])
  React.useEffect(() => {
    new KitApi(askBase).agents().then((a) => setAgents(a.map((x: any) => x.name)))
      .catch(() => setAgents([]))
  }, [askBase])

  const send = async () => {
    if (!text.trim() || busy) return
    setBusy(true)
    try {
      const r = await api.ask(rid, { prompt: text, agent, resume }, askBase)
      if (!r.ok) { onError(r.error || 'follow-up failed'); return }
      setText('')
      await load()
    } catch (e: any) {
      onError(e.message)
    } finally {
      setBusy(false)
    }
  }

  const otherAgent = agent !== report.agent
  return (
    <Card title="Ask the agent">
      {thread.length > 0 && (
        <div className="mb-3 space-y-3">
          {thread.map((m) => (
            <div key={m.id} className={cx('rounded-lg px-3 py-2 text-sm whitespace-pre-wrap',
              // An ALPHA tint, not bg-blue-50: the flat 50-tints are the one thing the grey ramp does not
              // invert, so a pale blue box kept its colour while text-gray-900 flipped to light —
              // light ink on a light box. Alpha over the card works in either theme.
              m.role === 'user' ? 'bg-blue-400/10 text-gray-900' : 'bg-gray-50 text-gray-800')}>
              <div className="mb-1 text-xs font-medium text-gray-500">
                {m.role === 'user' ? 'You' : (agentLabels?.[m.agent || ''] || m.agent)}
                {' · '}{fmtAgo(m.created)}
                {m.status === 'error' && <span className="ml-2"><Badge tone="red">failed</Badge></span>}
                {m.rstatus && m.rstatus !== 'ok' && (
                  <span className="ml-2"><Badge tone={m.rstatus}>{m.rstatus}</Badge></span>)}
              </div>
              {m.status === 'running'
                ? <span className="text-gray-500"><Spinner /> thinking…</span>
                : <Markdown text={m.text} />}
              {/* the reply's json block, parsed: findings render like a report's own */}
              {/* defensive: a malformed field must never take the whole page down with it */}
              {Array.isArray(m.findings) && m.findings.length > 0 && (
                <ul className="mt-2 list-disc space-y-1 pl-5 text-xs text-gray-600">
                  {m.findings.map((f, i) => <li key={i}><FindingLine f={f} /></li>)}
                </ul>
              )}
              {/* questions/proposals THIS reply raised, answerable where they were asked */}
              {Array.isArray(m.items) && m.items.map((it) => (
                <div key={it.id} className="mt-2">
                  <ItemRow api={api} item={it} project={project} onChanged={load}
                    onError={onError} />
                </div>
              ))}
            </div>
          ))}
        </div>
      )}

      <textarea
        value={text}
        onChange={(e) => setText(e.target.value)}
        placeholder="Ask about this report — why something happened, or to go and fix it…"
        rows={3}
        className="w-full rounded-lg border border-gray-300 p-2 text-sm"
      />
      <div className="mt-2 flex flex-wrap items-center gap-2">
        <select value={agent} onChange={(e) => setAgent(e.target.value)}
          className="rounded-lg border border-gray-300 px-2 py-1 text-sm">
          {(agents.length ? agents : [report.agent]).map((a) => (
            <option key={a} value={a}>
              {(agentLabels?.[a] || a) + (a === report.agent ? ' (wrote this)' : '')}
            </option>
          ))}
        </select>
        <select
          value={otherAgent ? 'new' : (resume ? 'continue' : 'new')}
          disabled={otherAgent}
          onChange={(e) => setResume(e.target.value === 'continue')}
          className="rounded-lg border border-gray-300 px-2 py-1 text-sm disabled:opacity-60"
          /* another agent can't continue this conversation: its transcript was written under a
             different system prompt, so resuming it would read as that agent's own history */
          title={otherAgent ? 'A different agent always starts fresh (with the report attached)'
                            : 'Continue the run that produced this report, or start fresh'}
        >
          <option value="continue">Continue that session</option>
          <option value="new">New session (report attached)</option>
        </select>
        <Button onClick={send} disabled={!text.trim() || busy}>
          {busy ? 'Sending…' : 'Send'}
        </Button>
        <span className="text-xs text-gray-500">
          runs with full permissions — it may change files and commit
        </span>
      </div>
    </Card>
  )
}

function ItemRow({ api, item, onChanged, onError, project }: {
  api: KitApi; item: ReportItem; onChanged: () => void; onError: (e: string) => void
  project?: ProjectTag
}) {
  const [answer, setAnswer] = React.useState(item.answer || '')
  const [busy, setBusy] = React.useState(false)
  const settled = item.status !== 'open'

  const call = async (fn: () => Promise<any>) => {
    setBusy(true)
    try { await fn(); onChanged() } catch (e: any) { onError(e.message) } finally { setBusy(false) }
  }

  return (
    <div className={cx('rounded-lg border p-3',
      settled ? 'border-gray-100 bg-gray-50' : 'border-amber-200 bg-amber-50/40')}>
      <div className="flex flex-wrap items-center gap-2">
        <ProjectPill project={project} />
        <Badge tone={item.kind === 'proposal' ? 'warn' : 'info'}>{item.kind}</Badge>
        <Badge tone={item.status}>{item.status}</Badge>
        {item.agent && <span className="text-xs text-gray-400">from {item.agent}</span>}
      </div>

      <p className="mt-2 whitespace-pre-wrap text-sm text-gray-800">{item.text}</p>

      {settled ? (
        item.answer && <p className="mt-2 text-sm text-gray-500"><em>your note:</em> {item.answer}</p>
      ) : (
        <div className="mt-3 flex flex-wrap items-center gap-2">
          <input
            className="min-w-0 flex-1 rounded-md border border-gray-300 px-2 py-1 text-sm"
            placeholder={item.kind === 'question' ? 'Your answer…' : 'Optional note…'}
            value={answer} onChange={(e) => setAnswer(e.target.value)}
          />
          {item.kind === 'question' ? (
            <Button variant="primary" loading={busy} disabled={!answer.trim()}
              onClick={() => call(() => api.answer(item.id, answer))}>
              Answer
            </Button>
          ) : (
            <>
              <Button variant="primary" loading={busy}
                onClick={() => call(() => api.decide(item.id, true, answer))}>
                Approve
              </Button>
              <Button loading={busy} onClick={() => call(() => api.decide(item.id, false, answer))}>
                Reject
              </Button>
            </>
          )}
        </div>
      )}
    </div>
  )
}

/** Which project a row belongs to. Renders nothing outside fleet mode, so every call site can
 *  place one unconditionally instead of guarding. The colour is per-project data, not a theme
 *  token, so it is inline rather than a Tailwind class. */
function ProjectPill({ project }: { project?: ProjectTag }) {
  if (!project) return null
  const c = project.color
  return (
    <span
      className={cx('inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5 text-xs font-medium',
        !c && 'border-gray-200 text-gray-600')}
      style={c ? { borderColor: c, color: c } : undefined}
      title={`project: ${project.label}`}
    >
      <span className="h-1.5 w-1.5 rounded-full" style={{ background: c || '#9ca3af' }} />
      {project.label}
    </span>
  )
}

/** The filter along the top. Chips rather than a <select>: with a handful of projects the whole
 *  fleet is visible at a glance, and the active one is obvious. */
function ProjectFilter({ projects, value, onChange }: {
  projects: ProjectTag[]; value: string | null; onChange: (key: string | null) => void
}) {
  const chip = (active: boolean) => cx(
    'rounded-full border px-3 py-1 text-sm font-medium transition',
    active ? 'border-contrast bg-contrast text-oncontrast'
      : 'border-gray-200 bg-white text-gray-600 hover:border-gray-300 hover:bg-gray-50')
  return (
    <div className="mb-4 flex flex-wrap items-center gap-2">
      <button className={chip(!value)} onClick={() => onChange(null)}>All projects</button>
      {projects.map((p) => (
        <button key={p.key} className={chip(value === p.key)} onClick={() => onChange(p.key)}>
          <span className="mr-1.5 inline-block h-1.5 w-1.5 rounded-full align-middle"
            style={{ background: p.color || '#9ca3af' }} />
          {p.label}
        </button>
      ))}
    </div>
  )
}

/** Neutral counterpart to ErrorNote — for "this worked, here is what happened" messages that are
 *  not errors and would be a lie in red. */
function Notice({ text, onDismiss }: { text?: string | null; onDismiss?: () => void }) {
  if (!text) return null
  return (
    <div className="mb-3 flex items-start justify-between gap-3 rounded-lg border border-blue-200 bg-blue-50 px-3 py-2 text-sm text-blue-800">
      <span className="whitespace-pre-wrap break-words">{text}</span>
      {onDismiss && (
        <button onClick={onDismiss} className="shrink-0 text-blue-500 hover:text-blue-700" aria-label="Dismiss">×</button>
      )}
    </div>
  )
}

export default KitReports
