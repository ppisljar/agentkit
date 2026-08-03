/**
 * Small presentational primitives shared by the kit's panels.
 *
 * Tailwind classes only, no component library — the host app's Tailwind build picks these up and
 * they inherit its theme. Nothing here knows about claudeKit's domain.
 */

import React from 'react'

export const cx = (...c: (string | false | null | undefined)[]) => c.filter(Boolean).join(' ')

export function Card({ title, subtitle, right, children, className }: {
  title?: React.ReactNode
  subtitle?: React.ReactNode
  right?: React.ReactNode
  children?: React.ReactNode
  className?: string
}) {
  return (
    <div className={cx('rounded-xl border border-gray-200 bg-white shadow-sm', className)}>
      {(title || right) && (
        <div className="flex items-start justify-between gap-4 border-b border-gray-100 px-4 py-3">
          <div className="min-w-0">
            {title && <div className="font-semibold text-gray-900">{title}</div>}
            {subtitle && <div className="mt-0.5 text-sm text-gray-500">{subtitle}</div>}
          </div>
          {right && <div className="flex shrink-0 items-center gap-2">{right}</div>}
        </div>
      )}
      <div className="px-4 py-3">{children}</div>
    </div>
  )
}

type BtnProps = React.ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: 'primary' | 'default' | 'danger' | 'ghost'
  loading?: boolean
}

export function Button({ variant = 'default', loading, className, children, disabled, ...rest }: BtnProps) {
  const base = 'inline-flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-sm font-medium transition disabled:opacity-50 disabled:cursor-not-allowed'
  const styles = {
    primary: 'bg-blue-600 text-white hover:bg-blue-700',
    default: 'border border-gray-300 bg-white text-gray-700 hover:bg-gray-50',
    danger: 'bg-red-600 text-white hover:bg-red-700',
    ghost: 'text-gray-600 hover:bg-gray-100',
  }[variant]
  return (
    <button className={cx(base, styles, className)} disabled={disabled || loading} {...rest}>
      {loading && <Spinner />}
      {children}
    </button>
  )
}

export function Spinner({ className }: { className?: string }) {
  return (
    <svg className={cx('h-3.5 w-3.5 animate-spin', className)} viewBox="0 0 24 24" fill="none" aria-hidden>
      <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
      <path className="opacity-75" fill="currentColor"
        d="M4 12a8 8 0 018-8V0C5.4 0 0 5.4 0 12h4z" />
    </svg>
  )
}

const TONES: Record<string, string> = {
  ok: 'bg-green-100 text-green-800',
  done: 'bg-green-100 text-green-800',
  approved: 'bg-green-100 text-green-800',
  active: 'bg-green-100 text-green-800',
  warn: 'bg-amber-100 text-amber-800',
  running: 'bg-blue-100 text-blue-800',
  open: 'bg-amber-100 text-amber-800',
  answered: 'bg-blue-100 text-blue-800',
  fail: 'bg-red-100 text-red-800',
  error: 'bg-red-100 text-red-800',
  rejected: 'bg-red-100 text-red-800',
  interrupted: 'bg-orange-100 text-orange-800',
  info: 'bg-gray-100 text-gray-700',
  never: 'bg-gray-100 text-gray-500',
}

export function Badge({ tone, children }: { tone?: string; children: React.ReactNode }) {
  return (
    <span className={cx('rounded-full px-2 py-0.5 text-xs font-medium',
      TONES[tone || 'info'] || TONES.info)}>
      {children}
    </span>
  )
}

export function Toggle({ checked, onChange, label, disabled }: {
  checked: boolean
  onChange: (v: boolean) => void
  label?: string
  disabled?: boolean
}) {
  return (
    <label className={cx('inline-flex cursor-pointer items-center gap-2', disabled && 'cursor-not-allowed opacity-50')}>
      <span className="relative inline-block h-5 w-9">
        <input type="checkbox" className="peer sr-only" checked={checked} disabled={disabled}
          onChange={(e) => onChange(e.target.checked)} />
        <span className="absolute inset-0 rounded-full bg-gray-300 transition peer-checked:bg-blue-600" />
        <span className="absolute left-0.5 top-0.5 h-4 w-4 rounded-full bg-white transition peer-checked:translate-x-4" />
      </span>
      {label && <span className="text-sm text-gray-700">{label}</span>}
    </label>
  )
}

export function ErrorNote({ error, onDismiss }: { error?: string | null; onDismiss?: () => void }) {
  if (!error) return null
  return (
    <div className="mb-3 flex items-start justify-between gap-3 rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-800">
      <span className="whitespace-pre-wrap break-words">{error}</span>
      {onDismiss && (
        <button onClick={onDismiss} className="shrink-0 text-red-500 hover:text-red-700" aria-label="Dismiss">×</button>
      )}
    </div>
  )
}

export function Empty({ children }: { children: React.ReactNode }) {
  return <div className="py-8 text-center text-sm text-gray-500">{children}</div>
}

/** Monospace log/output box that keeps its scroll pinned to the bottom while content streams. */
export function LogBox({ text, className }: { text?: string | null; className?: string }) {
  const ref = React.useRef<HTMLPreElement>(null)
  const pinned = React.useRef(true)

  React.useEffect(() => {
    const el = ref.current
    if (el && pinned.current) el.scrollTop = el.scrollHeight
  }, [text])

  if (!text) return null
  return (
    <pre
      ref={ref}
      onScroll={(e) => {
        const el = e.currentTarget
        pinned.current = el.scrollHeight - el.scrollTop - el.clientHeight < 40
      }}
      className={cx('max-h-72 overflow-auto rounded-lg bg-gray-900 p-3 text-xs leading-relaxed text-gray-100',
        className)}
    >
      {text}
    </pre>
  )
}
