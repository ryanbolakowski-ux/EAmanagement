import { useEffect, useRef, useState } from 'react'

type Candle = { time: number; open: number; high: number; low: number; close: number }
type Marker = { time: number; type: 'entry' | 'exit'; direction: string; price: number; is_winner: boolean }

interface Props {
  candles: Candle[]
  markers: Marker[]
  height?: number
}

export default function CandlestickChart({ candles, markers, height = 400 }: Props) {
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const containerRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!canvasRef.current || !containerRef.current || candles.length === 0) return
    const canvas = canvasRef.current
    const ctx = canvas.getContext('2d')
    if (!ctx) return

    const w = containerRef.current.clientWidth
    const h = height
    canvas.width = w * 2
    canvas.height = h * 2
    canvas.style.width = w + 'px'
    canvas.style.height = h + 'px'
    ctx.scale(2, 2)

    // Build a time-to-index map for all candles
    const timeMap = new Map<number, number>()
    for (let i = 0; i < candles.length; i++) {
      timeMap.set(candles[i].time, i)
    }

    // Find which candle indices have markers
    const markerIndices: number[] = []
    for (const m of markers) {
      const exact = timeMap.get(m.time)
      if (exact !== undefined) {
        markerIndices.push(exact)
      } else {
        // Find closest candle
        let closest = 0, minDiff = Infinity
        for (let i = 0; i < candles.length; i++) {
          const diff = Math.abs(candles[i].time - m.time)
          if (diff < minDiff) { minDiff = diff; closest = i }
        }
        markerIndices.push(closest)
      }
    }

    // Determine visible range: show candles that contain trades with padding
    let startIdx = 0, endIdx = candles.length - 1
    if (markerIndices.length > 0) {
      const minMarker = Math.min(...markerIndices)
      const maxMarker = Math.max(...markerIndices)
      const tradeSpan = maxMarker - minMarker
      const padding = Math.max(20, Math.floor(tradeSpan * 0.1))
      startIdx = Math.max(0, minMarker - padding)
      endIdx = Math.min(candles.length - 1, maxMarker + padding)
      // Ensure we show at least 50 candles for context
      if (endIdx - startIdx < 50) {
        const center = Math.floor((minMarker + maxMarker) / 2)
        startIdx = Math.max(0, center - 25)
        endIdx = Math.min(candles.length - 1, center + 25)
      }
      // Cap at 600 candles max for performance
      if (endIdx - startIdx > 600) {
        endIdx = startIdx + 600
      }
    } else {
      // No markers, show last 200
      startIdx = Math.max(0, candles.length - 200)
    }

    const data = candles.slice(startIdx, endIdx + 1)
    const indexOffset = startIdx

    const pad = { top: 20, bottom: 30, left: 65, right: 20 }
    const chartW = w - pad.left - pad.right
    const chartH = h - pad.top - pad.bottom

    // Find price range
    let minP = Infinity, maxP = -Infinity
    for (const c of data) {
      if (c.low < minP) minP = c.low
      if (c.high > maxP) maxP = c.high
    }
    // Also include marker prices in range
    for (const m of markers) {
      if (m.price < minP) minP = m.price
      if (m.price > maxP) maxP = m.price
    }
    const range = maxP - minP || 1
    minP -= range * 0.05
    maxP += range * 0.05
    const priceRange = maxP - minP

    const toX = (i: number) => pad.left + ((i + 0.5) / data.length) * chartW
    const toY = (p: number) => pad.top + (1 - (p - minP) / priceRange) * chartH
    const candleW = Math.max(1, Math.min(12, (chartW / data.length) * 0.7))

    // Background
    ctx.fillStyle = '#0f172a'
    ctx.fillRect(0, 0, w, h)

    // Grid lines
    ctx.strokeStyle = '#1e293b'
    ctx.lineWidth = 0.5
    const priceStep = priceRange / 6
    for (let i = 0; i <= 6; i++) {
      const p = minP + i * priceStep
      const y = toY(p)
      ctx.beginPath()
      ctx.moveTo(pad.left, y)
      ctx.lineTo(w - pad.right, y)
      ctx.stroke()
      ctx.fillStyle = '#64748b'
      ctx.font = '10px sans-serif'
      ctx.textAlign = 'right'
      ctx.fillText(p.toFixed(2), pad.left - 5, y + 3)
    }

    // Draw candles
    for (let i = 0; i < data.length; i++) {
      const c = data[i]
      const x = toX(i)
      const isGreen = c.close >= c.open
      const color = isGreen ? '#22c55e' : '#ef4444'

      // Wick
      ctx.strokeStyle = color
      ctx.lineWidth = 1
      ctx.beginPath()
      ctx.moveTo(x, toY(c.high))
      ctx.lineTo(x, toY(c.low))
      ctx.stroke()

      // Body
      const bodyTop = toY(Math.max(c.open, c.close))
      const bodyBot = toY(Math.min(c.open, c.close))
      const bodyH = Math.max(1, bodyBot - bodyTop)
      ctx.fillStyle = color
      ctx.fillRect(x - candleW / 2, bodyTop, candleW, bodyH)
    }

    // Draw trade markers — pair entries with exits
    const entries: { m: Marker; idx: number }[] = []
    const exits: { m: Marker; idx: number }[] = []

    for (const m of markers) {
      // Find index within visible data
      const globalIdx = timeMap.get(m.time)
      let localIdx: number
      if (globalIdx !== undefined) {
        localIdx = globalIdx - indexOffset
      } else {
        // Closest match within visible data
        let closest = 0, minDiff = Infinity
        for (let i = 0; i < data.length; i++) {
          const diff = Math.abs(data[i].time - m.time)
          if (diff < minDiff) { minDiff = diff; closest = i }
        }
        localIdx = closest
      }

      // Skip if outside visible range
      if (localIdx < 0 || localIdx >= data.length) continue

      const x = toX(localIdx)
      const y = toY(m.price)

      if (m.type === 'entry') {
        entries.push({ m, idx: localIdx })
        const size = 7
        ctx.fillStyle = m.direction === 'long' ? '#3b82f6' : '#f59e0b'
        ctx.beginPath()
        if (m.direction === 'long') {
          // Up triangle below the candle low
          const belowY = toY(data[localIdx].low) + size + 4
          ctx.moveTo(x, belowY - size * 2)
          ctx.lineTo(x - size, belowY)
          ctx.lineTo(x + size, belowY)
        } else {
          // Down triangle above the candle high
          const aboveY = toY(data[localIdx].high) - size - 4
          ctx.moveTo(x, aboveY + size * 2)
          ctx.lineTo(x - size, aboveY)
          ctx.lineTo(x + size, aboveY)
        }
        ctx.closePath()
        ctx.fill()
      } else {
        exits.push({ m, idx: localIdx })
        // Exit marker: small square like TradingView
        const sz = 4
        const exitColor = m.is_winner ? '#22c55e' : '#ef4444'
        if (m.direction === 'long') {
          // Exit long = sold, show above candle high
          const aboveY = toY(data[localIdx].high) - sz - 6
          ctx.fillStyle = exitColor
          ctx.fillRect(x - sz, aboveY - sz, sz * 2, sz * 2)
          ctx.strokeStyle = '#fff'
          ctx.lineWidth = 1
          ctx.strokeRect(x - sz, aboveY - sz, sz * 2, sz * 2)
        } else {
          // Exit short = bought back, show below candle low
          const belowY = toY(data[localIdx].low) + sz + 6
          ctx.fillStyle = exitColor
          ctx.fillRect(x - sz, belowY - sz, sz * 2, sz * 2)
          ctx.strokeStyle = '#fff'
          ctx.lineWidth = 1
          ctx.strokeRect(x - sz, belowY - sz, sz * 2, sz * 2)
        }
      }
    }

    // Draw dashed lines connecting entry to exit
    for (let i = 0; i < entries.length && i < exits.length; i++) {
      const e = entries[i]
      const ex = exits[i]
      const eX = toX(e.idx)
      const eY = toY(e.m.price)
      const exX = toX(ex.idx)
      const exY = toY(ex.m.price)
      ctx.strokeStyle = ex.m.is_winner ? 'rgba(34,197,94,0.3)' : 'rgba(239,68,68,0.3)'
      ctx.lineWidth = 1
      ctx.setLineDash([3, 3])
      ctx.beginPath()
      ctx.moveTo(eX, eY)
      ctx.lineTo(exX, exY)
      ctx.stroke()
      ctx.setLineDash([])
    }

  }, [candles, markers, height])

  if (candles.length === 0) {
    return <div className="flex items-center justify-center h-64 bg-slate-900 rounded-lg text-slate-500 text-sm">No chart data available</div>
  }

  return (
    <div ref={containerRef} className="w-full bg-slate-900 rounded-lg overflow-hidden">
      <canvas ref={canvasRef} className="w-full cursor-crosshair"/>
      <div className="flex items-center gap-4 px-4 py-2 bg-slate-800 text-xs text-slate-400">
        <span className="flex items-center gap-1"><span className="w-3 h-3 bg-blue-500" style={{clipPath: 'polygon(50% 0%, 0% 100%, 100% 100%)'}}></span> Long Entry</span>
        <span className="flex items-center gap-1"><span className="w-3 h-3 bg-amber-500" style={{clipPath: 'polygon(0% 0%, 100% 0%, 50% 100%)'}}></span> Short Entry</span>
        <span className="flex items-center gap-1"><span className="w-3 h-3 bg-green-500"></span> Win</span>
        <span className="flex items-center gap-1"><span className="w-3 h-3 bg-red-500"></span> Loss</span>
      </div>
    </div>
  )
}
