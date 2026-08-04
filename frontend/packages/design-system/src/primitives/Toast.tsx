import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import { cx } from '../utils/cx'
import styles from './Toast.module.css'

export type ToastTone = 'success' | 'error' | 'info' | 'warning'

export interface Toast {
  id: string
  tone: ToastTone
  message: ReactNode
  /** Milliseconds before auto-dismiss. `null` keeps it until dismissed. */
  duration?: number | null
}

interface ToastContextValue {
  toasts: readonly Toast[]
  show: (toast: Omit<Toast, 'id'>) => string
  dismiss: (id: string) => void
}

const ToastContext = createContext<ToastContextValue | null>(null)

/**
 * Access the toast queue.
 *
 * ⚠️ A toast is for transient acknowledgement ("Plan saved"). It is the wrong
 * carrier for anything the user must act on or might need to re-read: it
 * disappears, it is easy to miss, and on a screen reader it interrupts. Errors
 * that need a decision belong in `ErrorState` or a `Modal`; validation errors
 * belong on the field (NFR-063).
 */
export function useToast(): ToastContextValue {
  const context = useContext(ToastContext)
  if (!context) {
    throw new Error(
      'useToast must be used inside <ToastProvider>. Add it once at the app root — ' +
        'the provider owns the queue and renders the live region.',
    )
  }
  return context
}

/** Default dwell times. Errors persist; acknowledgements do not need to. */
const DEFAULT_DURATION: Record<ToastTone, number | null> = {
  success: 4000,
  info: 5000,
  warning: 7000,
  error: null,
}

export interface ToastProviderProps {
  children: ReactNode
  /** Cap on simultaneous toasts. Beyond this, the oldest is dropped. */
  limit?: number
}

/**
 * ToastProvider — owns the queue and renders the live region.
 *
 * 🔒 The live region is rendered **once, always present and empty**, rather
 * than being created when a toast appears. A live region inserted into the DOM
 * at the same moment as its content is frequently not announced at all — the
 * screen reader has nothing to observe changing. This is the single most common
 * way toast implementations silently fail for assistive technology, and it is
 * invisible in manual testing with a mouse.
 */
export function ToastProvider({ children, limit = 3 }: ToastProviderProps) {
  const [toasts, setToasts] = useState<readonly Toast[]>([])
  const timers = useRef(new Map<string, ReturnType<typeof setTimeout>>())
  const counter = useRef(0)

  const dismiss = useCallback((id: string) => {
    setToasts((current) => current.filter((toast) => toast.id !== id))
    const timer = timers.current.get(id)
    if (timer) {
      clearTimeout(timer)
      timers.current.delete(id)
    }
  }, [])

  const show = useCallback(
    ({ tone, message, duration }: Omit<Toast, 'id'>) => {
      counter.current += 1
      const id = `toast-${counter.current}`
      const dwell = duration === undefined ? DEFAULT_DURATION[tone] : duration

      setToasts((current) => {
        const next = [...current, { id, tone, message, duration: dwell }]
        return next.length > limit ? next.slice(next.length - limit) : next
      })

      if (dwell !== null) {
        timers.current.set(
          id,
          setTimeout(() => dismiss(id), dwell),
        )
      }
      return id
    },
    [dismiss, limit],
  )

  // Clear pending timers on unmount so a dismissed provider cannot call
  // setState afterwards.
  useEffect(() => {
    const pending = timers.current
    return () => {
      for (const timer of pending.values()) clearTimeout(timer)
      pending.clear()
    }
  }, [])

  const value = useMemo(() => ({ toasts, show, dismiss }), [toasts, show, dismiss])

  return (
    <ToastContext.Provider value={value}>
      {children}
      <ToastRegion toasts={toasts} onDismiss={dismiss} />
    </ToastContext.Provider>
  )
}

interface ToastRegionProps {
  toasts: readonly Toast[]
  onDismiss: (id: string) => void
}

function ToastRegion({ toasts, onDismiss }: ToastRegionProps) {
  return (
    <div className={styles.region}>
      {/* 🔒 Two regions, because the politeness level must match the urgency
       * and cannot be changed after the fact. `assertive` interrupts what the
       * screen reader is currently saying — correct for an error, hostile for
       * "Saved". Both are always mounted so announcements are observed. */}
      <div aria-live="polite" aria-atomic="false" className={styles.liveRegion}>
        {toasts
          .filter((toast) => toast.tone !== 'error')
          .map((toast) => (
            <ToastItem key={toast.id} toast={toast} onDismiss={onDismiss} />
          ))}
      </div>
      <div aria-live="assertive" aria-atomic="false" className={styles.liveRegion}>
        {toasts
          .filter((toast) => toast.tone === 'error')
          .map((toast) => (
            <ToastItem key={toast.id} toast={toast} onDismiss={onDismiss} />
          ))}
      </div>
    </div>
  )
}

const TONE_LABEL: Record<ToastTone, string> = {
  success: 'Success',
  error: 'Error',
  warning: 'Warning',
  info: 'Information',
}

function ToastItem({ toast, onDismiss }: { toast: Toast; onDismiss: (id: string) => void }) {
  return (
    <div className={cx(styles.toast, styles[toast.tone])}>
      {/* 🔒 WCAG 1.4.1 — colour is never the only signal. The tone is stated
       * in text for screen readers, and carried by an icon for sighted users
       * who cannot distinguish the hues. */}
      <span className={styles.srOnly}>{TONE_LABEL[toast.tone]}:</span>
      <ToneIcon tone={toast.tone} />
      <div className={styles.message}>{toast.message}</div>
      <button
        type="button"
        onClick={() => onDismiss(toast.id)}
        className={styles.dismiss}
        aria-label="Dismiss notification"
      >
        <svg viewBox="0 0 24 24" width="16" height="16" fill="none" aria-hidden="true">
          <path d="M18 6 6 18M6 6l12 12" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
        </svg>
      </button>
    </div>
  )
}

function ToneIcon({ tone }: { tone: ToastTone }) {
  const path =
    tone === 'success'
      ? 'M20 6 9 17l-5-5'
      : tone === 'error'
        ? 'M18 6 6 18M6 6l12 12'
        : tone === 'warning'
          ? 'M12 9v4m0 4h.01M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z'
          : 'M12 16v-4m0-4h.01M22 12a10 10 0 1 1-20 0 10 10 0 0 1 20 0Z'

  return (
    <svg
      className={styles.icon}
      viewBox="0 0 24 24"
      width="18"
      height="18"
      fill="none"
      aria-hidden="true"
    >
      <path d={path} stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  )
}
