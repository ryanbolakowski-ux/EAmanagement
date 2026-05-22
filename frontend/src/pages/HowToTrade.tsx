import { useQuery } from '@tanstack/react-query'
import { useParams, useNavigate, Link } from 'react-router-dom'
import { strategiesApi } from '../api/endpoints'
import { BookOpen, Clock, Target, Shield, AlertTriangle, ChevronRight, Play } from 'lucide-react'

const TF_HUMAN: Record<string, string> = {
  '1m': '1-minute',
  '2m': '2-minute',
  '3m': '3-minute',
  '5m': '5-minute',
  '15m': '15-minute',
  '30m': '30-minute',
  '1h': '1-hour',
  '1H': '1-hour',
  '4h': '4-hour',
  '4H': '4-hour',
  '1D': 'Daily',
}
const tfHuman = (tf: string) => TF_HUMAN[tf] || tf

const SESSION_HUMAN: Record<string, { label: string; window: string }> = {
  NY:           { label: 'NY Session',         window: '9:30 AM – 4:00 PM ET' },
  NY_AM:        { label: 'NY AM Killzone',     window: '9:30 AM – 12:00 PM ET' },
  NY_PM:        { label: 'NY PM Killzone',     window: '2:00 PM – 3:00 PM ET' },
  LONDON:       { label: 'London Killzone',    window: '2:00 AM – 5:00 AM ET' },
  LONDON_CLOSE: { label: 'London Close',       window: '10:00 AM – 12:00 PM ET' },
  ASIA:         { label: 'Asia Session',       window: '8:00 PM – 12:00 AM ET' },
}

function Step({ n, title, children }: { n: number; title: string; children: React.ReactNode }) {
  return (
    <div className="rounded-xl border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900 p-5">
      <div className="flex items-start gap-4">
        <div className="flex-shrink-0 w-9 h-9 rounded-full bg-blue-600 text-white font-bold flex items-center justify-center">
          {n}
        </div>
        <div className="flex-1">
          <h3 className="text-sm font-bold text-slate-900 dark:text-slate-100 uppercase tracking-wider mb-2">{title}</h3>
          <div className="text-sm text-slate-700 dark:text-slate-300 space-y-1.5 leading-relaxed">{children}</div>
        </div>
      </div>
    </div>
  )
}

function Pill({ children, color = 'blue' }: { children: React.ReactNode; color?: 'blue' | 'green' | 'amber' | 'slate' }) {
  const tones: Record<string, string> = {
    blue:  'bg-blue-50 text-blue-700 dark:bg-blue-900/30 dark:text-blue-300',
    green: 'bg-green-50 text-green-700 dark:bg-green-900/30 dark:text-green-300',
    amber: 'bg-amber-50 text-amber-700 dark:bg-amber-900/30 dark:text-amber-300',
    slate: 'bg-slate-100 text-slate-700 dark:bg-slate-800 dark:text-slate-300',
  }
  return <span className={`inline-block px-2 py-0.5 rounded text-xs font-semibold ${tones[color]}`}>{children}</span>
}

export default function HowToTrade() {
  const { id } = useParams<{ id?: string }>()
  const navigate = useNavigate()
  const { data: strategies, isLoading } = useQuery({
    queryKey: ['strategies'],
    queryFn: () => strategiesApi.list().then(r => r.data),
  })

  // List view: no id → show strategies grid
  if (!id) {
    return (
      <div className="space-y-6 max-w-5xl mx-auto px-4 sm:px-6 py-6">
        <div>
          <h1 className="text-2xl font-bold text-slate-900 dark:text-slate-100 flex items-center gap-2">
            <BookOpen size={24} /> How To Trade
          </h1>
          <p className="text-sm text-slate-500 dark:text-slate-400 mt-1">
            Step-by-step playbook for every strategy you've built. Pick one to see exactly how to trade it manually — including which timeframes to look at and what signals to wait for.
          </p>
        </div>

        {isLoading && <div className="text-sm text-slate-400">Loading strategies…</div>}
        {strategies && strategies.length === 0 && (
          <div className="rounded-xl border border-slate-200 dark:border-slate-700 bg-slate-50 dark:bg-slate-900 p-8 text-center">
            <p className="text-sm text-slate-500 dark:text-slate-400">You haven't built any strategies yet.</p>
            <Link to="/app/strategies" className="inline-block mt-3 px-4 py-2 bg-blue-600 hover:bg-blue-700 text-white text-sm font-semibold rounded-lg">
              Build a strategy →
            </Link>
          </div>
        )}

        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3">
          {strategies?.map((s: any) => (
            <button
              key={s.id}
              onClick={() => navigate(`/app/how-to-trade/${s.id}`)}
              className="text-left rounded-xl border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900 p-4 hover:border-blue-400 hover:shadow-md transition"
            >
              <div className="flex items-start justify-between gap-3">
                <div className="flex-1 min-w-0">
                  <div className="font-bold text-slate-900 dark:text-slate-100 truncate">{s.name}</div>
                  <div className="text-xs text-slate-500 dark:text-slate-400 mt-0.5 line-clamp-2">{s.description || 'No description'}</div>
                </div>
                <ChevronRight size={18} className="text-slate-400 flex-shrink-0" />
              </div>
              <div className="mt-3 flex flex-wrap gap-1.5">
                {(s.instruments || []).slice(0, 4).map((inst: string) => <Pill key={inst} color="slate">{inst}</Pill>)}
                <Pill color="blue">{tfHuman(s.primary_timeframe)}</Pill>
              </div>
            </button>
          ))}
        </div>
      </div>
    )
  }

  // Detail view: render the playbook for this strategy
  const strategy = strategies?.find((s: any) => s.id === id)
  if (!strategies) return <div className="text-sm text-slate-400">Loading strategy…</div>
  if (!strategy) return (
    <div className="text-sm text-red-500">
      Strategy not found. <Link to="/app/how-to-trade" className="underline">Back</Link>
    </div>
  )

  const ptf = strategy.primary_timeframe                                  // setup / FVG frame
  const etf = strategy.execution_timeframe                                // entry trigger frame
  const htfs = (strategy.higher_timeframes || []) as string[]
  // Bias should always be a higher TF than the primary. If the user didn't
  // configure one, infer a sensible default from the primary so we never tell
  // them "find the bias on a 5-minute chart" — that's the setup frame.
  const inferBiasTf = (): string => {
    if (htfs.length > 0) return htfs[0]
    if (ptf === '1m' || ptf === '2m' || ptf === '3m' || ptf === '5m') return '1h'
    if (ptf === '15m' || ptf === '30m') return '4H'
    if (ptf === '1h' || ptf === '1H' || ptf === '4h' || ptf === '4H') return '1D'
    return '1H'
  }
  const biasTf = inferBiasTf()
  const sessions = (strategy.session_filters || []) as string[]
  const rr = strategy.risk_reward_ratio
  const stopType = strategy.stop_loss_type

  return (
    <div className="space-y-6 max-w-4xl mx-auto px-4 sm:px-6 py-6">
      <div>
        <button onClick={() => navigate('/app/how-to-trade')} className="text-xs text-slate-500 dark:text-slate-400 hover:text-slate-700 dark:hover:text-slate-200">
          ← All strategies
        </button>
        <h1 className="text-2xl font-bold text-slate-900 dark:text-slate-100 mt-1">How to trade: {strategy.name}</h1>
        {strategy.description && <p className="text-sm text-slate-500 dark:text-slate-400 mt-1">{strategy.description}</p>}
      </div>

      {/* Setup card */}
      <div className="rounded-xl border border-slate-200 dark:border-slate-700 bg-slate-50 dark:bg-slate-900 p-5">
        <h2 className="text-xs font-bold text-slate-500 dark:text-slate-400 uppercase tracking-wider mb-3 flex items-center gap-2">
          <Target size={14} /> Setup
        </h2>
        <div className="grid grid-cols-2 sm:grid-cols-5 gap-4 text-sm">
          <div>
            <div className="text-[10px] uppercase text-slate-400 dark:text-slate-500 tracking-wider">Instruments</div>
            <div className="font-semibold text-slate-900 dark:text-slate-100 mt-0.5">{(strategy.instruments || []).join(', ') || '—'}</div>
          </div>
          <div>
            <div className="text-[10px] uppercase text-slate-400 dark:text-slate-500 tracking-wider">Bias TF</div>
            <div className="font-semibold text-slate-900 dark:text-slate-100 mt-0.5">{tfHuman(biasTf)}</div>
          </div>
          <div>
            <div className="text-[10px] uppercase text-slate-400 dark:text-slate-500 tracking-wider">Setup TF</div>
            <div className="font-semibold text-slate-900 dark:text-slate-100 mt-0.5">{tfHuman(ptf)}</div>
          </div>
          <div>
            <div className="text-[10px] uppercase text-slate-400 dark:text-slate-500 tracking-wider">Search / Entry TF</div>
            <div className="font-semibold text-slate-900 dark:text-slate-100 mt-0.5">{tfHuman(etf)}</div>
          </div>
          <div>
            <div className="text-[10px] uppercase text-slate-400 dark:text-slate-500 tracking-wider">Risk : Reward</div>
            <div className="font-semibold text-slate-900 dark:text-slate-100 mt-0.5">1 : {rr}</div>
          </div>
        </div>
        {sessions.length > 0 && (
          <div className="mt-4 pt-4 border-t border-slate-200 dark:border-slate-700">
            <div className="text-[10px] uppercase text-slate-400 dark:text-slate-500 tracking-wider mb-1.5 flex items-center gap-1.5"><Clock size={11}/> Trade only during</div>
            <div className="flex flex-wrap gap-2">
              {sessions.map(s => {
                const meta = SESSION_HUMAN[s.toUpperCase()] || { label: s, window: '' }
                return (
                  <Pill key={s} color="amber">
                    {meta.label}{meta.window && ` · ${meta.window}`}
                  </Pill>
                )
              })}
            </div>
          </div>
        )}
      </div>

      {/* Steps */}
      <div className="space-y-3">
        <Step n={1} title={`Set bias on the ${tfHuman(biasTf)} chart`}>
          <p>Start on the <strong>{tfHuman(biasTf)}</strong> chart for {(strategy.instruments || []).join(', ')} — this is where you decide direction. You should not look at any lower timeframe yet.</p>
          <ul className="list-disc list-inside ml-2 text-slate-600 dark:text-slate-400">
            <li>Higher highs and higher lows, price above the EMAs → <Pill color="green">BULLISH</Pill> bias. Longs only today.</li>
            <li>Lower highs and lower lows, price below the EMAs → <Pill color="amber">BEARISH</Pill> bias. Shorts only today.</li>
            <li>Choppy range with no clear structure → <Pill color="slate">NO TRADE</Pill>. Skip the session.</li>
          </ul>
          <p className="text-xs text-slate-500 italic">Lock in your bias here and don't change it. Switching bias mid-session is how accounts blow up.</p>
        </Step>

        <Step n={2} title={`Find a setup on the ${tfHuman(ptf)} chart`}>
          <p>Drop to the <strong>{tfHuman(ptf)}</strong> chart. This is your <em>setup</em> frame — where structure forms and FVGs print.</p>
          <p>First, wait for <strong>displacement</strong>: a strong impulsive move in your bias direction (3+ consecutive same-color candles that close decisively, ideally with above-average range).</p>
          <p>Then look for a 3-candle Fair Value Gap inside the impulse — a gap between candle 1 and candle 3 where the wicks do <strong>not</strong> overlap:</p>
          <ul className="list-disc list-inside ml-2 text-slate-600 dark:text-slate-400">
            <li><strong>Bullish FVG:</strong> low of candle 3 is above the high of candle 1.</li>
            <li><strong>Bearish FVG:</strong> high of candle 3 is below the low of candle 1.</li>
          </ul>
          <p>Box the gap. That's your entry zone. No FVG aligned with bias = no trade.</p>
        </Step>

        <Step n={3} title={`Search for the entry on the ${tfHuman(etf)} chart`}>
          <p>Drop to the <strong>{tfHuman(etf)}</strong> chart — this is your <em>search / entry</em> frame.</p>
          <p>Wait for price to retrace back <em>into</em> the FVG zone you boxed on the {tfHuman(ptf)}. Don't chase — let price come to your level.</p>
          <p>Your trigger is when price taps the <strong>midpoint of the FVG</strong> (the "consequent encroachment" / CE level). The bot fills exactly there.</p>
        </Step>

        <Step n={4} title="Place the trade">
          <div className="grid grid-cols-3 gap-3 mt-2">
            <div className="rounded-lg border border-blue-200 dark:border-blue-800 bg-blue-50 dark:bg-blue-900/20 p-3 text-center">
              <div className="text-[10px] uppercase font-bold text-blue-600 dark:text-blue-400">Entry</div>
              <div className="text-xs mt-1 text-slate-700 dark:text-slate-300">FVG midpoint (CE)</div>
            </div>
            <div className="rounded-lg border border-red-200 dark:border-red-800 bg-red-50 dark:bg-red-900/20 p-3 text-center">
              <div className="text-[10px] uppercase font-bold text-red-600 dark:text-red-400">Stop</div>
              <div className="text-xs mt-1 text-slate-700 dark:text-slate-300">
                {stopType === 'ticks' ? 'Fixed ticks past entry' : 'Past the swing that created the FVG'}
              </div>
            </div>
            <div className="rounded-lg border border-green-200 dark:border-green-800 bg-green-50 dark:bg-green-900/20 p-3 text-center">
              <div className="text-[10px] uppercase font-bold text-green-600 dark:text-green-400">Target</div>
              <div className="text-xs mt-1 text-slate-700 dark:text-slate-300">{rr}× the stop distance</div>
            </div>
          </div>
        </Step>

        <Step n={5} title="Manage and walk away">
          <ul className="list-disc list-inside ml-2 text-slate-600 dark:text-slate-400">
            <li>Set the stop and target as orders the moment you fill — do not babysit.</li>
            <li>Don't move the stop closer "to be safe." It defeats the edge.</li>
            <li>Don't close early at +1R. The math only works if you take the full {rr}R.</li>
            <li>If the FVG gets <strong>fully invalidated</strong> (price closes through the far side of the gap), the setup is dead — exit.</li>
          </ul>
        </Step>
      </div>

      {/* Risk reminders */}
      <div className="rounded-xl border border-amber-200 dark:border-amber-800/50 bg-amber-50 dark:bg-amber-900/20 p-5">
        <div className="flex items-start gap-3">
          <AlertTriangle size={18} className="text-amber-600 dark:text-amber-400 flex-shrink-0 mt-0.5" />
          <div className="text-sm text-amber-900 dark:text-amber-200">
            <div className="font-bold mb-1">Risk reminders</div>
            <ul className="list-disc list-inside space-y-0.5 text-amber-800 dark:text-amber-300">
              <li>Risk no more than 1% of your account on any single trade.</li>
              <li>Two losses in a row? Close the platform for the day. Tilt is real.</li>
              <li>Don't take setups outside your defined session windows — that's not your strategy anymore.</li>
              <li>Past performance does not guarantee future results. This guide is educational, not advice. Futures and options carry substantial risk of loss.</li>
            </ul>
          </div>
        </div>
      </div>

      <div className="flex gap-2">
        <Link
          to="/app/paper"
          className="inline-flex items-center gap-2 px-4 py-2 bg-blue-600 hover:bg-blue-700 text-white text-sm font-semibold rounded-lg shadow-sm"
        >
          <Play size={14} /> Paper trade this strategy
        </Link>
        <Link
          to="/app/strategies"
          className="inline-flex items-center gap-2 px-4 py-2 bg-slate-100 dark:bg-slate-800 hover:bg-slate-200 dark:hover:bg-slate-700 text-slate-700 dark:text-slate-200 text-sm font-semibold rounded-lg"
        >
          <Shield size={14} /> Edit strategy
        </Link>
      </div>
    </div>
  )
}
