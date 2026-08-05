/**
 * Conversation history for one agent: past runs, each expanding to its full transcript.
 *
 * The backend already captures every run's `.jsonl` and parses it (agent_run.list_history /
 * parse_transcript, exposed at /agent_history); this is the missing surface for it. Being able to
 * read what an agent actually did — including each tool call and what it returned — is the
 * difference between trusting a one-line report and being able to check it.
 */

import React from 'react'
import { KitApi, fmtAgo } from './api'
import { Badge, Button, Empty, Markdown, Spinner, cx } from './ui'

type ToolCall = { name?: string; input?: unknown; result?: string | null; is_error?: boolean }
type Msg = { role: string; text?: string; ts?: string; tools?: ToolCall[] }
type Run = { agent: string; file: string; session: string; mtime: number; size: number }

function pretty(v: unknown): string {
  if (v == null) return ''
  if (typeof v === 'string') return v
  try {
    return JSON.stringify(v, null, 2)
  } catch {
    return String(v)
  }
}

function fmtSize(n: number): string {
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(0)} KB`
  return `${(n / 1024 / 1024).toFixed(1)} MB`
}

/** One tool call, collapsed by default — a run can make hundreds. */
function ToolCard({ t }: { t: ToolCall }) {
  const [open, setOpen] = React.useState(false)
  return (
    <div className={cx('rounded border', t.is_error ? 'border-red-300 bg-red-50' : 'border-gray-200 bg-gray-50')}>
      <button
        className="flex w-full items-center justify-between px-2 py-1 text-left text-xs font-medium text-gray-700"
        onClick={() => setOpen((v) => !v)}
      >
        <span className="truncate">🔧 {t.name || 'tool'}</span>
        <span className="ml-2 text-gray-400">{open ? '▾' : '▸'}</span>
      </button>
      {open && (
        <div className="space-y-1 border-t border-gray-200 px-2 py-1">
          {t.input != null && (
            <>
              <div className="text-[10px] uppercase tracking-wide text-gray-400">input</div>
              <Payload text={pretty(t.input)} lang={langOf(t.name)} max="max-h-48" />
            </>
          )}
          {t.result != null && (
            <>
              <div className="text-[10px] uppercase tracking-wide text-gray-400">result</div>
              {t.is_error
                ? <pre className="max-h-64 overflow-auto whitespace-pre-wrap break-words rounded bg-red-100 p-1.5 text-[11px] text-red-800">{pretty(t.result)}</pre>
                : <Payload text={pretty(t.result)} max="max-h-64" />}
            </>
          )}
        </div>
      )}
    </div>
  )
}

/** One run's messages: user/assistant turns plus the tool calls each turn made. */
export function Transcript({ messages }: { messages: Msg[] }) {
  if (!messages.length) return <Empty>No messages in this run.</Empty>
  return (
    <div className="space-y-3">
      {messages.map((m, i) => (
        <div key={i} className={cx('rounded-lg border p-2',
          m.role === 'user' ? 'border-gray-200 bg-gray-50' : 'border-blue-100 bg-white')}>
          <div className="mb-1 flex items-center gap-2">
            <Badge tone={m.role === 'user' ? undefined : 'ok'}>{m.role}</Badge>
            {m.ts && <span className="text-[11px] text-gray-400">{m.ts}</span>}
          </div>
          {m.text && <Markdown text={m.text} className="break-words" />}
          {!!(m.tools || []).length && (
            <div className="mt-2 space-y-1">
              {m.tools!.map((t, j) => <ToolCard key={j} t={t} />)}
            </div>
          )}
        </div>
      ))}
    </div>
  )
}

/**
 * Collapsible per-agent run history. Nothing is fetched until it's opened, and a transcript is
 * fetched only when its run is expanded — these files reach several MB.
 */

/** A tool's input/result. Syntax-highlighted through the shared Markdown renderer rather than a
 *  second highlighter setup — a Bash input is a shell script and a Read result is usually source,
 *  and both are far easier to scan highlighted than as one grey block. */
function Payload({ text, lang, max }: { text: string; lang?: string; max: string }) {
  if (!text) return null
  return (
    <div className={cx('overflow-auto', max)}>
      <Markdown text={'```' + (lang || '') + '\n' + text + '\n```'} className="text-[11px]" />
    </div>
  )
}

/** Best-guess language for a tool's input, so the highlighter has something to work with. */
function langOf(tool?: string): string {
  const t = (tool || '').toLowerCase()
  if (t === 'bash') return 'bash'
  if (t === 'edit' || t === 'write' || t === 'read') return ''   // could be anything; no guess
  return 'json'
}

/**
 * ONE captured run, rendered.
 *
 * Split out of KitTranscripts so a report can open the exact conversation that produced it —
 * previously the only way in was the per-agent history panel, which meant finding the right run by
 * timestamp, and the report's own link went to raw .jsonl.
 */
export function KitTranscript({ api, agent, file }: { api: KitApi; agent: string; file: string }) {
  const [msgs, setMsgs] = React.useState<Msg[] | null>(null)
  const [err, setErr] = React.useState<string | null>(null)

  React.useEffect(() => {
    let alive = true
    api.transcript(agent, file)
      .then((r: any) => { if (alive) setMsgs(r.messages || r || []) })
      .catch((e: any) => { if (alive) setErr(e.message) })
    return () => { alive = false }
  }, [api, agent, file])

  if (err) return <div className="text-xs text-red-600">{err}</div>
  if (!msgs) return <div className="py-3 text-center text-gray-500"><Spinner /></div>
  if (!msgs.length) return <Empty>That transcript has no messages.</Empty>
  return <Transcript messages={msgs} />
}

export function KitTranscripts({ api, agent }: { api: KitApi; agent: string }) {
  const [open, setOpen] = React.useState(false)
  const [runs, setRuns] = React.useState<Run[] | null>(null)
  const [sel, setSel] = React.useState<string | null>(null)
  const [msgs, setMsgs] = React.useState<Msg[] | null>(null)
  const [loading, setLoading] = React.useState(false)
  const [err, setErr] = React.useState<string | null>(null)

  React.useEffect(() => {
    if (!open || runs !== null) return
    api.history(agent)
      .then((r) => setRuns((r as Run[]) || []))
      .catch((e) => { setErr(String(e)); setRuns([]) })
  }, [open, runs, agent, api])

  const openRun = async (file: string) => {
    if (sel === file) { setSel(null); setMsgs(null); return }
    setSel(file); setMsgs(null); setLoading(true); setErr(null)
    try {
      const d = await api.transcript(agent, file)
      setMsgs((d?.messages as Msg[]) || [])
    } catch (e) {
      setErr(String(e)); setMsgs([])
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="mt-2">
      <Button variant="ghost" onClick={() => setOpen((v) => !v)}>
        {open ? 'Hide conversation history' : 'Conversation history'}
      </Button>
      {open && (
        <div className="mt-2 space-y-1">
          {err && <p className="text-xs text-red-600">{err}</p>}
          {runs === null && <div className="flex items-center gap-2 text-sm text-gray-500"><Spinner /> Loading…</div>}
          {runs !== null && !runs.length && <Empty>No past runs.</Empty>}
          {(runs || []).map((r) => (
            <div key={r.file} className="rounded border border-gray-200">
              <button
                className="flex w-full items-center justify-between px-2 py-1 text-left text-sm"
                onClick={() => void openRun(r.file)}
              >
                <span className="text-gray-700">
                  <span className="mr-2 text-gray-400">{sel === r.file ? '▾' : '▸'}</span>
                  {fmtAgo(r.mtime)}
                </span>
                <span className="text-xs text-gray-400">{fmtSize(r.size)}</span>
              </button>
              {sel === r.file && (
                <div className="border-t border-gray-200 p-2">
                  {loading && <div className="flex items-center gap-2 text-sm text-gray-500"><Spinner /> Loading transcript…</div>}
                  {!loading && msgs && <Transcript messages={msgs} />}
                </div>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

export default KitTranscripts
