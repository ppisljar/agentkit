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
import { ApplyResult, Job, KitApi, Report, ReportItem, RowId, fmtAgo } from './api'
import { Badge, Button, Card, Empty, ErrorNote, LogBox, Spinner, cx } from './ui'

/** A project the reports may belong to. `key` is what the API filters on. */
export type ProjectTag = { key: string; label: string; color?: string; url?: string }

export function KitReports({ base = '/api/kit', agentLabels, projects, title, subtitle }: {
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
}) {
  const api = React.useMemo(() => new KitApi(base), [base])
  const [reports, setReports] = React.useState<Report[]>([])
  const [open, setOpen] = React.useState<Report | null>(null)
  const [pending, setPending] = React.useState<ReportItem[]>([])
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
      const [r, p] = await Promise.all([api.reports(30, project), api.openItems(project)])
      setReports(r)
      setPending(p)
    } catch (e: any) { setErr(e.message) }
  }, [api, project])

  React.useEffect(() => { load() }, [load])

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
  const readyToApply = React.useMemo(
    () => reports.some((r) => r.open_items === 0) || pending.length === 0, [reports, pending])

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
          {actionable > 0 && <Badge tone="open">{actionable} waiting on you</Badge>}
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

      {open ? (
        <ReportDetail api={api} report={open} agentLabels={agentLabels} project={byKey[open.project!]}
          onBack={() => { setOpen(null); load() }}
          onChanged={() => refreshOpen(open.id)} onError={setErr} />
      ) : (
        <>
          {pending.length > 0 && (
            <Card className="mb-5" title="Waiting on you"
              subtitle="Answer a question or approve a proposal, then press “Apply approved”.">
              <div className="space-y-3">
                {pending.map((it) => (
                  <ItemRow key={it.id} api={api} item={it} project={byKey[it.project!]}
                    onChanged={load} onError={setErr} />
                ))}
              </div>
            </Card>
          )}

          {reports.length === 0 && (
            <Empty>No reports yet. Run an agent from Settings → Agents.</Empty>
          )}

          <div className="space-y-3">
            {reports.map((r) => (
              <button key={r.id} onClick={() => refreshOpen(r.id)}
                className="block w-full rounded-xl border border-gray-200 bg-white p-4 text-left shadow-sm transition hover:border-gray-300 hover:shadow">
                <div className="flex flex-wrap items-center gap-2">
                  <ProjectPill project={byKey[r.project!]} />
                  <Badge tone={r.status}>{r.status}</Badge>
                  <span className="font-medium text-gray-900">{agentLabels?.[r.agent] || r.agent}</span>
                  <span className="text-xs text-gray-500">{fmtAgo(r.created)}</span>
                  {!!r.open_items && (
                    <span className="ml-auto"><Badge tone="open">{r.open_items} open</Badge></span>
                  )}
                </div>
                {r.summary && <p className="mt-2 text-sm text-gray-600">{r.summary}</p>}
                <div className="mt-2 text-xs text-gray-400">
                  {(r.findings || []).length} findings · {r.duration_sec}s
                </div>
              </button>
            ))}
          </div>
        </>
      )}
    </div>
  )
}

function ReportDetail({ api, report, onBack, onChanged, onError, agentLabels, project }: {
  api: KitApi; report: Report; onBack: () => void; onChanged: () => void
  onError: (e: string) => void
  agentLabels?: Record<string, string>
  project?: ProjectTag
}) {
  const [showRaw, setShowRaw] = React.useState(false)
  // In fleet mode the id is namespaced ("homeflix:23"); show the project as a pill and the
  // number the project itself would show, rather than repeating the key inside the heading.
  const shown = report.local_id ?? report.id
  return (
    <div className="space-y-4">
      <button onClick={onBack} className="text-sm text-blue-600 hover:underline">← All reports</button>

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

      {(report.findings || []).length > 0 && (
        <Card title="Findings">
          <div className="space-y-2">
            {report.findings.map((f, i) => (
              <div key={i} className="rounded-lg border border-gray-100 p-3">
                <div className="flex items-center gap-2">
                  <Badge tone={f.severity}>{f.severity || 'info'}</Badge>
                  {f.area && <span className="text-xs uppercase tracking-wide text-gray-400">{f.area}</span>}
                  <span className="font-medium text-gray-900">{f.title}</span>
                  {f.action === 'fixed' && <Badge tone="ok">fixed</Badge>}
                </div>
                {f.detail && <p className="mt-1 whitespace-pre-wrap text-sm text-gray-600">{f.detail}</p>}
              </div>
            ))}
          </div>
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

      {report.detail && (
        <Card title="Raw agent output"
          right={<Button variant="ghost" onClick={() => setShowRaw(!showRaw)}>
            {showRaw ? 'Hide' : 'Show'}
          </Button>}>
          {showRaw && <LogBox text={report.detail} className="max-h-[32rem]" />}
        </Card>
      )}
    </div>
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
    active ? 'border-gray-900 bg-gray-900 text-white'
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
