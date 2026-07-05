import { useEffect, useRef, useState } from 'react'

/**
 * EngineField — the DashboardV2 "Engine" panel visual: a slow particle
 * constellation on a single <canvas> with REAL system-activity captions
 * floating over it. Decorative, but honest — every caption the parent
 * passes in is derived from data already on the page (stream state, open
 * positions, latest scanner pick, latest signal) or a true static fact
 * about the system. No fabricated stats, ever.
 *
 * Performance contract (hard rules):
 *   • single canvas, one requestAnimationFrame loop, capped at ~30fps by
 *     frame-skipping (rAF fires at display rate; we only step + paint
 *     when ≥33ms have elapsed);
 *   • ZERO allocations inside the frame loop — particle state lives in
 *     Float32Arrays pre-allocated once per mount, the loop touches only
 *     scalars and canvas calls;
 *   • the loop STOPS COMPLETELY (cancelAnimationFrame, not an early
 *     return) whenever the tab is hidden (visibilitychange) or the panel
 *     is scrolled offscreen (IntersectionObserver);
 *   • devicePixelRatio-aware backing store, resized by ResizeObserver
 *     (dpr capped at 2 — retina glow doesn't need a 4× fill on 4k).
 *
 * prefers-reduced-motion — ONE static frame (particles + links, no
 * drift, no pulses) and the captions render as a static list instead of
 * floating. live=false — same static treatment, plus the field dims and
 * an "idle" chip appears (the parent passes polling-derived captions so
 * the list stays truthful while the stream is down).
 *
 * Captions are absolutely-positioned DOM elements over the canvas (not
 * canvas text) so they stay crisp at any dpr and inherit v2 tokens.
 */

export interface EngineFieldProps {
  /** Field height in px (default 220). */
  height?: number
  /** Approximate particle count (default 110, clamped 16–240). */
  density?: number
  /** Real activity captions, cycled in order over the field. */
  activity?: string[]
  /** false = stream down: static dimmed frame + captions as a list. */
  live?: boolean
  className?: string
}

type FloatingLabel = {
  id: number
  text: string
  leftPct: number
  topPct: number
  on: boolean
}

/** Snapshot of the simulation the caption spawner reads positions from. */
type Sim = {
  xs: Float32Array
  ys: Float32Array
  count: number
  w: number
  h: number
}

const FRAME_MS = 1000 / 30 // 30fps cap
const LINK_DIST = 96 // px — pairs closer than this get a line
const MAX_LINE_ALPHA = 0.3
const NODE_RADIUS = 1.6
const MAX_SPEED = 0.5 // px per (30fps) frame — slow institutional drift
const JITTER = 0.045 // Brownian acceleration per frame
const DAMPING = 0.985
const PULSE_MS = 950 // duration of one node bloom
const MAX_LABELS = 3
const LABEL_LIFE_MS = 3200 // fade-in + linger before fade-out starts
const LABEL_FADE_MS = 600
const LABEL_SPAWN_MS = 1500
const TAU = Math.PI * 2

let labelSeq = 0

export default function EngineField({
  height = 220,
  density = 110,
  activity = [],
  live = true,
  className,
}: EngineFieldProps) {
  const wrapRef = useRef<HTMLDivElement>(null)
  const inViewRef = useRef(true)  // shared with the caption spawner (review nit)
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const simRef = useRef<Sim | null>(null)
  // Captions change every poll cycle — read through a ref so a new array
  // identity never restarts the spawner/simulation effects.
  const activityRef = useRef<string[]>(activity)
  activityRef.current = activity

  const [reduced, setReduced] = useState(
    () => typeof window !== 'undefined'
      && window.matchMedia('(prefers-reduced-motion: reduce)').matches,
  )
  const [labels, setLabels] = useState<FloatingLabel[]>([])

  const animate = live && !reduced

  // Track the OS reduced-motion setting live (mirrors LiveNumber's JS check).
  useEffect(() => {
    const mq = window.matchMedia('(prefers-reduced-motion: reduce)')
    const onChange = () => setReduced(mq.matches)
    mq.addEventListener('change', onChange)
    return () => mq.removeEventListener('change', onChange)
  }, [])

  // ── Simulation + render loop ─────────────────────────────────────────
  useEffect(() => {
    const wrap = wrapRef.current
    const canvas = canvasRef.current
    if (!wrap || !canvas) return
    const ctx = canvas.getContext('2d')
    if (!ctx) return

    // v2 tokens resolved once per mount — accent lines, up-green pulses.
    const styles = getComputedStyle(wrap)
    const accent = styles.getPropertyValue('--v2-accent').trim() || '#4c8dff'
    const up = styles.getPropertyValue('--v2-up').trim() || '#0ecb81'

    // Pre-allocated particle state — never reallocated after this point.
    const count = Math.max(16, Math.min(240, Math.round(density)))
    const xs = new Float32Array(count)
    const ys = new Float32Array(count)
    const vxs = new Float32Array(count)
    const vys = new Float32Array(count)

    let w = 0
    let h = 0
    let seeded = false

    // Pulse state — scalars only.
    let pulseIdx = -1
    let pulseStart = 0
    let nextPulseAt = performance.now() + 1200 + Math.random() * 1800

    const seed = () => {
      for (let i = 0; i < count; i++) {
        xs[i] = Math.random() * w
        ys[i] = Math.random() * h
        vxs[i] = (Math.random() - 0.5) * MAX_SPEED
        vys[i] = (Math.random() - 0.5) * MAX_SPEED
      }
    }

    /** One frame. `withMotion=false` paints the static (reduced/idle) frame. */
    const draw = (now: number, withMotion: boolean) => {
      ctx.clearRect(0, 0, w, h)

      if (withMotion) {
        // Brownian drift: jitter + damping + speed clamp + soft wall bounce.
        for (let i = 0; i < count; i++) {
          let vx = (vxs[i] + (Math.random() - 0.5) * JITTER) * DAMPING
          let vy = (vys[i] + (Math.random() - 0.5) * JITTER) * DAMPING
          if (vx > MAX_SPEED) vx = MAX_SPEED
          else if (vx < -MAX_SPEED) vx = -MAX_SPEED
          if (vy > MAX_SPEED) vy = MAX_SPEED
          else if (vy < -MAX_SPEED) vy = -MAX_SPEED
          let x = xs[i] + vx
          let y = ys[i] + vy
          if (x < 0) { x = 0; vx = -vx } else if (x > w) { x = w; vx = -vx }
          if (y < 0) { y = 0; vy = -vy } else if (y > h) { y = h; vy = -vy }
          xs[i] = x; ys[i] = y; vxs[i] = vx; vys[i] = vy
        }
      }

      // Links — accent blue, alpha proportional to proximity.
      ctx.strokeStyle = accent
      ctx.lineWidth = 1
      const linkDist2 = LINK_DIST * LINK_DIST
      for (let i = 0; i < count; i++) {
        const xi = xs[i]
        const yi = ys[i]
        for (let j = i + 1; j < count; j++) {
          const dx = xs[j] - xi
          const dy = ys[j] - yi
          const d2 = dx * dx + dy * dy
          if (d2 >= linkDist2) continue
          ctx.globalAlpha = (1 - Math.sqrt(d2) / LINK_DIST) * MAX_LINE_ALPHA
          ctx.beginPath()
          ctx.moveTo(xi, yi)
          ctx.lineTo(xs[j], ys[j])
          ctx.stroke()
        }
      }

      // Nodes.
      ctx.fillStyle = accent
      ctx.globalAlpha = 0.75
      for (let i = 0; i < count; i++) {
        ctx.beginPath()
        ctx.arc(xs[i], ys[i], NODE_RADIUS, 0, TAU)
        ctx.fill()
      }

      // Pulse — brief up-green radius + glow bloom on one random node
      // every 2–4s. shadowBlur is expensive, so it's applied to exactly
      // one arc per frame, only while a pulse is in flight.
      if (withMotion) {
        if (pulseIdx < 0 && now >= nextPulseAt) {
          pulseIdx = Math.floor(Math.random() * count)
          pulseStart = now
        }
        if (pulseIdx >= 0) {
          const p = (now - pulseStart) / PULSE_MS
          if (p >= 1) {
            pulseIdx = -1
            nextPulseAt = now + 2000 + Math.random() * 2000
          } else {
            const bloom = Math.sin(p * Math.PI) // 0 → 1 → 0
            ctx.fillStyle = up
            ctx.shadowColor = up
            ctx.shadowBlur = 16 * bloom
            ctx.globalAlpha = 0.35 + 0.65 * bloom
            ctx.beginPath()
            ctx.arc(xs[pulseIdx], ys[pulseIdx], NODE_RADIUS + 3.2 * bloom, 0, TAU)
            ctx.fill()
            ctx.shadowBlur = 0
          }
        }
      }

      ctx.globalAlpha = 1
    }

    // ── Loop control: rAF runs ONLY while animating, tab visible AND
    //    the panel intersects the viewport. Otherwise it's cancelled.
    let raf = 0
    let lastTs = 0
    let docVisible = !document.hidden
    let inView = false

    const tick = (ts: number) => {
      raf = requestAnimationFrame(tick)
      if (ts - lastTs < FRAME_MS - 0.5) return // frame-skip → ~30fps cap
      lastTs = ts
      draw(ts, true)
    }

    const syncLoop = () => {
      const shouldRun = animate && docVisible && inView
      if (shouldRun && raf === 0) {
        lastTs = 0
        raf = requestAnimationFrame(tick)
      } else if (!shouldRun && raf !== 0) {
        cancelAnimationFrame(raf)
        raf = 0
      }
    }

    const onVisibility = () => {
      docVisible = !document.hidden
      syncLoop()
    }
    document.addEventListener('visibilitychange', onVisibility)

    const io = new IntersectionObserver(entries => {
      inView = entries[entries.length - 1]?.isIntersecting ?? false
      inViewRef.current = inView
      syncLoop()
    })
    io.observe(wrap)

    const resize = () => {
      const rect = wrap.getBoundingClientRect()
      w = Math.max(1, Math.floor(rect.width))
      h = Math.max(1, Math.floor(rect.height))
      const dpr = Math.min(2, window.devicePixelRatio || 1)
      canvas.width = Math.max(1, Math.round(w * dpr))
      canvas.height = Math.max(1, Math.round(h * dpr))
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
      if (!seeded) {
        seed()
        seeded = true
      } else {
        // Keep existing particles; pull any strays back inside new bounds.
        for (let i = 0; i < count; i++) {
          if (xs[i] > w) xs[i] = Math.random() * w
          if (ys[i] > h) ys[i] = Math.random() * h
        }
      }
      simRef.current = { xs, ys, count, w, h }
      // Static treatments have no loop — repaint the single frame here.
      if (!animate) draw(0, false)
    }
    const ro = new ResizeObserver(resize)
    ro.observe(wrap)
    resize()

    return () => {
      document.removeEventListener('visibilitychange', onVisibility)
      io.disconnect()
      ro.disconnect()
      if (raf !== 0) cancelAnimationFrame(raf)
      if (simRef.current !== null && simRef.current.xs === xs) simRef.current = null
    }
  }, [density, animate])

  // ── Floating caption spawner (animated mode only) ────────────────────
  useEffect(() => {
    if (!animate) {
      setLabels([])
      return
    }
    const timers = new Set<number>()
    const later = (fn: () => void, ms: number) => {
      const t = window.setTimeout(() => { timers.delete(t); fn() }, ms)
      timers.add(t)
    }
    let cursor = 0

    const spawn = () => {
      if (document.hidden) return
      if (!inViewRef.current) return  // offscreen: skip DOM label churn (review nit)
      const sim = simRef.current
      const lines = activityRef.current
      if (!sim || lines.length === 0) return
      const text = lines[cursor % lines.length]
      cursor += 1
      // Anchor the caption to a random node's current position.
      const node = Math.floor(Math.random() * sim.count)
      const leftPct = Math.min(86, Math.max(12, (sim.xs[node] / sim.w) * 100))
      const topPct = Math.min(84, Math.max(14, (sim.ys[node] / sim.h) * 100))
      const id = ++labelSeq
      // At most MAX_LABELS at once — if full, this cycle is skipped and the
      // scheduled timers below simply no-op against the missing id.
      setLabels(prev => (prev.length >= MAX_LABELS
        ? prev
        : [...prev, { id, text, leftPct, topPct, on: false }]))
      later(() => setLabels(p => p.map(l => (l.id === id ? { ...l, on: true } : l))), 40)
      later(() => setLabels(p => p.map(l => (l.id === id ? { ...l, on: false } : l))), LABEL_LIFE_MS)
      later(() => setLabels(p => p.filter(l => l.id !== id)), LABEL_LIFE_MS + LABEL_FADE_MS)
    }

    spawn() // first caption without the initial interval wait
    const interval = window.setInterval(spawn, LABEL_SPAWN_MS)
    return () => {
      window.clearInterval(interval)
      timers.forEach(t => window.clearTimeout(t))
      setLabels([])
    }
  }, [animate])

  // Reduced-motion or idle: captions as a static list (no floaters).
  const showStaticList = !animate && activity.length > 0

  return (
    <div
      ref={wrapRef}
      className={`v2-engine${live ? '' : ' v2-engine--idle'}${className ? ` ${className}` : ''}`}
      style={{ height }}
      aria-hidden="true"
    >
      <canvas ref={canvasRef} className="v2-engine__canvas" />
      {animate && labels.map(l => (
        <span
          key={l.id}
          className={`v2-engine__label${l.on ? ' v2-engine__label--on' : ''}`}
          style={{ left: `${l.leftPct}%`, top: `${l.topPct}%` }}
        >
          {l.text}
        </span>
      ))}
      {showStaticList && (
        <ul className="v2-engine__list">
          {activity.slice(0, 5).map((line, i) => <li key={`${i}-${line}`}>{line}</li>)}
        </ul>
      )}
      {!live && <span className="v2-engine__idle">idle — stream offline</span>}
    </div>
  )
}
