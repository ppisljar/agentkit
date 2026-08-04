/**
 * Reports — where agents talk to you.
 *
 * An agent investigates read-only and records what it found, what it needs decided (questions)
 * and what it would like to do but didn't (proposals). You answer and approve here; "Apply
 * approved" then hands *only* those items to the apply agent.
 */

import React from 'react'
import { Job, KitApi, Report, ReportItem, fmtAgo } from './api'
import { Badge, Button, Card, Empty, ErrorNote, LogBox, Spinner, cx } from './ui'

export function KitReports({ base = '/api/kit', agentLabels }: {
  base?: string
  /** Optional agent-id -> display name, so rows read "Daily health check" rather than "selfcheck".
   *  Left to the host because the kit doesn't know which of an app's agents deserve friendlier
   *  names, and the ids are what the API speaks. */
  agentLabels?: Record<string, string>
}) {
  const api = React.useMemo(() => new KitApi(base), [base])
  const [reports, setReports] = React.useState<Report[]>([])
  const [open, setOpen] = React.useState<Report | null>(null)
  const [pending, setPending] = React.useState<ReportItem[]>([])
  const [err, setErr] = React.useState<string | null>(null)
  const [applyJob, setApplyJob] = React.useState<number | null>(null)
  const [applying, setApplying] = React.useState(false)
  const [job, setJob] = React.useState<Job | null>(null)

  const load = React.useCallback(async () => {
    try {
      const [r, p] = await Promise.all([api.reports(), api.openItems()])
      setReports(r)
      setPending(p)
    } catch (e: any) { setErr(e.message) }
  }, [api])

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

  const refreshOpen = async (id: number) => {
    try { setOpen(await api.report(id)) } catch (e: any) { setErr(e.message) }
    load()
  }

  const actionable = pending.length
  const readyToApply = React.useMemo(
    () => reports.some((r) => r.open_items === 0) || pending.length === 0, [reports, pending])

  const apply = async () => {
    setApplying(true)
    try {
      const r = await api.applyDecisions()
      if (r.skipped) setErr('Nothing is approved or answered yet — decide on an item first.')
      else if (r.job) setApplyJob(r.job)
    } catch (e: any) { setErr(e.message) }
    finally { setApplying(false) }
  }

  return (
    <div className="mx-auto max-w-5xl px-4 py-6">
      <div className="mb-5 flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-2xl font-bold text-gray-900">Reports</h1>
          <p className="mt-1 text-sm text-gray-500">
            What the agents found, and anything waiting on you.
          </p>
        </div>
        <div className="flex items-center gap-2">
          {actionable > 0 && <Badge tone="open">{actionable} waiting on you</Badge>}
          <Button variant="primary" onClick={apply} loading={applying}>Apply approved</Button>
        </div>
      </div>

      <ErrorNote error={err} onDismiss={() => setErr(null)} />

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
        <ReportDetail api={api} report={open} agentLabels={agentLabels}
          onBack={() => { setOpen(null); load() }}
          onChanged={() => refreshOpen(open.id)} onError={setErr} />
      ) : (
        <>
          {pending.length > 0 && (
            <Card className="mb-5" title="Waiting on you"
              subtitle="Answer a question or approve a proposal, then press “Apply approved”.">
              <div className="space-y-3">
                {pending.map((it) => (
                  <ItemRow key={it.id} api={api} item={it} onChanged={load} onError={setErr} />
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
                <div className="flex items-center gap-2">
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

function ReportDetail({ api, report, onBack, onChanged, onError, agentLabels }: {
  api: KitApi; report: Report; onBack: () => void; onChanged: () => void
  onError: (e: string) => void
  agentLabels?: Record<string, string>
}) {
  const [showRaw, setShowRaw] = React.useState(false)
  return (
    <div className="space-y-4">
      <button onClick={onBack} className="text-sm text-blue-600 hover:underline">← All reports</button>

      <Card title={`${agentLabels?.[report.agent] || report.agent} · report #${report.id}`}
        subtitle={`${fmtAgo(report.created)} · ${report.duration_sec}s`}
        right={<Badge tone={report.status}>{report.status}</Badge>}>
        {report.summary && <p className="text-sm text-gray-700">{report.summary}</p>}
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

function ItemRow({ api, item, onChanged, onError }: {
  api: KitApi; item: ReportItem; onChanged: () => void; onError: (e: string) => void
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

export default KitReports
