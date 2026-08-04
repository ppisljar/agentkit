/**
 * Typed client for the claudeKit HTTP surface.
 *
 * Uses plain fetch so the kit adds no npm dependencies to a host app — pass a custom `base` if
 * your API is mounted somewhere other than /api/kit.
 */

export type AgentInfo = {
  name: string
  label: string
  description: string
  schedule: string
  model: string
  system: string
  prompt: string
  customized: boolean
  hour: number
}

export type Task = {
  task: string
  kind: 'adapter' | 'agent'
  enabled: number
  paused: number
  interval_sec: number | null
  hour: number | null
  args: string
  last_run: number | null
  last_duration_sec: number | null
  last_status: string
  last_error: string | null
  next_run: number | null
  running: boolean
  label: string
  description?: string
  argv?: string[]
}

export type Job = {
  id: number
  kind: string
  status: 'running' | 'done' | 'error' | 'interrupted'
  created: number
  updated: number
  label: string
  params: any
  result: any
  error: string | null
  log: string | null
}

export type Finding = {
  area?: string
  severity?: 'info' | 'warn' | 'fail'
  title?: string
  detail?: string
  action?: string
}

/** Row id. A single app's API numbers its rows; a fleet API (claudekit.fleet) addresses rows
 *  across several projects and so uses "<project>:<id>". Ids are only ever passed back, never
 *  arithmetic, so both work everywhere. */
export type RowId = number | string

/** Fields a fleet API adds to every row so the UI can say which project it came from.
 *  Absent on a single-app API — code that renders them must treat them as optional. */
export type ProjectTagged = {
  project?: string
  project_label?: string
  project_url?: string
  project_color?: string
  /** The row's id within its own project, before it was namespaced. */
  local_id?: number
}

export type ReportItem = ProjectTagged & {
  id: RowId
  report_id: RowId
  kind: 'question' | 'proposal'
  text: string
  status: 'open' | 'answered' | 'approved' | 'rejected' | 'done'
  answer: string | null
  answered_at: number | null
  agent?: string
}

export type Report = ProjectTagged & {
  id: RowId
  created: number
  agent: string
  status: string
  summary: string
  findings: Finding[]
  duration_sec: number
  ok: number
  open_items?: number
  items?: ReportItem[]
  detail?: string
}

/** One project in a fleet — GET {base}/projects. `ok: false` means its database could not be
 *  read; the row is still listed, because a broken project is exactly what you want to see. */
export type FleetProject = {
  key: string
  label: string
  db?: string
  url?: string
  color?: string
  ok: boolean
  error?: string | null
  reports: number
  open_items: number
  pending_action?: number
  last_report?: number | null
}

/** What POST {base}/reports/apply returns. A single app starts the apply agent itself and returns
 *  a `job` to poll; a fleet cannot run another project's agent, so it queues a run request per
 *  project and describes it in `note`. */
export type ApplyResult = {
  ok: boolean
  job?: number
  skipped?: string
  note?: string
  queued?: { project: string; project_label?: string; task: string; items: number[] }[]
  failed?: { project: string; error: string }[]
}

export type ServiceInfo = {
  key: string
  label: string
  unit: string
  actions: string[]
  active: string
  enabled: string
}

export type ConfigEntry = { type: 'kv' | 'raw'; key?: string; value?: string; text?: string; secret?: boolean }
export type ConfigFile = {
  key: string
  label: string
  path?: string
  format: 'env' | 'json' | 'yaml' | 'text'
  editable?: boolean
  exists: boolean
  content?: string
  entries?: ConfigEntry[]
}

export const MASK = '••••••••'

export class KitApi {
  constructor(private base = '/api/kit') {}

  private async req<T>(path: string, init?: RequestInit): Promise<T> {
    const res = await fetch(this.base + path, {
      ...init,
      headers: { 'Content-Type': 'application/json', ...(init?.headers || {}) },
    })
    if (!res.ok) {
      let detail = res.statusText
      try {
        const body = await res.json()
        detail = body?.detail || detail
      } catch {
        /* non-JSON error body — keep the status text */
      }
      throw new Error(`${res.status}: ${detail}`)
    }
    return res.status === 204 ? (undefined as T) : ((await res.json()) as T)
  }

  private post<T>(path: string, body?: any) {
    return this.req<T>(path, { method: 'POST', body: body ? JSON.stringify(body) : undefined })
  }

  health = () => this.req<any>('/health')

  // agents
  agents = () => this.req<AgentInfo[]>('/agents')
  setPrompt = (name: string, system: string, prompt: string) =>
    this.post(`/agents/${name}/prompt`, { system, prompt })
  resetAgent = (name: string) => this.post(`/agents/${name}/reset`)
  setHour = (name: string, hour: number) => this.post(`/agents/${name}/hour`, { hour })
  runAgent = (name: string) => this.post<{ ok: boolean; job?: number; error?: string }>(`/agents/${name}/run`)

  // transcripts
  history = (agent?: string) =>
    this.req<any[]>('/agent_history' + (agent ? `?agent=${encodeURIComponent(agent)}` : ''))
  transcript = (agent: string, file: string) =>
    this.req<any>(`/agent_history/${encodeURIComponent(agent)}/${encodeURIComponent(file)}`)

  // reports
  //
  // `project` is only meaningful against a fleet API, where it narrows the merged list to one
  // project; a single-app API ignores the unknown query parameter, so the same client serves both.
  private qs = (params: Record<string, string | number | null | undefined>) => {
    const q = new URLSearchParams()
    for (const [k, v] of Object.entries(params)) if (v !== null && v !== undefined) q.set(k, String(v))
    const s = q.toString()
    return s ? `?${s}` : ''
  }

  reports = (limit = 30, project?: string | null) =>
    this.req<Report[]>(`/reports${this.qs({ limit, project })}`)
  openItems = (project?: string | null) =>
    this.req<ReportItem[]>(`/reports/open${this.qs({ project })}`)
  report = (id: RowId) => this.req<Report>(`/reports/${id}`)
  answer = (itemId: RowId, answer: string) => this.post<ReportItem>(`/reports/item/${itemId}/answer`, { answer })
  decide = (itemId: RowId, approved: boolean, note = '') =>
    this.post<ReportItem>(`/reports/item/${itemId}/decide`, { approved, note })
  applyDecisions = (project?: string | null) =>
    this.post<ApplyResult>(`/reports/apply${this.qs({ project })}`)

  // fleet (claudekit.fleet hosts only)
  fleetProjects = () => this.req<FleetProject[]>('/projects')

  // scheduler
  schedule = () => this.req<{ alive: boolean; tasks: Task[] }>('/schedule')
  updateTask = (task: string, patch: Partial<Record<'enabled' | 'paused' | 'interval_sec' | 'hour' | 'args', any>>) =>
    this.post<Task>(`/schedule/${encodeURIComponent(task)}`, patch)
  runTask = (task: string) =>
    this.post<{ ok: boolean; job?: number; error?: string }>(`/schedule/${encodeURIComponent(task)}/run`)

  // jobs
  jobs = (kind?: string, limit = 20) =>
    this.req<Job[]>(`/jobs?limit=${limit}` + (kind ? `&kind=${kind}` : ''))
  job = (id: number) => this.req<Job>(`/jobs/${id}`)

  // services
  services = () => this.req<ServiceInfo[]>('/services')
  serviceAction = (key: string, action: string) => this.post<any>(`/services/${key}/${action}`)

  // config files
  configFiles = () => this.req<ConfigFile[]>('/config')
  configFile = (key: string) => this.req<ConfigFile>(`/config/${key}`)
  writeConfig = (key: string, body: { content?: string; entries?: ConfigEntry[] }) =>
    this.req<any>(`/config/${key}`, { method: 'PUT', body: JSON.stringify(body) })
}

export const fmtAgo = (ts?: number | null): string => {
  if (!ts) return 'never'
  const s = Math.max(0, Date.now() / 1000 - ts)
  if (s < 60) return `${Math.round(s)}s ago`
  if (s < 3600) return `${Math.round(s / 60)}m ago`
  if (s < 86400) return `${Math.round(s / 3600)}h ago`
  return `${Math.round(s / 86400)}d ago`
}

export const fmtIn = (ts?: number | null): string => {
  if (!ts) return '—'
  const s = ts - Date.now() / 1000
  if (s <= 0) return 'due'
  if (s < 60) return `in ${Math.round(s)}s`
  if (s < 3600) return `in ${Math.round(s / 60)}m`
  if (s < 86400) return `in ${Math.round(s / 3600)}h`
  return `in ${Math.round(s / 86400)}d`
}
