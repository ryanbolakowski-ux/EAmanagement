import { useState } from 'react'
import { Link } from 'react-router-dom'
import { Building2, Check, X, AlertTriangle, ExternalLink, ShieldCheck } from 'lucide-react'

type Policy = 'allowed' | 'conditional' | 'banned'
type Firm = {
  slug: string
  name: string
  domain: string         // for the favicon
  policy: Policy
  underlying: string     // broker/platform under the hood (Tradovate, Rithmic, etc)
  summary: string        // one-line rule summary
  detail: string         // longer note — when it varies between eval/funded, etc.
  rulesUrl: string
}

// 2026 prop-firm rule landscape. THIS LIST IS CONSERVATIVE BY DESIGN — most
// futures prop firms quietly tightened their rules in 2024-2026 to ban
// algos. If a firm isn't explicitly listed as "allowed" with a recent
// confirmation, treat it as banned for safety. Always verify in writing
// with the firm before depositing.
const FIRMS: Firm[] = [
  // ─── ALLOWED — verified to permit algos as of last check ─────────────
  { slug: 'ftmo',           name: 'FTMO',                 domain: 'ftmo.com',               policy: 'allowed', underlying: 'MetaTrader / cTrader (/CFD)',
    summary: 'EA-friendly. /CFD focus, not futures.',
    detail: 'The most algo-friendly prop firm overall. Allows EAs explicitly per their published rules. /CFDs only — not CME futures, so this platform won\'t connect to it until  is added.',
    rulesUrl: 'https://ftmo.com/en/trading-rules' },
  { slug: 'fundednext',     name: 'FundedNext',           domain: 'fundednext.com',         policy: 'allowed', underlying: 'MetaTrader / cTrader',
    summary: 'EAs allowed on most plans (/CFD).',
    detail: '/CFD focused. Stellar Lite and Express plans permit EAs; some plans restrict copy trading. Read your specific contract before depositing.',
    rulesUrl: 'https://fundednext.com' },

  // ─── CONDITIONAL — explicit permission, plan-dependent, or eval-only ──
  { slug: 'tradeify',       name: 'Tradeify',             domain: 'tradeify.co',            policy: 'conditional', underlying: 'Tradovate / Rithmic',
    summary: 'Algos technically allowed but rules have shifted.',
    detail: 'Tradeify\'s FAQ historically permitted automation. As of late 2025/2026 multiple plan-specific restrictions have been added. Email support@tradeify.co for written confirmation before deploying a bot.',
    rulesUrl: 'https://tradeify.co' },
  { slug: 'mffu',           name: 'MyFundedFutures',      domain: 'myfundedfutures.com',    policy: 'conditional', underlying: 'Tradovate / Rithmic',
    summary: 'Plan-dependent. "Starter" tier banned, others vary.',
    detail: 'MFFU added algo restrictions to their Starter plan in 2025. Premium tiers may still allow. Verify with support per-plan before relying on it.',
    rulesUrl: 'https://myfundedfutures.com' },
  { slug: '5ers',           name: 'The 5%ers',            domain: 'the5ers.com',            policy: 'conditional', underlying: 'MetaTrader',
    summary: 'Some plans yes, others banned.',
    detail: '/CFD. "Hyper Growth" plan permits EAs; "Bootcamp" path does not. Read your specific contract.',
    rulesUrl: 'https://the5ers.com' },
  { slug: 'tradingpit',     name: 'The Trading Pit',      domain: 'thetradingpit.com',      policy: 'conditional', underlying: 'NinjaTrader / Rithmic',
    summary: 'Case-by-case, written approval required.',
    detail: 'Will permit some strategies on a funded account if reviewed and approved in writing. Default answer is no.',
    rulesUrl: 'https://thetradingpit.com' },

  // ─── BANNED — algo trading explicitly prohibited (2026) ──────────────
  { slug: 'apex',           name: 'Apex Trader Funding',  domain: 'apextraderfunding.com',  policy: 'banned', underlying: 'Tradovate / Rithmic',
    summary: 'Zero algo. Fully automated trading prohibited.',
    detail: 'Apex updated their rules in 2025 to ban all automated/algorithmic trading on both eval and funded accounts — EAs, copy trading, API bots, anything that\'s not a human placing orders. Detection results in immediate account closure with no payout. Older guides (including some prior versions of this list) said Apex was algo-friendly; that is no longer true.',
    rulesUrl: 'https://apextraderfunding.com/rules' },
  { slug: 'topstep',        name: 'Topstep',              domain: 'topstep.com',            policy: 'banned',  underlying: 'TopstepX / Rithmic',
    summary: 'Automated trading prohibited on funded accounts.',
    detail: 'Manual entries only on funded accounts. EAs / copy trading / API automation are grounds for account closure with no payout. Some semi-auto tools allowed in eval but not funded.',
    rulesUrl: 'https://www.topstep.com/rules-and-scaling-plan' },
  { slug: 'tpt',            name: 'Take Profit Trader',   domain: 'takeprofittrader.com',   policy: 'banned',  underlying: 'Tradovate',
    summary: 'No EAs / bots / API automation on funded accounts.',
    detail: 'Manual trading required on PA accounts. Their order-flow review will detect bot activity and close the account with no payout.',
    rulesUrl: 'https://takeprofittrader.com/rules' },
  { slug: 'bulenox',        name: 'Bulenox',              domain: 'bulenox.com',            policy: 'banned',  underlying: 'Tradovate / Rithmic',
    summary: 'Automated trading prohibited (rule update late 2025).',
    detail: 'Bulenox previously allowed EAs but added a no-automation clause in late 2025. Manual trading only on funded accounts.',
    rulesUrl: 'https://bulenox.com' },
  { slug: 'goat',           name: 'Goat Funded Futures',  domain: 'goatfundedfutures.com',  policy: 'banned',  underlying: 'Tradovate / Rithmic',
    summary: 'Algos prohibited (rule update 2025).',
    detail: 'Goat previously allowed automation but tightened rules in 2025. Manual trading only.',
    rulesUrl: 'https://goatfundedfutures.com' },
  { slug: 'tradersx',       name: 'TradersCentralFund',   domain: 'traderscentralfund.com', policy: 'banned',  underlying: 'Tradovate',
    summary: 'Automated trading no longer permitted.',
    detail: 'Removed their algo-friendly clause in 2025. Funded accounts now require manual execution.',
    rulesUrl: 'https://traderscentralfund.com' },
  { slug: 'leeloo',         name: 'Leeloo Trading',       domain: 'leelootrading.com',      policy: 'banned',  underlying: 'NinjaTrader / Rithmic',
    summary: 'EAs / bots / copy trading prohibited.',
    detail: 'Requires manual execution. Funded account violations result in immediate termination.',
    rulesUrl: 'https://leelootrading.com' },
  { slug: 'uprofit',        name: 'UProfit Trader',       domain: 'uprofitnow.com',         policy: 'banned',  underlying: 'NinjaTrader / Rithmic',
    summary: 'No automated or algorithmic trading.',
    detail: 'Manual trading only on both eval and funded accounts.',
    rulesUrl: 'https://uprofitnow.com' },
  { slug: 'oneup',          name: 'OneUp Trader',         domain: 'oneuptrader.com',        policy: 'banned',  underlying: 'NinjaTrader / Rithmic',
    summary: 'Bots and EAs not permitted on funded accounts.',
    detail: 'Manual trading enforced via order-flow review.',
    rulesUrl: 'https://www.oneuptrader.com' },
  { slug: 'earn2trade',     name: 'Earn2Trade',           domain: 'earn2trade.com',         policy: 'banned',  underlying: 'NinjaTrader / Rithmic',
    summary: 'No automation on funded accounts.',
    detail: 'Eval may permit some EAs but funded (TCP) accounts require manual execution. Treat as banned for live trading.',
    rulesUrl: 'https://earn2trade.com' },
]

const POLICY_LABEL: Record<Policy, string> = {
  allowed:     'Algos Allowed',
  conditional: 'Conditional',
  banned:      'No Algos',
}
const POLICY_BADGE: Record<Policy, string> = {
  allowed:     'bg-green-100 text-green-700 dark:bg-green-900/40 dark:text-green-300',
  conditional: 'bg-amber-100 text-amber-700 dark:bg-amber-900/40 dark:text-amber-300',
  banned:      'bg-red-100 text-red-600 dark:bg-red-900/40 dark:text-red-300',
}
const POLICY_ICON: Record<Policy, any> = {
  allowed: Check, conditional: AlertTriangle, banned: X,
}

function FirmLogo({ domain, name }: { domain: string; name: string }) {
  const sources = [
    `https://logo.clearbit.com/${domain}`,
    `https://www.google.com/s2/favicons?domain=${domain}&sz=128`,
  ]
  const [idx, setIdx] = useState(0)
  const [failed, setFailed] = useState(false)
  if (failed) {
    return (
      <div className="w-10 h-10 rounded-lg bg-slate-200 dark:bg-slate-700 flex items-center justify-center text-slate-600 dark:text-slate-300 font-bold text-xs flex-shrink-0">
        {name.split(' ').map(w => w[0]).slice(0, 2).join('')}
      </div>
    )
  }
  return (
    <div className="w-10 h-10 rounded-lg bg-white dark:bg-slate-800 border border-slate-200 dark:border-slate-700 flex items-center justify-center overflow-hidden flex-shrink-0">
      <img src={sources[idx]} alt={name} referrerPolicy="no-referrer"
           onError={() => idx + 1 < sources.length ? setIdx(idx + 1) : setFailed(true)}
           className="w-full h-full object-contain p-1.5"/>
    </div>
  )
}
function FirmCard({ firm }: { firm: Firm }) {
  const Icon = POLICY_ICON[firm.policy]
  return (
    <div className="rounded-xl border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900 p-4">
      <div className="flex items-start gap-3 mb-2">
        <FirmLogo domain={firm.domain} name={firm.name}/>
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            <h3 className="font-bold text-slate-900 dark:text-slate-100 truncate">{firm.name}</h3>
            <span className={`inline-flex items-center gap-1 text-[10px] font-bold uppercase tracking-wider px-1.5 py-0.5 rounded ${POLICY_BADGE[firm.policy]}`}>
              <Icon size={10}/>
              {POLICY_LABEL[firm.policy]}
            </span>
          </div>
          <div className="text-[11px] text-slate-500 dark:text-slate-400 mt-0.5">Underlying: {firm.underlying}</div>
        </div>
      </div>
      <p className="text-sm font-semibold text-slate-800 dark:text-slate-200 mb-1">{firm.summary}</p>
      <p className="text-xs text-slate-500 dark:text-slate-400 leading-relaxed mb-3">{firm.detail}</p>
      <a href={firm.rulesUrl} target="_blank" rel="noopener noreferrer"
         className="inline-flex items-center gap-1 text-xs font-semibold text-blue-600 hover:text-blue-700 dark:text-blue-400">
        <ExternalLink size={12}/> Official rules
      </a>
    </div>
  )
}

export default function PropFirms() {
  const allowed     = FIRMS.filter(f => f.policy === 'allowed')
  const conditional = FIRMS.filter(f => f.policy === 'conditional')
  const banned      = FIRMS.filter(f => f.policy === 'banned')
  const [filter, setFilter] = useState<'all' | Policy>('all')
  const [q, setQ] = useState('')

  const visible = (firms: Firm[]) => firms.filter(f =>
    (filter === 'all' || f.policy === filter) &&
    (q === '' || f.name.toLowerCase().includes(q.toLowerCase()) || f.underlying.toLowerCase().includes(q.toLowerCase()))
  )

  return (
    <div className="space-y-6 max-w-5xl mx-auto px-4 sm:px-6 py-6">
      <div>
        <h1 className="text-2xl font-bold text-slate-900 dark:text-slate-100 flex items-center gap-2">
          <Building2 size={24}/> Prop Firms
        </h1>
        <p className="text-sm text-slate-500 dark:text-slate-400 mt-1">
          Which futures prop firms permit automated/algorithmic trading and which don't. Pick the right firm before you sign up — using a bot on a no-algo firm gets your funded account closed with no payout.
        </p>
      </div>

      {/* Disclaimer banner */}
      <div className="rounded-xl border border-amber-200 dark:border-amber-800/50 bg-amber-50 dark:bg-amber-900/20 p-4 text-sm">
        <div className="flex items-start gap-3">
          <ShieldCheck size={18} className="text-amber-600 dark:text-amber-400 flex-shrink-0 mt-0.5"/>
          <div className="text-amber-900 dark:text-amber-200">
            <strong>The honest truth in 2026:</strong> almost every major futures prop firm has banned or restricted algo trading. Rules change quarterly and firms don't always announce updates. <strong>Always verify in writing</strong> with the firm's support team before depositing — get an email confirming "yes, you may use a third-party algorithmic platform on a funded account." Theta Algos is not responsible for account closures resulting from rule violations.
          </div>
        </div>
      </div>

      {/* What to do if your firm is on the banned list */}
      <div className="rounded-xl border border-blue-200 dark:border-blue-800/50 bg-blue-50/60 dark:bg-blue-900/20 p-4 text-sm">
        <div className="flex items-start gap-3">
          <AlertTriangle size={18} className="text-blue-600 dark:text-blue-400 flex-shrink-0 mt-0.5"/>
          <div className="text-blue-900 dark:text-blue-200">
            <strong>If your firm is on the banned list:</strong> use <Link to="/app/account-signals" className="font-bold underline">Account Signals</Link> instead. The bot watches the same strategies and emails/texts you a setup the moment it forms — you place the order yourself in the broker's interface. Stays compliant with every prop firm's "manual trading only" rule because <em>you</em> are the one trading.
          </div>
        </div>
      </div>

      {/* Filter bar */}
      <div className="flex flex-wrap items-center gap-3">
        <div className="inline-flex rounded-lg bg-slate-100 dark:bg-slate-800 p-0.5">
          {(['all', 'allowed', 'conditional', 'banned'] as const).map(f => (
            <button key={f} onClick={() => setFilter(f)}
              className={`px-3 py-1.5 text-xs font-semibold rounded-md transition ${ filter === f ? 'bg-white dark:bg-slate-700 text-slate-900 dark:text-slate-100 shadow-sm' : 'text-slate-500 dark:text-slate-400 hover:text-slate-800 dark:hover:text-slate-200' }`}>
              {f === 'all' ? 'All firms' : POLICY_LABEL[f]}
            </button>
          ))}
        </div>
        <input type="text" value={q} onChange={e => setQ(e.target.value)}
          placeholder="Search firms or platforms…"
          className="flex-1 min-w-[200px] max-w-sm border border-slate-300 rounded-lg px-3 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 dark:border-slate-700 dark:bg-slate-800 dark:text-slate-100"/>
      </div>

      {/* Sections */}
      {(filter === 'all' || filter === 'allowed') && visible(allowed).length > 0 && (
        <section>
          <div className="flex items-center gap-2 mb-3">
            <Check size={16} className="text-green-600"/>
            <h2 className="text-sm font-bold uppercase tracking-widest text-slate-700 dark:text-slate-200">Algo-friendly — use these</h2>
            <span className="text-xs text-slate-400">({visible(allowed).length})</span>
          </div>
          <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
            {visible(allowed).map(f => <FirmCard key={f.slug} firm={f}/>)}
          </div>
        </section>
      )}

      {(filter === 'all' || filter === 'conditional') && visible(conditional).length > 0 && (
        <section>
          <div className="flex items-center gap-2 mb-3">
            <AlertTriangle size={16} className="text-amber-600"/>
            <h2 className="text-sm font-bold uppercase tracking-widest text-slate-700 dark:text-slate-200">Conditional — read the fine print</h2>
            <span className="text-xs text-slate-400">({visible(conditional).length})</span>
          </div>
          <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
            {visible(conditional).map(f => <FirmCard key={f.slug} firm={f}/>)}
          </div>
        </section>
      )}

      {(filter === 'all' || filter === 'banned') && visible(banned).length > 0 && (
        <section>
          <div className="flex items-center gap-2 mb-3">
            <X size={16} className="text-red-500"/>
            <h2 className="text-sm font-bold uppercase tracking-widest text-slate-700 dark:text-slate-200">No algos — avoid for bot trading</h2>
            <span className="text-xs text-slate-400">({visible(banned).length})</span>
          </div>
          <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
            {visible(banned).map(f => <FirmCard key={f.slug} firm={f}/>)}
          </div>
        </section>
      )}

      <div className="text-xs text-slate-400 dark:text-slate-500 leading-relaxed pt-4 border-t border-slate-200 dark:border-slate-800">
        Don't see a firm? Most futures prop firms route through <strong>Tradovate</strong> or <strong>Rithmic</strong> under the hood, so as long as the firm permits algos and grants you API access, this platform's Tradovate connector will work. Email a screenshot of their algo policy to support@thetaalgos.com and we'll add it.
      </div>

      <div className="flex flex-wrap gap-2 pt-2">
        <Link to="/app/live" className="text-xs font-semibold text-blue-600 hover:underline">← Connect a broker account</Link>
      </div>
    </div>
  )
}
