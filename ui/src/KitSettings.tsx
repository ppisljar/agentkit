/**
 * Settings surface: scrapers, agents, services and config files.
 *
 * Drop into any host app:  <KitSettings />  (pass `base` if the API is not at /api/kit).
 */

import React from 'react'
import {
  AgentInfo, ConfigEntry, ConfigFile, Job, KitApi, MASK, ServiceInfo, Task, fmtAgo, fmtIn,
} from './api'
import { Badge, Button, Card, Empty, ErrorNote, LogBox, Spinner, Toggle, cx } from './ui'
import { KitTranscripts } from './KitTranscript'

type Tab = 'scrapers' | 'agents' | 'services' | 'config'

const TABS: { id: Tab; label: string }[] = [
  { id: 'scrapers', label: 'Scrapers' },
  { id: 'agents', label: 'Agents' },
  { id: 'services', label: 'Services' },
  { id: 'config', label: 'Configuration' },
]

/** Poll a job until it leaves `running`, so the UI shows live output without a websocket. */
function useJob(api: KitApi, jobId: number | null) {
  const [job, setJob] = React.useState<Job | null>(null)

  React.useEffect(() => {
    if (!jobId) { setJob(null); return }
    let alive = true
    let timer: any

    const tick = async () => {
      try {
        const j = await api.job(jobId)
        if (!alive) return
        setJob(j)
        if (j.status === 'running') timer = setTimeout(tick, 1500)
      } catch {
        if (alive) timer = setTimeout(tick, 4000)
      }
    }
    tick()
    return () => { alive = false; clearTimeout(timer) }
  }, [api, jobId])

  return job
}

/** A tab the HOST adds. Some settings are not kit concepts at all — restarting an app's own web
 *  process, or reloading its backend — and hard-coding them here would put one app's operations
 *  into every other app's UI. The host supplies the panel; the kit only makes room for it. */
export type ExtraTab = { id: string; label: string; content: React.ReactNode }

export function KitSettings({ base = '/api/kit', extraTabs = [] }:
    { base?: string; extraTabs?: ExtraTab[] }) {
  const api = React.useMemo(() => new KitApi(base), [base])
  const [tab, setTab] = React.useState<string>('scrapers')
  const tabs = [...TABS, ...extraTabs.map((t) => ({ id: t.id, label: t.label }))]

  return (
    <div className="mx-auto max-w-5xl px-4 py-6">
      <h1 className="mb-1 text-2xl font-bold text-gray-900">Settings</h1>
      <p className="mb-5 text-sm text-gray-500">
        Scrapers, agents, services and configuration for this install.
      </p>

      <div className="mb-5 flex gap-1 border-b border-gray-200">
        {tabs.map((t) => (
          <button
            key={t.id}
            onClick={() => setTab(t.id)}
            className={cx('-mb-px border-b-2 px-4 py-2 text-sm font-medium transition',
              tab === t.id
                ? 'border-blue-600 text-blue-700'
                : 'border-transparent text-gray-500 hover:text-gray-800')}
          >
            {t.label}
          </button>
        ))}
      </div>

      {/* key={base}: a changed base means a different target (e.g. another project). Remounting the
          active panel drops the previous target's agents/services/config up front, so they never
          show under the new one — or linger beside a 404 — while the panel refetches. */}
      {tab === 'scrapers' && <ScrapersPanel key={base} api={api} />}
      {tab === 'agents' && <AgentsPanel key={base} api={api} />}
      {tab === 'services' && <ServicesPanel key={base} api={api} />}
      {tab === 'config' && <ConfigPanel key={base} api={api} />}
      {extraTabs.map((t) => t.id === tab && <div key={t.id}>{t.content}</div>)}
    </div>
  )
}

/* ------------------------------------------------------------------ scrapers */

function ScrapersPanel({ api }: { api: KitApi }) {
  const [tasks, setTasks] = React.useState<Task[]>([])
  const [err, setErr] = React.useState<string | null>(null)
  const [busy, setBusy] = React.useState<string | null>(null)
  const [openJob, setOpenJob] = React.useState<number | null>(null)
  const job = useJob(api, openJob)

  const load = React.useCallback(async () => {
    try {
      const s = await api.schedule()
      setTasks(s.tasks.filter((t) => t.kind === 'adapter'))
    } catch (e: any) { setErr(e.message) }
  }, [api])

  React.useEffect(() => { load() }, [load])
  // refresh while a run is in flight so last_status/next_run update when it finishes
  React.useEffect(() => {
    if (job && job.status !== 'running') load()
  }, [job?.status, load])

  const patch = async (task: string, p: any) => {
    setBusy(task)
    try { await api.updateTask(task, p); await load() }
    catch (e: any) { setErr(e.message) }
    finally { setBusy(null) }
  }

  const run = async (task: string) => {
    setBusy(task)
    try {
      const r = await api.runTask(task)
      if (r.ok && r.job) setOpenJob(r.job)
      else setErr(r.error || 'could not start')
      await load()
    } catch (e: any) { setErr(e.message) }
    finally { setBusy(null) }
  }

  return (
    <div className="space-y-4">
      <ErrorNote error={err} onDismiss={() => setErr(null)} />
      {tasks.length === 0 && <Empty>No scrapers declared for this app.</Empty>}

      {tasks.map((t) => (
        <Card
          key={t.task}
          title={t.label}
          subtitle={<code className="text-xs">{(t.argv || []).join(' ')}</code>}
          right={
            <>
              <Badge tone={t.running ? 'running' : t.last_status}>
                {t.running ? 'running' : t.last_status}
              </Badge>
              <Button onClick={() => run(t.task)} loading={busy === t.task} disabled={t.running}>
                Run now
              </Button>
            </>
          }
        >
          <div className="flex flex-wrap items-center gap-x-6 gap-y-3">
            <Toggle checked={!!t.enabled} label="Scheduled"
              onChange={(v) => patch(t.task, { enabled: v ? 1 : 0 })} />
            <Toggle checked={!!t.paused} label="Paused"
              onChange={(v) => patch(t.task, { paused: v ? 1 : 0 })} disabled={!t.enabled} />

            <label className="flex items-center gap-2 text-sm text-gray-700">
              Every
              <select
                className="rounded-md border border-gray-300 px-2 py-1 text-sm"
                value={t.interval_sec ?? 21600}
                onChange={(e) => patch(t.task, { interval_sec: Number(e.target.value) })}
              >
                {[[1800, '30 min'], [3600, '1 h'], [21600, '6 h'], [43200, '12 h'], [86400, '24 h']]
                  .map(([v, l]) => <option key={v} value={v as number}>{l}</option>)}
              </select>
            </label>

            <ArgsEditor value={t.args} onSave={(a) => patch(t.task, { args: a })} />
          </div>

          <div className="mt-3 flex flex-wrap gap-x-5 gap-y-1 text-xs text-gray-500">
            <span>last run {fmtAgo(t.last_run)}</span>
            {t.last_duration_sec != null && <span>took {t.last_duration_sec}s</span>}
            {!!t.enabled && !t.paused && <span>next {fmtIn(t.next_run)}</span>}
            {t.last_error && <span className="text-red-600">error: {t.last_error}</span>}
          </div>

          {openJob && job && job.params?.task === t.task && (
            <div className="mt-3">
              <div className="mb-1 flex items-center gap-2 text-xs text-gray-500">
                <Badge tone={job.status}>{job.status}</Badge>
                {job.status === 'running' && <Spinner />}
                <button className="ml-auto hover:text-gray-800" onClick={() => setOpenJob(null)}>
                  hide output
                </button>
              </div>
              <LogBox text={job.log || '(no output yet)'} />
            </div>
          )}
        </Card>
      ))}
    </div>
  )
}

/** Extra CLI arguments, edited as a JSON array so quoting stays unambiguous. */
function ArgsEditor({ value, onSave }: { value: string; onSave: (v: string) => void }) {
  const [text, setText] = React.useState(value || '[]')
  const [bad, setBad] = React.useState(false)
  React.useEffect(() => setText(value || '[]'), [value])

  const commit = () => {
    try {
      const parsed = JSON.parse(text)
      if (!Array.isArray(parsed)) throw new Error('not an array')
      setBad(false)
      onSave(JSON.stringify(parsed))
    } catch { setBad(true) }
  }

  return (
    <label className="flex items-center gap-2 text-sm text-gray-700">
      Extra args
      <input
        className={cx('w-56 rounded-md border px-2 py-1 font-mono text-xs',
          bad ? 'border-red-400 bg-red-50' : 'border-gray-300')}
        value={text}
        onChange={(e) => setText(e.target.value)}
        onBlur={commit}
        onKeyDown={(e) => e.key === 'Enter' && commit()}
        placeholder='["--pages","3"]'
        title='JSON array, e.g. ["--pages","3"]'
      />
    </label>
  )
}

/* -------------------------------------------------------------------- agents */

function AgentsPanel({ api }: { api: KitApi }) {
  const [agents, setAgents] = React.useState<AgentInfo[]>([])
  const [tasks, setTasks] = React.useState<Record<string, Task>>({})
  const [err, setErr] = React.useState<string | null>(null)
  const [editing, setEditing] = React.useState<string | null>(null)
  const [busy, setBusy] = React.useState<string | null>(null)
  const [openJob, setOpenJob] = React.useState<number | null>(null)
  const job = useJob(api, openJob)

  const load = React.useCallback(async () => {
    try {
      const [a, s] = await Promise.all([api.agents(), api.schedule()])
      setAgents(a)
      setTasks(Object.fromEntries(s.tasks.filter((t) => t.kind === 'agent').map((t) => [t.task, t])))
    } catch (e: any) { setErr(e.message) }
  }, [api])

  React.useEffect(() => { load() }, [load])
  React.useEffect(() => { if (job && job.status !== 'running') load() }, [job?.status, load])

  const run = async (name: string) => {
    setBusy(name)
    try {
      const r = await api.runAgent(name)
      if (r.ok && r.job) setOpenJob(r.job)
      else setErr(r.error || 'could not start')
    } catch (e: any) { setErr(e.message) }
    finally { setBusy(null) }
  }

  return (
    <div className="space-y-4">
      <ErrorNote error={err} onDismiss={() => setErr(null)} />

      {agents.map((a) => {
        const t = tasks[a.name]
        return (
          <Card
            key={a.name}
            title={a.label}
            subtitle={a.description}
            right={
              <>
                {a.customized && <Badge tone="info">edited</Badge>}
                <Badge tone={t?.running ? 'running' : t?.last_status || 'never'}>
                  {t?.running ? 'running' : t?.last_status || a.schedule}
                </Badge>
                <Button onClick={() => run(a.name)} loading={busy === a.name} disabled={t?.running}>
                  Run now
                </Button>
              </>
            }
          >
            <div className="flex flex-wrap items-center gap-x-6 gap-y-3">
              {a.schedule === 'daily' && t && (
                <>
                  <Toggle checked={!!t.enabled} label="Scheduled daily"
                    onChange={async (v) => { await api.updateTask(a.name, { enabled: v ? 1 : 0 }); load() }} />
                  <label className="flex items-center gap-2 text-sm text-gray-700">
                    at
                    <select
                      className="rounded-md border border-gray-300 px-2 py-1 text-sm"
                      value={a.hour}
                      onChange={async (e) => { await api.setHour(a.name, Number(e.target.value)); load() }}
                    >
                      {Array.from({ length: 24 }, (_, h) => (
                        <option key={h} value={h}>{String(h).padStart(2, '0')}:00</option>
                      ))}
                    </select>
                  </label>
                </>
              )}
              {a.schedule !== 'daily' && (
                <span className="text-sm text-gray-500">Runs {a.schedule}.</span>
              )}
              <Button variant="ghost" onClick={() => setEditing(editing === a.name ? null : a.name)}>
                {editing === a.name ? 'Close prompts' : 'Edit prompts'}
              </Button>
            </div>

            <div className="mt-2 text-xs text-gray-500">
              last run {fmtAgo(t?.last_run)}
              {t?.last_error && <span className="ml-3 text-red-600">error: {t.last_error}</span>}
            </div>

            <KitTranscripts api={api} agent={a.name} />

            {editing === a.name && (
              <PromptEditor api={api} agent={a} onSaved={load} onError={setErr} />
            )}

            {openJob && job && job.label === `agent:${a.name}` && (
              <div className="mt-3">
                <div className="mb-1 flex items-center gap-2 text-xs text-gray-500">
                  <Badge tone={job.status}>{job.status}</Badge>
                  {job.status === 'running' && <><Spinner /> <span>this can take several minutes</span></>}
                  {job.result?.report && (
                    <span className="text-green-700">→ report #{job.result.report}</span>
                  )}
                  <button className="ml-auto hover:text-gray-800" onClick={() => setOpenJob(null)}>hide</button>
                </div>
                <LogBox text={job.log || '(waiting for the agent…)'} />
              </div>
            )}
          </Card>
        )
      })}
    </div>
  )
}

function PromptEditor({ api, agent, onSaved, onError }: {
  api: KitApi; agent: AgentInfo; onSaved: () => void; onError: (e: string) => void
}) {
  const [system, setSystem] = React.useState(agent.system)
  const [prompt, setPrompt] = React.useState(agent.prompt)
  const [saving, setSaving] = React.useState(false)
  const dirty = system !== agent.system || prompt !== agent.prompt

  return (
    <div className="mt-4 space-y-3 border-t border-gray-100 pt-4">
      <Field label="System prompt" value={system} onChange={setSystem} rows={5} />
      <Field label="User prompt" value={prompt} onChange={setPrompt} rows={12} />
      <div className="flex items-center gap-2">
        <Button variant="primary" loading={saving} disabled={!dirty}
          onClick={async () => {
            setSaving(true)
            try { await api.setPrompt(agent.name, system, prompt); onSaved() }
            catch (e: any) { onError(e.message) }
            finally { setSaving(false) }
          }}>
          Save
        </Button>
        <Button
          onClick={async () => {
            try {
              await api.resetAgent(agent.name)
              const fresh = (await api.agents()).find((x) => x.name === agent.name)
              if (fresh) { setSystem(fresh.system); setPrompt(fresh.prompt) }
              onSaved()
            } catch (e: any) { onError(e.message) }
          }}>
          Reset to default
        </Button>
        {dirty && <span className="text-xs text-amber-600">unsaved changes</span>}
      </div>
    </div>
  )
}

function Field({ label, value, onChange, rows = 6, mono = true }: {
  label: string; value: string; onChange: (v: string) => void; rows?: number; mono?: boolean
}) {
  return (
    <label className="block">
      <span className="mb-1 block text-xs font-medium uppercase tracking-wide text-gray-500">{label}</span>
      <textarea
        className={cx('w-full rounded-lg border border-gray-300 p-2 text-sm', mono && 'font-mono text-xs')}
        rows={rows} value={value} onChange={(e) => onChange(e.target.value)}
      />
    </label>
  )
}

/* ------------------------------------------------------------------ services */

function ServicesPanel({ api }: { api: KitApi }) {
  const [svcs, setSvcs] = React.useState<ServiceInfo[]>([])
  const [err, setErr] = React.useState<string | null>(null)
  const [busy, setBusy] = React.useState<string | null>(null)
  const [note, setNote] = React.useState<string | null>(null)

  const load = React.useCallback(async () => {
    try { setSvcs(await api.services()) } catch (e: any) { setErr(e.message) }
  }, [api])
  React.useEffect(() => { load() }, [load])

  const act = async (key: string, action: string, label: string) => {
    if (action === 'restart' && !confirm(`Restart ${label}? In-flight requests will be dropped.`)) return
    setBusy(key); setNote(null)
    try {
      const r = await api.serviceAction(key, action)
      if (r.ok === false) setErr(r.hint ? `${r.error || r.output}\n\nhint: ${r.hint}` : (r.error || r.output))
      else setNote(`${label}: ${action} requested`)
      // a restart takes a moment; re-read state once it has had time to come back
      setTimeout(load, 4000)
    } catch (e: any) { setErr(e.message) }
    finally { setBusy(null) }
  }

  return (
    <div className="space-y-4">
      <ErrorNote error={err} onDismiss={() => setErr(null)} />
      {note && <div className="rounded-lg border border-green-200 bg-green-50 px-3 py-2 text-sm text-green-800">{note}</div>}

      {svcs.map((s) => (
        <Card key={s.key} title={s.label} subtitle={<code className="text-xs">{s.unit}</code>}
          right={
            <>
              <Badge tone={s.active === 'active' ? 'active' : 'fail'}>{s.active}</Badge>
              <Badge tone="info">{s.enabled}</Badge>
              {s.actions.filter((a) => a !== 'status').map((a) => (
                <Button key={a} variant={a === 'restart' ? 'danger' : 'default'}
                  loading={busy === s.key} onClick={() => act(s.key, a, s.label)}>
                  {a}
                </Button>
              ))}
            </>
          }
        >
          <p className="text-sm text-gray-500">
            {s.actions.includes('restart')
              ? 'Restarting drops in-flight requests; the service comes back on its own.'
              : 'Read-only here — this unit is not restartable from the UI.'}
          </p>
        </Card>
      ))}
    </div>
  )
}

/* -------------------------------------------------------------------- config */

function ConfigPanel({ api }: { api: KitApi }) {
  const [files, setFiles] = React.useState<ConfigFile[]>([])
  const [open, setOpen] = React.useState<string | null>(null)
  const [err, setErr] = React.useState<string | null>(null)

  React.useEffect(() => {
    api.configFiles().then(setFiles).catch((e) => setErr(e.message))
  }, [api])

  return (
    <div className="space-y-4">
      <ErrorNote error={err} onDismiss={() => setErr(null)} />
      <div className="rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-800">
        Saving writes the file and keeps a <code>.bak</code> of the previous version. Most changes
        need a service restart to take effect.
      </div>

      {files.map((f) => (
        <Card key={f.key} title={f.label}
          subtitle={<code className="text-xs">{f.path}</code>}
          right={
            <>
              <Badge tone="info">{f.format}</Badge>
              {!f.exists && <Badge tone="fail">missing</Badge>}
              <Button onClick={() => setOpen(open === f.key ? null : f.key)} disabled={!f.exists}>
                {open === f.key ? 'Close' : 'Edit'}
              </Button>
            </>
          }
        >
          {open === f.key && <ConfigEditor api={api} fileKey={f.key} onError={setErr} />}
        </Card>
      ))}
    </div>
  )
}

function ConfigEditor({ api, fileKey, onError }: {
  api: KitApi; fileKey: string; onError: (e: string) => void
}) {
  const [file, setFile] = React.useState<ConfigFile | null>(null)
  const [entries, setEntries] = React.useState<ConfigEntry[]>([])
  const [content, setContent] = React.useState('')
  const [saving, setSaving] = React.useState(false)
  const [saved, setSaved] = React.useState(false)

  React.useEffect(() => {
    api.configFile(fileKey).then((f) => {
      setFile(f)
      setEntries(f.entries || [])
      setContent(f.content || '')
    }).catch((e) => onError(e.message))
  }, [api, fileKey, onError])

  if (!file) return <div className="py-4 text-sm text-gray-500">Loading…</div>

  const save = async () => {
    setSaving(true); setSaved(false)
    try {
      await api.writeConfig(fileKey, file.format === 'env' ? { entries } : { content })
      setSaved(true)
    } catch (e: any) { onError(e.message) }
    finally { setSaving(false) }
  }

  return (
    <div className="space-y-3">
      {file.format === 'env' ? (
        <div className="space-y-1.5">
          {entries.map((e, i) =>
            e.type === 'kv' ? (
              <div key={i} className="flex items-center gap-2">
                <span className="w-56 shrink-0 truncate font-mono text-xs text-gray-600" title={e.key}>
                  {e.key}
                </span>
                <input
                  className="flex-1 rounded-md border border-gray-300 px-2 py-1 font-mono text-xs"
                  value={e.value ?? ''}
                  onChange={(ev) => {
                    const next = [...entries]
                    next[i] = { ...e, value: ev.target.value }
                    setEntries(next)
                  }}
                />
                {e.secret && (
                  <span className="shrink-0 text-xs text-gray-400"
                    title="The real value stays on the server. Leave the dots to keep it.">
                    secret
                  </span>
                )}
              </div>
            ) : null,
          )}
          <p className="pt-1 text-xs text-gray-500">
            Comments and blank lines are preserved. Leave a secret as {MASK} to keep its stored value.
          </p>
        </div>
      ) : (
        <textarea
          className="w-full rounded-lg border border-gray-300 p-2 font-mono text-xs"
          rows={18} value={content} onChange={(e) => setContent(e.target.value)}
        />
      )}

      <div className="flex items-center gap-2">
        <Button variant="primary" loading={saving} onClick={save}>Save</Button>
        {saved && <span className="text-sm text-green-700">Saved — restart to apply.</span>}
      </div>
    </div>
  )
}

export default KitSettings
