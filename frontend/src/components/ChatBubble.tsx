import { useState, useRef, useEffect } from 'react'
import { MessageCircle, X, Send, Mail, CheckCircle2, AlertCircle } from 'lucide-react'
import api from '../api/client'

/**
 * ChatBubble — in-app help assistant.
 *
 * Knowledge base covers every feature of the platform end-to-end: the 8:30 ET
 * pre-market email, all 5 scanner strategies, Tradier integration (sandbox vs
 * live), confirm/skip flow, options paper/live/backtest, the Wheel, news
 * blackouts, SEC EDGAR catalysts, pricing tiers, disclosure flow, etc.
 *
 * When the bot can't confidently answer, it surfaces a "Still need help?
 * Email support" button that opens an inline compose form and POSTs the
 * message to /api/v1/support/contact — no client-side mailto: dependency.
 */
type Entry = { keys: string[]; aliases?: string[]; answer: string }

const SUPPORT_EMAIL = 'support@thetaalgos.com'

const KB: Entry[] = [
  // ── What is Theta Algos? ──────────────────────────────────────────────────
  {
    keys: ['theta', 'algos'], aliases: ['what is theta', 'what is this', 'what is theta algos', 'what does this do', 'what platform', 'about'],
    answer: 'Theta Algos is an automated trading platform that scans 3,000+ stocks every 5 minutes between 4 AM-8 PM ET, finds the biggest movers with real catalysts, and emails you the top 1 pick plus 4 runners-up at 8:30 AM ET every morning. It trades options through your Tradier account (sandbox or live) and emits futures signals for prop-firm accounts. Built to remove emotional trading — the bot just runs the math.'
  },

  // ── Getting started ───────────────────────────────────────────────────────
  {
    keys: ['start', 'how'], aliases: ['get started', 'how do i start', 'first time', 'new user', 'sign up', 'signup'],
    answer: 'Three steps to your first signal: (1) Sign up for the free 30-day trial — no card required. (2) Get a free Tradier sandbox token at developer.tradier.com (no funding needed, simulated trades), and connect it under Live Trading → Connect Account → Tradier. (3) Wait for tomorrow morning at 8:30 AM ET — you\'ll get the consolidated email with the top pick and 4 runners-up. Click Confirm on any one to fire the trade.'
  },

  // ── The morning email ─────────────────────────────────────────────────────
  {
    keys: ['email', 'morning'], aliases: ['8:30 email', 'morning email', 'morning signal', 'pre market email', 'premarket email'],
    answer: 'Every weekday at 8:30 AM ET you get ONE email per active strategy with the top pick highlighted at the top + up to 4 runners-up below. Each row has its own one-click Confirm / Skip buttons. Skip = the bot ignores that signal forever. Confirm = the bot fires the trade through your Tradier account (or paper-trades it if no Tradier connected). If you don\'t click within 15 min, the signal expires automatically.'
  },
  {
    keys: ['confirm'], aliases: ['confirm button', 'how do i confirm', 'click confirm'],
    answer: 'When the morning email arrives, scroll to the pick you want and click ✓ Confirm — execute. That opens the confirm page in your browser. If your Tradier account is connected and live-trading-enabled, the bot fires a real order within seconds. If you\'re paper-mode, it logs a simulated fill. You can confirm any combination of the picks — they\'re independent.'
  },
  {
    keys: ['skip'], aliases: ['skip button', 'reject'],
    answer: 'Click Skip on any signal in the morning email to tell the bot to ignore it. Skipping one doesn\'t affect the others. Once skipped, the signal can\'t be revived — the bot will look for new ones on the next scan.'
  },
  {
    keys: ['intraday'], aliases: ['during the day', 'mid day', 'after open'],
    answer: 'Outside the 8:30 ET morning batch, the bot also scans every 5 min from 4 AM to 8 PM ET. Intraday hits auto-execute (no confirm required) and email you a receipt after the fill — that way you don\'t miss the move.'
  },

  // ── Scanner strategies (the 5 pre-built ones) ─────────────────────────────
  {
    keys: ['pre', 'market', 'gap'], aliases: ['gap runner', 'pre-market gap', 'premarket gap', 'gapper'],
    answer: 'Pre-Market Gap Runner — hunts stocks gapping up 5%+ in pre-market (4 AM-9:30 AM ET) with at least 100K pre-market volume. Top 15 ranked by volume. Best for catching the morning runners before 9:30 ET open.'
  },
  {
    keys: ['low', 'float', 'squeeze'], aliases: ['low float', 'penny stock', 'low float runner'],
    answer: 'Low-Float Squeeze — Tim Sykes-style scanner. Filters: price $0.50-$20, float under 10M shares, pre-market vol > 5K or intraday vol > 1M, 5%+ pre or 10%+ intraday move, plus a positive text catalyst on the wire (FDA approval, earnings beat, contract awarded). Fires only when EVERY filter matches. Highest-conviction setup the bot makes.'
  },
  {
    keys: ['52', 'week', 'high'], aliases: ['52wh', '52-week high', 'breakout'],
    answer: '52-Week High Breakout — tracks stocks within 2% of their 52-week high with intraday volume spike of 300%+ vs the 20-day average, AND RSI confirming the move (RSI > 60). Entry triggers when a 1-min candle closes above the 52WH resistance line. Stop = 3% below the breakout level. Target = +10% measured move.'
  },
  {
    keys: ['oracle'], aliases: ['oracle setup', 'opening candle', '5 min trigger'],
    answer: 'Oracle 5-Min Opening Candle — StocksToTrade clone. Three phases: (1) Pre-market filters universe to 15-20 high-vol gappers under $50. (2) Tracks the 9:30-9:35 ET opening candle on each. (3) At 9:35 ET emits the setup: bias from VWAP (Green if close > VWAP, Red if below), entry at opening-candle high (longs) or low (shorts), stop at the opposite end, target at 2× risk. Includes Fibonacci retracements and half-dollar/whole-dollar Oracle Levels.'
  },
  {
    keys: ['momentum', 'gapper'], aliases: ['momentum gappers', 'momentum scanner'],
    answer: 'Momentum Gappers — broader scan than Low-Float Squeeze. Filters: stocks moving 10%+ on 1.5x volume between $1-$20. Doesn\'t require a positive catalyst (just the move + volume). Best for catching second-day continuation runs and sympathy plays.'
  },
  {
    keys: ['futures', 'signal'], aliases: ['ict signal', 'futures scanner', 'prop firm', 'apex', 'topstep', 'tpt', 'topstep x'],
    answer: 'Futures Signal Scanner (ICT) — designed for prop-firm accounts where algos are banned (Apex, TPT, Topstep). Runs ICT Liquidity Sweep + FVG + Displacement filters on ES, NQ, RTY, YM. Emits email + push notifications with entry, stop, target — you place the order manually inside your prop account. Same setups as the futures backtest engine, just delivered as signals.'
  },
  {
    keys: ['wheel'], aliases: ['wheel strategy', 'csp', 'cash secured put', 'covered call'],
    answer: 'The Wheel — full state machine. Sell cash-secured puts on a stock you\'d be happy to own → if assigned, you get 100 shares at the strike → immediately sell covered calls at or above your cost basis → if called away, cycle restarts. The bot auto-buys back any short at 50% profit (Tastytrade-style) and rolls. Best for income-focused traders on names you actually want to own.'
  },

  // ── Tradier ───────────────────────────────────────────────────────────────
  {
    keys: ['broker', 'support', 'supported'],
    aliases: [
      'what brokers', 'what brokers do you support', 'which brokers',
      'which brokers do you support', 'broker support', 'supported brokers',
      'broker integration', 'brokers supported', 'list of brokers',
      'what broker do you use', 'broker options', 'what brokerages',
      'which brokerage', 'supported brokerages',
    ],
    answer: 'Theta Algos integrates with two brokers today: (1) Tradier — for stocks + options. Free sandbox token at developer.tradier.com (no funding required), production token requires a funded Tradier brokerage account. (2) Tradovate — for futures (ES/NQ/RTY/YM). Demo + live accounts available at tradovate.com. For prop-firm accounts (Apex, TPT, Topstep, Topstep X) where automation is banned, we emit email + push signal notifications instead of placing orders — you trade those manually inside the prop firm. Coming later: Webull, Interactive Brokers, TradeStation, AMP / Rithmic — see the Connect Account modal for the full pipeline.'
  },
  {
    keys: ['prop', 'firm'], aliases: ['propfirm', 'prop firms', 'apex', 'topstep', 'tpt', 'topstep x', 'algo banned', 'no algo', 'manual trading'],
    answer: 'Prop firms (Apex, TPT, Topstep, Topstep X) ban automated trading. The Futures Signals tier ($49/mo) is built for this — the bot scans for the same ICT setups you would, then emails / push-notifies you the entry, stop, and target. You manually place the trade inside your prop firm rules. No broker connection required.'
  },
  {
    keys: ['webull'],
    answer: 'Webull is in the connect-account list but the integration is still pending Webull approval. Once they release retail API access, you will be able to connect Webull for options + equities the same way you connect Tradier.'
  },
  {
    keys: ['ibkr', 'interactive', 'brokers'], aliases: ['interactive brokers', 'ib', 'tws'],
    answer: 'Interactive Brokers is on the broker pipeline but not yet wired. Their Client Portal API exists; integration is gated on building out our adapter — likely after we hit 50+ paying users.'
  },
  {
    keys: ['tradestation'],
    answer: 'TradeStation is on the broker pipeline but not yet wired. Their OAuth-based API is well-documented; we will add it after Tradier + Tradovate are battle-tested.'
  },
  {
    keys: ['rithmic', 'amp', 'cqg'], aliases: ['ninja', 'ninjatrader'],
    answer: 'AMP / Rithmic and CQG are on the futures-broker pipeline (lower priority than Tradovate since Tradovate covers the main prop-firm-friendly use case). Integration timeline depends on user demand.'
  },
  {
    keys: ['connect', 'account'], aliases: ['add broker', 'add account', 'connect broker', 'how do i connect', 'broker account'],
    answer: 'Live Trading page → click "Connect Account" → pick a broker tile (Tradier, Tradovate, etc.) → paste the credentials (Tradier just wants an Access Token; Tradovate wants username + password + App ID + CID + Secret) → toggle Sandbox/Demo on or off → click Test Connection → if green, click Save. The account appears in your broker list immediately.'
  },
  {
    keys: ['tradier'], aliases: ['tradier account', 'tradier broker', 'tradier token'],
    answer: 'Tradier is the broker we route options orders through. Free to sign up at developer.tradier.com. Sandbox token = $0 funding required, simulated fills, real market data. Production token requires a funded brokerage account (separate token, separate setup). Both work the same in Theta Algos — just toggle Sandbox on/off when connecting.'
  },
  {
    keys: ['sandbox'], aliases: ['paper tradier', 'tradier sandbox', 'demo tradier'],
    answer: 'Tradier sandbox is a free simulated environment — real market data, fake fills, $0 funding required. Get a sandbox token at developer.tradier.com → Sandbox tab → API Access Keys → Generate Access Token. Paste it into Live Trading → Connect Account → Tradier with the Sandbox toggle ON.'
  },
  {
    keys: ['fund', 'tradier'], aliases: ['fund my account', 'funding required', 'add money'],
    answer: 'You only need to fund a Tradier account when you\'re ready to switch from Sandbox to Production. Sandbox is 100% free and simulated. Production requires a real funded brokerage account; minimums vary. Generate a separate Production token at developer.tradier.com → Production tab when you\'re ready.'
  },
  {
    keys: ['tradovate'], aliases: ['tradovate broker', 'futures broker'],
    answer: 'Tradovate is the futures broker we connect to for live ES/NQ trading. Sign up at tradovate.com, apply for API access (free demo + paid live), and paste your credentials (username, password, App ID, CID, Secret) into Live Trading → Connect Account → Tradovate. For prop-firm users (Apex/TPT/Topstep) who can\'t use algos, use the Futures Signal Scanner instead — it emails you signals to place manually.'
  },

  // ── News blackouts + catalysts ────────────────────────────────────────────
  {
    keys: ['news', 'blackout'], aliases: ['skip news', 'fomc', 'cpi', 'ppi', 'nfp', 'red folder', 'economic event'],
    answer: 'The scanner auto-pauses ±30 minutes around every high-impact (red-folder) economic event: FOMC rate decisions, CPI, PPI, NFP, Core PCE, Retail Sales, Advance GDP. Calendar covers all of 2026 (72 events hardcoded from BLS/Fed schedules) so blackouts work even if the live news feed fails.'
  },
  {
    keys: ['edgar', 'sec'], aliases: ['sec filings', '8-k', '8k', 'sec edgar'],
    answer: 'The bot polls SEC EDGAR every 5 minutes for fresh 8-K filings (the "material event" form companies must file within ~4 business days of major news). When a Low-Float Squeeze candidate has a matching 8-K (earnings results, material agreement, FDA action, etc.), the catalyst is confirmed and the signal fires. EDGAR data is free and real-time.'
  },
  {
    keys: ['catalyst'],
    answer: 'A catalyst is a real-world event that justifies a price move — FDA approval, earnings beat, contract awarded, M&A deal. The Low-Float Squeeze scanner requires a positive catalyst from SEC EDGAR 8-K filings OR yfinance news headlines (FDA approval, earnings beat, contract awarded, etc.) before it fires. Negative catalysts (dilution, lawsuit, going-concern) trigger short signals.'
  },

  // ── Pricing ───────────────────────────────────────────────────────────────
  {
    keys: ['price', 'cost'], aliases: ['plans', 'pricing', 'how much', 'tier', 'subscription', 'monthly cost'],
    answer: '5 tiers: Tier 1 — Free Trial ($0, 30 days no card). Tier 2 — Futures Signals ($49/mo, ES/NQ/RTY/YM signals for prop-firm accounts). Tier 3 — Options Scanner ($99/mo, full 3K-ticker pre-market email). Tier 4 — Options Live ($199/mo, Tradier-routed real fills, most popular). Tier 5 — Fully Automated ($399/mo, zero clicks). Visit /pricing for the full comparison.'
  },
  {
    keys: ['free', 'trial'], aliases: ['free trial', 'trial', '30 days'],
    answer: 'Free Trial is 30 days, no credit card required. You get the full scanner preview, paper trading, all 5 strategies, and the morning email. After 30 days you upgrade or your account pauses (no auto-charge).'
  },
  {
    keys: ['upgrade', 'plan'], aliases: ['change plan', 'upgrade plan'],
    answer: 'Click your name in the sidebar → Profile → Change Plan. Pick a tier from the 5-card grid → optionally enter a promo code → check out via Stripe. Upgrade takes effect immediately.'
  },
  {
    keys: ['promo', 'code'], aliases: ['discount', 'coupon'],
    answer: 'FREETRIAL gives you 30 days at $0, no card. Other codes (FREE150, MASTER) are admin-issued for specific accounts. Enter codes on the upgrade page before checkout.'
  },
  {
    keys: ['cancel'], aliases: ['cancel subscription'],
    answer: 'Profile → Manage Subscription → Cancel. Your access continues through the end of the current billing period, then drops to Free Trial level (paper only).'
  },

  // ── Disclosure / consent flow ─────────────────────────────────────────────
  {
    keys: ['disclosure'], aliases: ['legal', 'consent', 'terms', 'agreement'],
    answer: 'Before any live trading or options strategy fires, you have to scroll-and-accept the relevant disclosure: Risk Disclosure, Live Trading Consent, and Options Trading Consent. Each one is logged with your IP and a version stamp. One-time per disclosure — once you accept the current version, no more modals until we update the text.'
  },

  // ── Pending Trades dashboard ──────────────────────────────────────────────
  {
    keys: ['pending'], aliases: ['pending trades', 'pending signals', 'awaiting confirm'],
    answer: 'View every pre-market signal the bot has generated at /app/options/pending. Active rows show "Awaiting confirm" with the trade details; clicking through opens the confirm page. Confirmed signals flip to "Confirmed", executed ones to "Executed", skipped ones to "Skipped", and ones you didn\'t act on within 15 min to "Expired".'
  },

  // ── Options-specific features ─────────────────────────────────────────────
  {
    keys: ['strike'], aliases: ['strike picker', 'pick contract', 'which option'],
    answer: 'When a directional signal fires, the bot auto-picks the right option contract: 30-60 DTE, target delta 0.30-0.50, factoring IV and stop distance. Strategy mode determines the picker rules — Trend Pullback picks ATM-ish, Vertical Spread picks both legs, etc. The picked contract\'s greeks + cost-per-contract show in the email and in the strategy card preview.'
  },
  {
    keys: ['delta'], aliases: ['option delta'],
    answer: 'Delta = how much an option\'s price moves per $1 move in the underlying. 0.50 = ATM, 0.30 = 30 cents per dollar (slightly OTM), 1.00 = deep ITM (moves like the stock). The bot targets 0.30-0.50 delta by default — balance of leverage and decay.'
  },
  {
    keys: ['theta', 'decay'], aliases: ['time decay'],
    answer: 'Theta is how much an option\'s premium drops per day, all else equal. The bot avoids selling theta during news blackouts and targets 30+ DTE for long-options strategies to minimize decay drag.'
  },
  {
    keys: ['iv', 'volatility'], aliases: ['implied volatility'],
    answer: 'Implied Volatility is what the market expects the underlying to move (annualized) over the option\'s life. High IV = expensive options; low IV = cheap. The bot uses Tradier\'s live IV when available, falls back to Black-Scholes-derived IV from historical pricing.'
  },

  // ── Existing trading concepts (kept from old KB) ──────────────────────────
  {
    keys: ['rsi'], aliases: ['relative strength'],
    answer: 'RSI (Relative Strength Index) is a 0-100 momentum oscillator. Above 70 = overbought, below 30 = oversold. The scanners use RSI as confirmation: 52-Week Breakouts require RSI > 60; Low-Float Squeeze rejects longs above RSI 80 (chasing) and shorts below RSI 20.'
  },
  {
    keys: ['vwap'], aliases: ['volume weighted'],
    answer: 'VWAP (Volume-Weighted Average Price) is the average price weighted by volume since session open. Above VWAP = bullish intraday, below = bearish. Oracle uses VWAP as the bias gate — opening-candle close above VWAP = Green/Long bias.'
  },
  {
    keys: ['fvg', 'fair'], aliases: ['fair value gap'],
    answer: 'Fair Value Gap (FVG) is a 3-candle imbalance: candle 1 wick doesn\'t overlap candle 3 wick. The futures scanner uses FVG retracements as entry zones. The 50% midpoint (Consequent Encroachment) is the precise entry level.'
  },
  {
    keys: ['ict'], aliases: ['ict trading', 'ict strategy', 'liquidity sweep', 'displacement'],
    answer: 'ICT (Inner Circle Trader) is the methodology behind the futures signal scanner: Liquidity Sweep → Market Structure Shift → FVG entry. The bot scans ES/NQ/RTY/YM for these patterns and emits signal-only notifications for prop-firm users.'
  },
  {
    keys: ['stop', 'loss'],
    answer: 'A stop loss is your pre-set exit price that caps the loss. The scanner sets stops at the prior swing low (longs) or high (shorts), capped at a maximum dollar risk. For options, stop = 50% premium loss by default.'
  },
  {
    keys: ['take', 'profit'],
    answer: 'A take profit is your pre-set exit that locks in gains. The scanner computes TP from the strategy\'s R:R ratio applied to stop distance. For options, target = 100% premium gain by default (double your money).'
  },
  {
    keys: ['risk', 'reward'], aliases: ['rr', 'r:r', 'risk reward ratio'],
    answer: 'Risk:Reward is target distance ÷ stop distance. 2R = take-profit is twice as far as the stop. A 2R strategy can lose 60% of trades and still be profitable. Most strategies here default to 2R.'
  },
  {
    keys: ['drawdown'], aliases: ['max drawdown'],
    answer: 'Drawdown is peak-to-trough decline in your equity curve. Max Drawdown = the worst observed in the period. Shown on every backtest result. Generally want < 20% — anything worse is hard to stomach in real money.'
  },
  {
    keys: ['profit', 'factor'],
    answer: 'Profit Factor = total winning P&L / |total losing P&L|. Above 1.0 is profitable, 1.5+ is solid, 2.0+ is excellent. Shows on every backtest + session detail page.'
  },

  // ── Account / settings ────────────────────────────────────────────────────
  {
    keys: ['paper', 'trade'], aliases: ['paper trading'],
    answer: 'Paper Trading runs your strategy against real-time market data with simulated fills. No real money. Use it to build a track record before going live. Every strategy supports paper mode by default — flip the Activate button on a strategy card and pick "Paper".'
  },
  {
    keys: ['backtest', 'how'], aliases: ['run backtest', 'historical test'],
    answer: 'Open Backtests → New Backtest → pick a strategy + instrument + date range → Run. Replays bar-by-bar with realistic slippage and commissions over up to 5 years of data. Options strategies use real historical option aggs from Polygon. Results render with equity curve, monthly returns, win rate, max drawdown, and a per-trade log.'
  },
  {
    keys: ['2fa', 'two', 'factor'], aliases: ['authenticator', 'totp'],
    answer: 'Profile → Two-Factor Authentication → Enable 2FA. Scan the QR with Google Authenticator / Authy / 1Password, enter the 6-digit code. Disable any time by entering a current code.'
  },
  {
    keys: ['password', 'reset'], aliases: ['forgot password'],
    answer: 'On the sign-in page click "Forgot password?" — you\'ll get an email with a one-hour reset link.'
  },
  {
    keys: ['dark', 'mode'], aliases: ['light mode', 'theme'],
    answer: 'Profile → Appearance → toggle Light or Dark. Stored on this device.'
  },

  // ── Risk controls ─────────────────────────────────────────────────────────
  {
    keys: ['kill', 'switch'],
    answer: 'Kill Switch instantly halts a live or paper session. Use it when something\'s gone wrong — the bot stops placing new orders. Existing positions stay open until you manually close them or they hit their stop/target.'
  },
  {
    keys: ['daily', 'loss'], aliases: ['daily loss limit', 'daily limit'],
    answer: 'Set a daily loss limit on any session — once realized losses cross the threshold, the kill switch trips automatically and no new trades fire until the next day. Default for new sessions is 3% of starting balance.'
  },
  {
    keys: ['earnings'], aliases: ['earnings filter', 'earnings avoidance'],
    answer: 'The bot auto-skips entering trades within 3 days of a stock\'s earnings announcement (yfinance-sourced calendar, cached 24h). Earnings-Catalyst strategy is the exception — that one trades INTO earnings on purpose with tiny size.'
  },

  // ── Universe / scanning ───────────────────────────────────────────────────
  {
    keys: ['universe'], aliases: ['tickers scanned', 'what stocks', 'how many tickers'],
    answer: 'The scanner watches 3,035 tickers — the full Russell 2000 + Russell 1000 pulled live from iShares + 70 curated low-float momentum names. Small-caps scan first (where 10%+ moves actually happen), mega-caps last. Full scan finishes in ~2 minutes and runs every 5 minutes between 4 AM-8 PM ET.'
  },
  {
    keys: ['scan'], aliases: ['how often', 'how frequently', 'scan time'],
    answer: 'Scanner runs every 5 minutes from 04:00 ET to 20:00 ET, plus a special pre-market batch at 08:30 ET that emails you the top 1+4 picks. Outside the window the scheduler sleeps until next open. Weekends off.'
  },
]

function findAnswer(question: string): string | null {
  const q = question.toLowerCase().trim()
  if (!q) return null

  // First pass — high-precision phrase matches via aliases
  for (const e of KB) {
    if (e.aliases) {
      for (const a of e.aliases) {
        if (q.includes(a.toLowerCase())) return e.answer
      }
    }
  }

  // Second pass — token-overlap score on `keys`
  const tokens = new Set(q.replace(/[^a-z0-9 ]/g, ' ').split(/\s+/).filter(Boolean))
  let best: { entry: Entry; score: number } | null = null
  for (const e of KB) {
    let score = 0
    for (const k of e.keys) {
      if (tokens.has(k.toLowerCase())) score += 2
      else if (q.includes(k.toLowerCase())) score += 1
    }
    if (score >= 2 && (!best || score > best.score)) best = { entry: e, score }
  }
  return best ? best.entry.answer : null
}

type Msg = { role: 'user' | 'bot'; text: string; fallback?: boolean }

export default function ChatBubble() {
  const [open, setOpen] = useState(false)
  const [msgs, setMsgs] = useState<Msg[]>([{
    role: 'bot',
    text: 'Hi! Ask me anything about Theta Algos — how the morning email works, what the scanners do, how to connect Tradier, what each tier costs, or any trading concept (RSI, VWAP, FVG, delta, theta, etc.).',
  }])
  const [input, setInput] = useState('')
  const [unanswered, setUnanswered] = useState<string[]>([])  // questions the bot couldn't answer
  const [showEmail, setShowEmail] = useState(false)
  const endRef = useRef<HTMLDivElement>(null)

  useEffect(() => { endRef.current?.scrollIntoView({ behavior: 'smooth' }) }, [msgs, showEmail])

  const send = () => {
    if (!input.trim()) return
    const userMsg = input.trim()
    setMsgs(prev => [...prev, { role: 'user', text: userMsg }])
    setInput('')
    setTimeout(() => {
      const answer = findAnswer(userMsg)
      if (answer) {
        setMsgs(prev => [...prev, { role: 'bot', text: answer }])
      } else {
        setUnanswered(prev => [...prev, userMsg])
        setMsgs(prev => [
          ...prev,
          {
            role: 'bot',
            fallback: true,
            text: `I don't have a confident answer for that. If you want, I can forward your question to our support team at ${SUPPORT_EMAIL} — click "Email support" below.`,
          },
        ])
      }
    }, 350)
  }

  return (
    <>
      {!open && (
        <button onClick={() => setOpen(true)}
          className="fixed bottom-6 right-6 w-14 h-14 bg-blue-600 rounded-full flex items-center justify-center shadow-lg hover:bg-blue-700 transition-all hover:scale-105 z-50">
          <MessageCircle size={24} className="text-white"/>
        </button>
      )}

      {open && (
        <div className="fixed bottom-6 right-6 w-96 h-[34rem] bg-white dark:bg-slate-800 rounded-xl shadow-2xl border border-slate-200 dark:border-slate-700 flex flex-col z-50">
          <div className="flex items-center justify-between px-4 py-3 bg-blue-600 rounded-t-xl">
            <div className="text-white font-semibold text-sm">Theta Algos Assistant</div>
            <button onClick={() => setOpen(false)} className="text-white/80 hover:text-white"><X size={16}/></button>
          </div>

          {!showEmail ? (
            <>
              <div className="flex-1 overflow-y-auto p-3 space-y-2">
                {msgs.map((m, i) => (
                  <div key={i}>
                    <div className={`flex ${m.role === 'user' ? 'justify-end' : 'justify-start'}`}>
                      <div className={`max-w-[88%] px-3 py-2 rounded-lg text-sm leading-relaxed ${
                        m.role === 'user'
                          ? 'bg-blue-600 text-white'
                          : 'bg-slate-100 text-slate-800 dark:bg-slate-700 dark:text-slate-100'
                      }`}>
                        {m.text}
                      </div>
                    </div>
                    {m.fallback && (
                      <div className="flex justify-start mt-1.5">
                        <button onClick={() => setShowEmail(true)}
                          className="inline-flex items-center gap-1.5 px-3 py-1.5 bg-amber-50 dark:bg-amber-900/30 border border-amber-200 dark:border-amber-800 text-amber-800 dark:text-amber-200 rounded-lg text-xs font-bold hover:bg-amber-100 dark:hover:bg-amber-900/50">
                          <Mail size={12}/> Email support
                        </button>
                      </div>
                    )}
                  </div>
                ))}
                <div ref={endRef}/>
              </div>

              <div className="px-3 pb-2">
                <div className="text-[10px] text-slate-400 dark:text-slate-500 text-center">
                  Still stuck? <button onClick={() => setShowEmail(true)} className="underline font-semibold hover:text-blue-600">Email {SUPPORT_EMAIL}</button>
                </div>
              </div>
              <div className="p-3 border-t border-slate-200 dark:border-slate-700 flex gap-2">
                <input value={input} onChange={e => setInput(e.target.value)}
                  onKeyDown={e => e.key === 'Enter' && send()}
                  placeholder="Ask anything…"
                  className="flex-1 px-3 py-2 border border-slate-300 dark:border-slate-600 dark:bg-slate-900 dark:text-slate-100 rounded-lg text-sm outline-none focus:border-blue-500"/>
                <button onClick={send} className="w-9 h-9 bg-blue-600 rounded-lg flex items-center justify-center hover:bg-blue-700">
                  <Send size={14} className="text-white"/>
                </button>
              </div>
            </>
          ) : (
            <SupportEmailForm
              defaultQuestion={unanswered[unanswered.length - 1] || ''}
              chatTranscript={msgs.filter(m => !m.fallback)
                .map(m => `${m.role === 'user' ? 'You' : 'Bot'}: ${m.text}`).join('\n\n')}
              onCancel={() => setShowEmail(false)}
              onSent={() => {
                setShowEmail(false)
                setMsgs(prev => [...prev, {
                  role: 'bot',
                  text: `✓ Sent — we'll reply to your email within 24 hours. You can also reach us anytime at ${SUPPORT_EMAIL}.`,
                }])
              }}
            />
          )}
        </div>
      )}
    </>
  )
}


function SupportEmailForm({ defaultQuestion, chatTranscript, onCancel, onSent }: {
  defaultQuestion: string
  chatTranscript: string
  onCancel: () => void
  onSent: () => void
}) {
  const [fromEmail, setFromEmail] = useState('')
  const [fromName,  setFromName]  = useState('')
  const [subject,   setSubject]   = useState(defaultQuestion.slice(0, 80) || 'Help needed with Theta Algos')
  const [message,   setMessage]   = useState(defaultQuestion || '')
  const [sending,   setSending]   = useState(false)
  const [error,     setError]     = useState<string | null>(null)

  async function submit() {
    if (!fromEmail.trim() || !message.trim()) {
      setError('Email and message are required.')
      return
    }
    setSending(true); setError(null)
    try {
      await api.post('/api/v1/support/contact', {
        from_email: fromEmail.trim(),
        from_name:  fromName.trim() || null,
        subject:    subject.trim() || null,
        message:    message.trim(),
        chat_transcript: chatTranscript || null,
      })
      onSent()
    } catch (e: any) {
      setError(e?.response?.data?.detail || 'Could not send right now. Please email support@thetaalgos.com directly.')
    } finally {
      setSending(false)
    }
  }

  return (
    <div className="flex-1 overflow-y-auto p-4 space-y-3">
      <div className="text-sm font-bold text-slate-800 dark:text-slate-100 flex items-center gap-2">
        <Mail size={14}/> Email support
      </div>
      <p className="text-xs text-slate-500 dark:text-slate-400">
        Your message + this chat transcript will be sent to {SUPPORT_EMAIL}. We typically reply within 24 hours.
      </p>

      <div>
        <label className="text-[10px] font-bold uppercase tracking-wider text-slate-500 dark:text-slate-400 block mb-1">Your email</label>
        <input type="email" value={fromEmail} onChange={e => setFromEmail(e.target.value)}
          placeholder="you@example.com"
          className="w-full px-3 py-2 border border-slate-300 dark:border-slate-600 dark:bg-slate-900 dark:text-slate-100 rounded-lg text-sm outline-none focus:border-blue-500"/>
      </div>
      <div>
        <label className="text-[10px] font-bold uppercase tracking-wider text-slate-500 dark:text-slate-400 block mb-1">Name (optional)</label>
        <input value={fromName} onChange={e => setFromName(e.target.value)}
          placeholder="Your name"
          className="w-full px-3 py-2 border border-slate-300 dark:border-slate-600 dark:bg-slate-900 dark:text-slate-100 rounded-lg text-sm outline-none focus:border-blue-500"/>
      </div>
      <div>
        <label className="text-[10px] font-bold uppercase tracking-wider text-slate-500 dark:text-slate-400 block mb-1">Subject</label>
        <input value={subject} onChange={e => setSubject(e.target.value)}
          className="w-full px-3 py-2 border border-slate-300 dark:border-slate-600 dark:bg-slate-900 dark:text-slate-100 rounded-lg text-sm outline-none focus:border-blue-500"/>
      </div>
      <div>
        <label className="text-[10px] font-bold uppercase tracking-wider text-slate-500 dark:text-slate-400 block mb-1">Message</label>
        <textarea value={message} onChange={e => setMessage(e.target.value)}
          rows={5}
          className="w-full px-3 py-2 border border-slate-300 dark:border-slate-600 dark:bg-slate-900 dark:text-slate-100 rounded-lg text-sm outline-none focus:border-blue-500 resize-none"/>
      </div>

      {error && (
        <div className="flex items-start gap-2 p-2.5 bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-900/40 rounded-lg text-xs text-red-700 dark:text-red-300">
          <AlertCircle size={13} className="flex-shrink-0 mt-0.5"/>
          <span>{error}</span>
        </div>
      )}

      <div className="flex gap-2 pt-2 border-t border-slate-200 dark:border-slate-700">
        <button onClick={onCancel}
          className="flex-1 border border-slate-300 dark:border-slate-700 text-slate-600 dark:text-slate-300 py-2 rounded-lg text-sm font-semibold hover:bg-slate-50 dark:hover:bg-slate-700/50">
          Back
        </button>
        <button onClick={submit} disabled={sending}
          className="flex-1 bg-blue-600 hover:bg-blue-700 disabled:opacity-50 text-white py-2 rounded-lg text-sm font-bold inline-flex items-center justify-center gap-1.5">
          {sending ? 'Sending…' : (<><Send size={12}/> Send</>)}
        </button>
      </div>
    </div>
  )
}
