/**
 * DashboardV2 — STAGE 2b of the V2 "institutional terminal" redesign.
 *
 * Information-dense but calm: a stat strip up top, market context (daily
 * bias regime), the Theta Scanner's latest pick, a per-strategy health
 * board, live open positions and a recent-activity feed. Every panel is
 * isolated in its own ErrorBoundary and renders Skeleton → EmptyState →
 * data, so one bad payload can never blank the whole terminal.
 *
 * Data comes exclusively from api/endpoints.ts:
 *   dashboardApi.summary / dashboardApi.bias
 *   liveTradingApi.portfolioSummary / liveTradingApi.listSessions
 *   paperTradingApi.listSessions
 *   scannerApi.history
 *   strategiesApi.list
 *   tradesApi.list / tradesApi.openPositions
 *
 * Polling follows the existing react-query refetchInterval pattern (see
 * Dashboard.tsx / PaperTrading.tsx), with staleTime explicitly set BELOW
 * each refetchInterval so remounts inside the window serve cache instead
 * of stampeding the API, while the interval still guarantees freshness.
 *
 * Live updates: when the SSE stream (hooks/useEventStream.ts →
 * GET /api/v1/stream/dashboard) is connected, its positions/pnl/signals
 * events are written into the react-query cache and the three matching
 * refetchIntervals pause; on disconnect polling resumes automatically.
 *
 * Styling: v2.css tokens/classes for ALL visual treatment; Tailwind is
 * used for layout-only utilities (grid/flex/gap/padding) to match how the
 * rest of the codebase composes pages. Namespaced under .v2-root so V1
 * screens are untouched.
 */
import { useEffect, useMemo } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Activity, Briefcase, Compass, Cpu, Crosshair, LayoutGrid,
} from 'lucide-react'
import {
  dashboardApi, liveTradingApi, paperTradingApi, scannerApi, strategiesApi, tradesApi,
  type DailyBias, type LiveSessionRow, type ScannerPick,
} from '../../api/endpoints'
import type { DashboardSummary, Strategy, Trade } from '../../types'
import {
  EmptyState, EngineField, ErrorBoundary, LiveNumber, SectionHeader, Skeleton, Sparkline, StatCard,
} from '../../components/v2'
import { fmtEntryTime, fmtHold } from '../../components/TradeMetrics'
import { useEventStream } from '../../hooks/useEventStream'

/** Named SSE events the dashboard stream emits — see backend
 *  app/api/routes/stream.py. Each payload is byte-compatible with the REST
 *  endpoint the matching query polls, so it can be written straight into
 *  the react-query cache. */
const STREAM_EVENTS = ['positions', 'pnl', 'signals'] as const

// ─────────────────────────────────────────────────────────────────────────────
// Local payload types — these endpoints return untyped axios responses in
// endpoints.ts; shapes verified against the backend route handlers.
// ─────────────────────────────────────────────────────────────────────────────

/** GET /api/v1/live-trading/portfolio-summary (subset this page reads) */
type PortfolioSummary = {
  total_equity: number
  today_pnl: number
  today_unrealized_pnl: number
  total_unrealized_pnl: number
  open_positions_count: number
  accounts_count: number
  healthy_accounts: number
  equity_curve_14d: { d: string | null; pnl: number }[]
}

/** GET /api/v1/paper-trading/sessions row (SessionResponse) */
type PaperSession = {
  id: string
  strategy_id: string
  strategy_name: string
  mode: string
  is_active: boolean
  started_at: string
  instrument: string | null
  label: string | null
  total_trades: number
  wins: number
  losses: number
  net_pnl: number
}

/** GET /api/v1/trades/open-positions row (paper runner in-memory position) */
type OpenPosition = {
  session_id: string
  instrument: string
  direction: string
  entry_price: number
  stop_loss: number
  take_profit: number
  contracts: number
  entry_time: string | null
  current_price: number
  unrealized_pnl: number
  status: string
}

// ─────────────────────────────────────────────────────────────────────────────
// Formatters + semantic-tone helpers (v2-num / v2-up / v2-down conventions)
// ─────────────────────────────────────────────────────────────────────────────

const fmtUsd = (v: number, dp = 0) =>
  `${v < 0 ? '−' : ''}$${Math.abs(v).toLocaleString('en-US', { minimumFractionDigits: dp, maximumFractionDigits: dp })}`

const fmtSignedUsd = (v: number, dp = 0) =>
  `${v > 0 ? '+' : v < 0 ? '−' : ''}$${Math.abs(v).toLocaleString('en-US', { minimumFractionDigits: dp, maximumFractionDigits: dp })}`

const fmtPx = (v: number | null | undefined) =>
  v == null ? '—' : v.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })

const pnlTone = (v: number) => (v > 0 ? 'v2-up' : v < 0 ? 'v2-down' : 'v2-flat')

const BIAS_META: Record<DailyBias['bias'], { label: string; badge: string }> = {
  strong_bullish: { label: 'Strong Bullish', badge: 'v2-badge--up' },
  bullish:        { label: 'Bullish',        badge: 'v2-badge--up' },
  neutral:        { label: 'Neutral',        badge: 'v2-badge--neutral' },
  bearish:        { label: 'Bearish',        badge: 'v2-badge--down' },
  strong_bearish: { label: 'Strong Bearish', badge: 'v2-badge--down' },
}

const REGIME_SCORE: Record<DailyBias['bias'], number> = {
  strong_bullish: 2, bullish: 1, neutral: 0, bearish: -1, strong_bearish: -2,
}

const SESSION_LABEL: Record<string, string> = {
  asian: 'Asian', london: 'London', ny: 'New York', overnight: 'Overnight', unknown: '—',
}

/** Roll the per-instrument biases up into one regime badge for the header. */
function computeRegime(biases: DailyBias[]): { label: string; badge: string } {
  const score = biases.reduce((acc, b) => acc + (REGIME_SCORE[b.bias] ?? 0), 0)
  if (score >= 2)  return { label: 'Risk-on regime',  badge: 'v2-badge--up' }
  if (score <= -2) return { label: 'Risk-off regime', badge: 'v2-badge--down' }
  return { label: 'Mixed regime', badge: 'v2-badge--neutral' }
}

// ─────────────────────────────────────────────────────────────────────────────
// 1. Top strip — equity / day P&L / open positions / win rate / paper P&L
// ─────────────────────────────────────────────────────────────────────────────

function StatStrip({ summary, summaryLoading, portfolio, portfolioFailed, paperOpenCount }: {
  summary: DashboardSummary | undefined
  summaryLoading: boolean
  portfolio: PortfolioSummary | undefined
  portfolioFailed: boolean
  paperOpenCount: number
}) {
  if (summaryLoading && !summary) {
    return (
      <div className="grid grid-cols-2 lg:grid-cols-5 gap-3">
        {Array.from({ length: 5 }, (_, i) => <Skeleton key={i} variant="card" />)}
      </div>
    )
  }

  const paperPnl = summary?.paper_trading.net_pnl ?? 0
  const paperTrades = summary?.paper_trading.total_trades ?? 0
  const liveTrades = summary?.live_trading.total_trades ?? 0
  const totalTrades = paperTrades + liveTrades
  // Blended win rate weighted by trade count, same math as V1 Dashboard.tsx
  const blendedWinRate = totalTrades > 0
    ? (((summary?.paper_trading.win_rate ?? 0) * paperTrades
        + (summary?.live_trading.win_rate ?? 0) * liveTrades) / totalTrades) * 100
    : 0

  const dayPnl = (portfolio?.today_pnl ?? 0) + (portfolio?.today_unrealized_pnl ?? 0)
  const liveOpenCount = portfolio?.open_positions_count ?? 0
  const openCount = paperOpenCount + liveOpenCount

  // Cumulative 14-day live equity curve for the sparkline (daily pnl → running sum)
  const equitySpark = (portfolio?.equity_curve_14d ?? []).reduce<number[]>((acc, p) => {
    acc.push((acc[acc.length - 1] ?? 0) + (p.pnl ?? 0))
    return acc
  }, [])

  return (
    <div className="grid grid-cols-2 lg:grid-cols-5 gap-3">
      <StatCard
        label="Equity"
        value={portfolio ? <LiveNumber value={portfolio.total_equity} format={v => fmtUsd(v)} /> : '—'}
        hint={portfolio
          ? `${portfolio.accounts_count} broker account${portfolio.accounts_count === 1 ? '' : 's'}`
          : portfolioFailed ? 'no broker linked' : 'loading…'}
        sparkline={equitySpark.length > 1 ? equitySpark : undefined}
      />
      <StatCard
        label="Day P&L (live)"
        value={
          <span className={pnlTone(dayPnl)}>
            <LiveNumber value={dayPnl} format={v => fmtSignedUsd(v)} />
          </span>
        }
        hint={portfolio
          ? `realized ${fmtSignedUsd(portfolio.today_pnl)} · open ${fmtSignedUsd(portfolio.today_unrealized_pnl)}`
          : 'live accounts only'}
      />
      <StatCard
        label="Open positions"
        value={<LiveNumber value={openCount} format={v => String(Math.round(v))} />}
        hint={`paper ${paperOpenCount} · live ${liveOpenCount}`}
      />
      <StatCard
        label="Win rate"
        value={<LiveNumber value={blendedWinRate} format={v => `${v.toFixed(1)}%`} />}
        hint={`${totalTrades} closed trade${totalTrades === 1 ? '' : 's'} · blended`}
      />
      <StatCard
        label="Paper P&L"
        value={
          <span className={pnlTone(paperPnl)}>
            <LiveNumber value={paperPnl} format={v => fmtSignedUsd(v)} />
          </span>
        }
        hint={`${paperTrades} trade${paperTrades === 1 ? '' : 's'} lifetime`}
      />
    </div>
  )
}

// ─────────────────────────────────────────────────────────────────────────────
// 2. Market context — daily bias per instrument + rolled-up regime badge
// ─────────────────────────────────────────────────────────────────────────────

function MarketContextCard({ biases, isLoading, isError }: {
  biases: DailyBias[]
  isLoading: boolean
  isError: boolean
}) {
  if (isLoading) return <Skeleton variant="card" height={236} className="h-full" />
  if (isError) {
    return (
      <EmptyState
        icon={Compass}
        title="Bias engine unavailable"
        hint="Daily bias could not be loaded — it refreshes automatically every 5 minutes."
        className="h-full"
      />
    )
  }
  if (biases.length === 0) {
    return (
      <EmptyState
        icon={Compass}
        title="No bias data yet"
        hint="ES / NQ / RTY / YM daily bias appears here once the first candles land."
        className="h-full"
      />
    )
  }

  const regime = computeRegime(biases)
  const session = biases[0]?.current_session ?? 'unknown'

  return (
    <div className="v2-card p-4 h-full">
      <SectionHeader
        title="Market context"
        subtitle="EMA(9/21) daily bias · refreshes every 5 min"
        icon={Compass}
        actions={
          <>
            <span className={`v2-badge ${regime.badge}`}>{regime.label}</span>
            <span className="v2-badge v2-badge--neutral">{SESSION_LABEL[session] ?? session} session</span>
          </>
        }
      />
      <div className="overflow-x-auto">
        <table className="v2-table">
          <thead>
            <tr>
              <th>Instrument</th>
              <th className="v2-num">Last</th>
              <th>Bias</th>
              <th className="v2-num">Strength</th>
              <th>Draw target</th>
            </tr>
          </thead>
          <tbody>
            {biases.map(b => {
              const meta = BIAS_META[b.bias] ?? BIAS_META.neutral
              const strength = b.strength_pct ?? 0
              return (
                <tr key={b.instrument}>
                  <td className="font-semibold">{b.instrument}</td>
                  <td className="v2-num">{fmtPx(b.last_close)}</td>
                  <td><span className={`v2-badge ${meta.badge}`}>{meta.label}</span></td>
                  <td className={`v2-num ${pnlTone(strength)}`}>
                    {`${strength > 0 ? '+' : strength < 0 ? '−' : ''}${Math.abs(strength).toFixed(2)}%`}
                  </td>
                  <td className="v2-type-caption">
                    {b.draw_target ? `${b.draw_target.label} · ${fmtPx(b.draw_target.level)}` : '—'}
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
    </div>
  )
}

// ─────────────────────────────────────────────────────────────────────────────
// 3. Saro — Today's Pick — latest Theta Scanner pick (last 24h) with outcome status
// ─────────────────────────────────────────────────────────────────────────────

const OUTCOME_BADGE: Record<string, { label: string; cls: string }> = {
  win:     { label: 'Win',     cls: 'v2-badge--up' },
  loss:    { label: 'Loss',    cls: 'v2-badge--down' },
  expired: { label: 'Expired', cls: 'v2-badge--neutral' },
}

function TodaysPickCard({ pick, isLoading, isError }: {
  pick: ScannerPick | null
  isLoading: boolean
  isError: boolean
}) {
  if (isLoading) return <Skeleton variant="card" height={236} className="h-full" />
  if (isError) {
    return (
      <EmptyState
        icon={Crosshair}
        title="Scanner unavailable"
        hint="Pick history could not be loaded right now."
        className="h-full"
      />
    )
  }
  if (!pick) {
    return (
      <EmptyState
        icon={Crosshair}
        title="No pick yet today"
        hint="Saro posts one qualifying setup each morning. Nothing has qualified in the last 24 hours."
        className="h-full"
      />
    )
  }

  const status = pick.outcome
    ? (OUTCOME_BADGE[pick.outcome] ?? { label: pick.outcome, cls: 'v2-badge--neutral' })
    : { label: 'Open', cls: 'v2-badge--accent' }
  const isUp = /long|call|bull/i.test(pick.direction ?? '')

  const levels: { label: string; value: number | null }[] = [
    { label: 'Entry',  value: pick.entry },
    { label: 'Stop',   value: pick.stop },
    { label: 'Target', value: pick.target },
  ]

  return (
    <div className="v2-card p-4 h-full flex flex-col">
      <SectionHeader
        title="Today's Pick"
        subtitle={fmtEntryTime(pick.picked_at)}
        icon={Crosshair}
        actions={
          <span className={`v2-badge ${status.cls} v2-num`}>
            {status.label}
            {pick.outcome != null && pick.outcome_pct != null
              ? ` ${pick.outcome_pct > 0 ? '+' : ''}${pick.outcome_pct.toFixed(1)}%`
              : ''}
          </span>
        }
      />
      <div className="flex items-center gap-2 mb-3">
        <span className="v2-type-title">{pick.ticker}</span>
        <span className={`v2-badge ${isUp ? 'v2-badge--up' : 'v2-badge--down'}`}>{pick.direction}</span>
        <span className="v2-badge v2-badge--neutral">{pick.asset_type}</span>
        {pick.score != null && (
          <span className="v2-type-caption v2-num ml-auto">score {pick.score.toFixed(1)}</span>
        )}
      </div>
      <div className="grid grid-cols-3 gap-2">
        {levels.map(l => (
          <div key={l.label} className="v2-well px-3 py-2">
            <div className="v2-type-micro">{l.label}</div>
            <div className="v2-num v2-type-heading">{fmtPx(l.value)}</div>
          </div>
        ))}
      </div>
      {pick.catalyst_reason && (
        <p className="v2-type-caption mt-3 line-clamp-2">{pick.catalyst_reason}</p>
      )}
    </div>
  )
}

// ─────────────────────────────────────────────────────────────────────────────
// 4. Strategy health board — one compact row per active strategy
// ─────────────────────────────────────────────────────────────────────────────

function StrategyHealthBoard({ strategies, isLoading, isError, paperSessions, liveSessions, trades }: {
  strategies: Strategy[]
  isLoading: boolean
  isError: boolean
  paperSessions: PaperSession[]
  liveSessions: LiveSessionRow[]
  trades: Trade[]
}) {
  if (isLoading) return <Skeleton variant="table" rows={3} cols={5} />
  if (isError) {
    return (
      <EmptyState
        icon={LayoutGrid}
        title="Strategies unavailable"
        hint="The strategy list could not be loaded right now."
      />
    )
  }

  const active = strategies.filter(s => s.status === 'active')
  if (active.length === 0) {
    return (
      <EmptyState
        icon={LayoutGrid}
        title="No active strategies"
        hint="Activate a strategy to see its session state and recent P&L here."
      />
    )
  }

  const tradeTs = (t: Trade) => new Date(t.exit_time ?? t.entry_time ?? 0).getTime()

  return (
    <div className="v2-card p-4">
      <SectionHeader
        title="Strategy health"
        subtitle={`${active.length} active strateg${active.length === 1 ? 'y' : 'ies'}`}
        icon={LayoutGrid}
      />
      <div className="overflow-x-auto">
        <table className="v2-table">
          <thead>
            <tr>
              <th>Strategy</th>
              <th>Mode</th>
              <th>Session</th>
              <th className="v2-num">Recent P&L</th>
              <th>Trend</th>
            </tr>
          </thead>
          <tbody>
            {active.map(s => {
              const activePaper = paperSessions.filter(ps => ps.strategy_id === s.id && ps.is_active)
              const activeLive = liveSessions.filter(ls => ls.strategy_id === s.id && ls.is_active)
              const isIdle = activePaper.length === 0 && activeLive.length === 0

              // Instruments the running sessions are attached to (deduped)
              const instruments = Array.from(new Set(
                [...activeLive.map(l => l.instrument), ...activePaper.map(p => p.instrument)]
                  .filter((i): i is string => !!i)
              ))

              // Cumulative recent P&L curve from this strategy's closed trades
              // in the fetched window (newest 200 overall, sorted oldest-first)
              const closed = trades
                .filter(t => t.strategy_id === s.id && t.status === 'closed' && t.net_pnl != null)
                .sort((a, b) => tradeTs(a) - tradeTs(b))
              let run = 0
              const curve = closed.map(t => { run += t.net_pnl ?? 0; return Number(run.toFixed(2)) })
              const recentPnl = curve.length > 0 ? curve[curve.length - 1] : 0

              return (
                <tr key={s.id}>
                  <td className="font-semibold whitespace-nowrap">{s.name}</td>
                  <td>
                    <span className="inline-flex items-center gap-1">
                      {activeLive.length > 0 && <span className="v2-badge v2-badge--accent">live</span>}
                      {activePaper.length > 0 && <span className="v2-badge v2-badge--neutral">paper</span>}
                      {isIdle && <span className="v2-badge v2-badge--neutral">idle</span>}
                    </span>
                  </td>
                  <td className="v2-type-caption whitespace-nowrap">
                    {isIdle
                      ? 'no running session'
                      : `${activeLive.length + activePaper.length} running${instruments.length ? ` · ${instruments.join(', ')}` : ''}`}
                  </td>
                  <td className={`v2-num ${pnlTone(recentPnl)}`}>
                    {closed.length > 0 ? fmtSignedUsd(recentPnl, 2) : '—'}
                  </td>
                  <td>
                    {curve.length > 1
                      ? <Sparkline data={curve.slice(-20)} width={96} height={24} />
                      : <span className="v2-type-caption">—</span>}
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
    </div>
  )
}

// ─────────────────────────────────────────────────────────────────────────────
// 5. Open positions — paper runner positions, 10s poll, semantic P&L colors
// ─────────────────────────────────────────────────────────────────────────────

function OpenPositionsPanel({ positions, isLoading, isError }: {
  positions: OpenPosition[]
  isLoading: boolean
  isError: boolean
}) {
  if (isLoading) return <Skeleton variant="table" rows={4} cols={6} className="h-full" />
  if (isError) {
    return (
      <EmptyState
        icon={Briefcase}
        title="Positions unavailable"
        hint="Open positions could not be loaded right now."
        className="h-full"
      />
    )
  }
  if (positions.length === 0) {
    return (
      <EmptyState
        icon={Briefcase}
        title="No open positions"
        hint="Positions from running paper sessions appear here within 10 seconds of entry."
        className="h-full"
      />
    )
  }

  const totalUnrealized = positions.reduce((acc, p) => acc + (p.unrealized_pnl ?? 0), 0)

  return (
    <div className="v2-card p-4 h-full">
      <SectionHeader
        title="Open positions"
        subtitle={`${positions.length} open · 10s refresh`}
        icon={Briefcase}
        actions={
          <span className={`v2-badge v2-num ${totalUnrealized > 0 ? 'v2-badge--up' : totalUnrealized < 0 ? 'v2-badge--down' : 'v2-badge--neutral'}`}>
            {fmtSignedUsd(totalUnrealized, 2)}
          </span>
        }
      />
      <div className="overflow-x-auto">
        <table className="v2-table">
          <thead>
            <tr>
              <th>Symbol</th>
              <th>Side</th>
              <th className="v2-num">Qty</th>
              <th className="v2-num">Entry</th>
              <th className="v2-num">Last</th>
              <th className="v2-num">Stop</th>
              <th className="v2-num">Target</th>
              <th className="v2-num">Unrlzd P&L</th>
              <th>Held</th>
            </tr>
          </thead>
          <tbody>
            {positions.map(p => (
              <tr key={`${p.session_id}:${p.instrument}`}>
                <td className="font-semibold">{p.instrument}</td>
                <td>
                  <span className={`v2-badge ${p.direction === 'long' ? 'v2-badge--up' : 'v2-badge--down'}`}>
                    {p.direction}
                  </span>
                </td>
                <td className="v2-num">{p.contracts}</td>
                <td className="v2-num">{fmtPx(p.entry_price)}</td>
                <td className="v2-num">{fmtPx(p.current_price)}</td>
                <td className="v2-num">{fmtPx(p.stop_loss)}</td>
                <td className="v2-num">{fmtPx(p.take_profit)}</td>
                <td className={`v2-num font-semibold ${pnlTone(p.unrealized_pnl ?? 0)}`}>
                  <LiveNumber value={p.unrealized_pnl ?? 0} format={v => fmtSignedUsd(v, 2)} />
                </td>
                <td className="v2-type-caption whitespace-nowrap">{fmtHold(p.entry_time, null)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}

// ─────────────────────────────────────────────────────────────────────────────
// 6. Activity feed — open/close events from the most recent trades
// ─────────────────────────────────────────────────────────────────────────────

type FeedEvent = {
  key: string
  ts: number
  iso: string
  kind: 'open' | 'close'
  trade: Trade
}

function buildFeed(trades: Trade[], max = 12): FeedEvent[] {
  const events: FeedEvent[] = []
  for (const t of trades) {
    if (t.entry_time) {
      events.push({ key: `${t.id}-open`, ts: new Date(t.entry_time).getTime(), iso: t.entry_time, kind: 'open', trade: t })
    }
    if (t.exit_time && t.status === 'closed') {
      events.push({ key: `${t.id}-close`, ts: new Date(t.exit_time).getTime(), iso: t.exit_time, kind: 'close', trade: t })
    }
  }
  return events.sort((a, b) => b.ts - a.ts).slice(0, max)
}

function ActivityFeed({ trades, isLoading, isError }: {
  trades: Trade[]
  isLoading: boolean
  isError: boolean
}) {
  if (isLoading) return <Skeleton variant="card" height={320} className="h-full" />
  if (isError) {
    return (
      <EmptyState
        icon={Activity}
        title="Activity unavailable"
        hint="Recent trades could not be loaded right now."
        className="h-full"
      />
    )
  }

  const events = buildFeed(trades)
  if (events.length === 0) {
    return (
      <EmptyState
        icon={Activity}
        title="No activity yet"
        hint="Entries and exits from paper and live sessions show up here as they happen."
        className="h-full"
      />
    )
  }

  return (
    <div className="v2-card p-4 h-full">
      <SectionHeader title="Activity" subtitle="latest entries & exits" icon={Activity} />
      <ul className="m-0 p-0 list-none">
        {events.map((e, i) => {
          const t = e.trade
          return (
            <li key={e.key} className={`flex items-center gap-2 py-2 ${i > 0 ? 'v2-hairline' : ''}`}>
              <span className={`v2-badge ${t.mode === 'live' ? 'v2-badge--accent' : 'v2-badge--neutral'}`}>
                {t.mode}
              </span>
              <div className="min-w-0 flex-1">
                <div className="v2-type-body truncate">
                  {e.kind === 'close' ? 'Closed' : 'Opened'} {t.direction} {t.instrument} ×{t.contracts}
                </div>
                <div className="v2-type-caption truncate">
                  {fmtEntryTime(e.iso)}
                  {e.kind === 'close' && t.exit_reason ? ` · ${t.exit_reason}` : ''}
                </div>
              </div>
              {e.kind === 'close' && t.net_pnl != null && (
                <span className={`v2-num text-xs font-semibold ${pnlTone(t.net_pnl)}`}>
                  {fmtSignedUsd(t.net_pnl, 2)}
                </span>
              )}
            </li>
          )
        })}
      </ul>
    </div>
  )
}

// ─────────────────────────────────────────────────────────────────────────────
// 7. Engine — particle-field visual with REAL system-activity captions.
// Decorative but honest: every caption is derived from data the page already
// holds (stream state, positions, latest pick/signal) or a true static fact
// about the system. No fabricated stats. See components/v2/EngineField.tsx
// for the perf contract (30fps cap, offscreen/hidden pause, reduced motion).
// ─────────────────────────────────────────────────────────────────────────────

function EnginePanel({ activity, live }: { activity: string[]; live: boolean }) {
  return (
    <div className="v2-card p-4">
      <SectionHeader
        title="Engine"
        subtitle="live system activity"
        icon={Cpu}
        actions={live ? (
          <span
            className="v2-type-micro inline-flex items-center gap-1.5 whitespace-nowrap"
            title="Live updates streaming — background polling paused"
          >
            <span className="v2-ticker__live-dot" />
            LIVE
          </span>
        ) : undefined}
      />
      <EngineField height={220} activity={activity} live={live} />
    </div>
  )
}

// ─────────────────────────────────────────────────────────────────────────────
// Page
// ─────────────────────────────────────────────────────────────────────────────

export default function DashboardV2() {
  const queryClient = useQueryClient()

  // ── Live stream (SSE) ──────────────────────────────────────────────────
  // While the stream is connected its named events are written straight
  // into the react-query cache (setQueryData below) and the three matching
  // refetchIntervals are PAUSED — the queries remain the single source of
  // truth for every panel; SSE is just a faster writer. The moment the
  // stream drops, `streamLive` flips false and polling resumes on its own.
  const { connected: streamLive, payloads: streamPayloads } =
    useEventStream('/api/v1/stream/dashboard', STREAM_EVENTS)

  useEffect(() => {
    if (streamPayloads.positions !== undefined) {
      queryClient.setQueryData(['v2-open-positions'], streamPayloads.positions as OpenPosition[])
    }
  }, [streamPayloads.positions, queryClient])
  useEffect(() => {
    if (streamPayloads.pnl !== undefined) {
      queryClient.setQueryData(['v2-portfolio-summary'], streamPayloads.pnl as PortfolioSummary)
    }
  }, [streamPayloads.pnl, queryClient])
  useEffect(() => {
    // Same shape as scannerApi.history(1, 'all') → { picks: [...] , ... }
    if (streamPayloads.signals !== undefined) {
      queryClient.setQueryData(['v2-scanner-pick'], streamPayloads.signals)
    }
  }, [streamPayloads.signals, queryClient])

  // Poll rhythm: fast (10s) for open positions, medium (30s) for sessions /
  // trades, slow (60s) for money aggregates, glacial (5m) for bias + scanner.
  // Every staleTime sits below its refetchInterval (coherent cache window).
  // The three stream-fed queries poll ONLY while the stream is down.
  const summaryQ = useQuery({
    queryKey: ['v2-dashboard-summary'],
    queryFn: () => dashboardApi.summary().then(r => r.data),
    refetchInterval: 60_000,
    staleTime: 30_000,
  })
  const portfolioQ = useQuery({
    queryKey: ['v2-portfolio-summary'],
    queryFn: () => liveTradingApi.portfolioSummary().then(r => r.data as PortfolioSummary),
    refetchInterval: streamLive ? false : 60_000, // stream 'pnl' events feed this while live
    staleTime: 30_000,
    retry: false, // 403s for non-KYC users — same treatment as V1 Dashboard
  })
  const biasQ = useQuery({
    queryKey: ['v2-daily-bias'],
    queryFn: () => dashboardApi.bias().then(r => r.data),
    refetchInterval: 5 * 60_000,
    staleTime: 2 * 60_000,
  })
  const pickQ = useQuery({
    queryKey: ['v2-scanner-pick'],
    queryFn: () => scannerApi.history(1, 'all').then(r => r.data),
    refetchInterval: streamLive ? false : 5 * 60_000, // stream 'signals' events feed this while live
    staleTime: 2 * 60_000,
    retry: false,
  })
  const strategiesQ = useQuery({
    queryKey: ['v2-strategies'],
    queryFn: () => strategiesApi.list().then(r => r.data),
    refetchInterval: 2 * 60_000,
    staleTime: 60_000,
  })
  const paperSessionsQ = useQuery({
    queryKey: ['v2-paper-sessions'],
    queryFn: () => paperTradingApi.listSessions().then(r => r.data as PaperSession[]),
    refetchInterval: 30_000,
    staleTime: 15_000,
  })
  const liveSessionsQ = useQuery({
    queryKey: ['v2-live-sessions'],
    queryFn: () => liveTradingApi.listSessions().then(r => r.data),
    refetchInterval: 30_000,
    staleTime: 15_000,
    retry: false, // KYC-gated like portfolio-summary
  })
  const openPositionsQ = useQuery({
    queryKey: ['v2-open-positions'],
    queryFn: () => tradesApi.openPositions().then(r => r.data as OpenPosition[]),
    refetchInterval: streamLive ? false : 10_000, // stream 'positions' events feed this while live
    staleTime: 5_000,
  })
  const tradesQ = useQuery({
    queryKey: ['v2-recent-trades'],
    queryFn: () => tradesApi.list({ limit: 200 }).then(r => r.data),
    refetchInterval: 30_000,
    staleTime: 15_000,
  })

  const openPositions = openPositionsQ.data ?? []
  const latestPick = pickQ.data?.picks?.[0] ?? null
  const recentTrades = tradesQ.data ?? []
  const strategies = strategiesQ.data ?? []

  // ── Engine panel captions — REAL lines only, derived from data already
  // on this page plus a few honest static system facts. Nothing invented.
  const engineActivity = useMemo(() => {
    const lines: string[] = []
    lines.push(streamLive
      ? 'live stream connected'
      : 'stream offline — polling fallback active')

    const liveOpen = portfolioQ.data?.open_positions_count ?? 0
    const openCount = openPositions.length + liveOpen
    lines.push(`monitoring ${openCount} open position${openCount === 1 ? '' : 's'}`)

    if (!pickQ.isError && pickQ.data) {
      lines.push(latestPick
        ? `Saro: latest pick ${latestPick.ticker}`
        : 'Saro: no pick today')
    }

    // Latest signal = newest trade entry in the fetched window (same data
    // the Activity feed renders).
    let lastEntry: Trade | null = null
    let lastEntryTs = -Infinity
    for (const t of recentTrades) {
      if (!t.entry_time || t.entry_price == null) continue
      const ts = new Date(t.entry_time).getTime()
      if (ts > lastEntryTs) { lastEntryTs = ts; lastEntry = t }
    }
    if (lastEntry) {
      lines.push(`signal: ${lastEntry.direction.toUpperCase()} ${lastEntry.instrument} @ ${fmtPx(lastEntry.entry_price)}`)
    }

    const activeStrategies = strategies.filter(s => s.status === 'active').length
    if (activeStrategies > 0) {
      lines.push(`V2 forward-test: ${activeStrategies} strateg${activeStrategies === 1 ? 'y' : 'ies'}`)
    }

    // Honest static system facts — real cadences, no invented stats.
    lines.push('FMP quotes polling 5s')
    lines.push('risk guardrails armed')
    lines.push('daily bias engine: EMA 9/21 · 5m refresh')
    return lines
  }, [streamLive, openPositions.length, portfolioQ.data, pickQ.isError, pickQ.data, latestPick, recentTrades, strategies])

  return (
    <div className="v2-root v2-page">
      <div className="max-w-7xl mx-auto px-4 sm:px-6 py-6 space-y-4">

        {/* Page header */}
        <div className="flex items-end justify-between gap-3">
          <div>
            <h1 className="v2-type-title">Dashboard</h1>
            <p className="v2-type-caption">Portfolio, market context and strategy health at a glance</p>
          </div>
          {/* Honest like the ticker pip: only rendered while the SSE stream
              is actually connected (polling is paused underneath it). */}
          {streamLive && (
            <span
              className="v2-type-micro inline-flex items-center gap-1.5 whitespace-nowrap pb-0.5"
              title="Live updates streaming — background polling paused"
            >
              <span className="v2-ticker__live-dot" />
              LIVE
            </span>
          )}
        </div>

        {/* 1. Stat strip */}
        <ErrorBoundary title="Portfolio stats">
          <StatStrip
            summary={summaryQ.data}
            summaryLoading={summaryQ.isLoading}
            portfolio={portfolioQ.data}
            portfolioFailed={portfolioQ.isError}
            paperOpenCount={openPositions.length}
          />
        </ErrorBoundary>

        {/* 2 + 3. Market context / Today's pick */}
        <div className="grid gap-4 lg:grid-cols-3">
          <div className="lg:col-span-2 min-w-0">
            <ErrorBoundary title="Market context">
              <MarketContextCard
                biases={biasQ.data?.biases ?? []}
                isLoading={biasQ.isLoading}
                isError={biasQ.isError}
              />
            </ErrorBoundary>
          </div>
          <div className="min-w-0">
            <ErrorBoundary title="Today's pick">
              <TodaysPickCard
                pick={latestPick}
                isLoading={pickQ.isLoading}
                isError={pickQ.isError}
              />
            </ErrorBoundary>
          </div>
        </div>

        {/* 4. Strategy health board */}
        <ErrorBoundary title="Strategy health">
          <StrategyHealthBoard
            strategies={strategiesQ.data ?? []}
            isLoading={strategiesQ.isLoading}
            isError={strategiesQ.isError}
            paperSessions={paperSessionsQ.data ?? []}
            liveSessions={liveSessionsQ.data ?? []}
            trades={tradesQ.data ?? []}
          />
        </ErrorBoundary>

        {/* 5 + 6. Open positions / Activity feed */}
        <div className="grid gap-4 lg:grid-cols-3">
          <div className="lg:col-span-2 min-w-0">
            <ErrorBoundary title="Open positions">
              <OpenPositionsPanel
                positions={openPositions}
                isLoading={openPositionsQ.isLoading}
                isError={openPositionsQ.isError}
              />
            </ErrorBoundary>
          </div>
          <div className="min-w-0">
            <ErrorBoundary title="Activity">
              <ActivityFeed
                trades={tradesQ.data ?? []}
                isLoading={tradesQ.isLoading}
                isError={tradesQ.isError}
              />
            </ErrorBoundary>
          </div>
        </div>

        {/* 7. Engine — live system activity field */}
        <ErrorBoundary title="Engine">
          <EnginePanel activity={engineActivity} live={streamLive} />
        </ErrorBoundary>

      </div>
    </div>
  )
}
