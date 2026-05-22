import { Link } from 'react-router-dom'
import { CheckCircle2, X, BarChart2, ArrowLeft } from 'lucide-react'

const TIERS = [
  {
    id: 'free_trial',
    name: 'Free Trial',
    tierLabel: 'Tier 1',
    price: '$0',
    originalPrice: '$49',
    per: 'per month',
    promoNote: '30 days · no card required',
    desc: 'See what the bot picks every morning at 8:30 ET. Full futures signals + options scanner preview, paper trading only.',
    highlight: false,
    cta: 'Start Free Trial',
    features: {
      'Strategy builder + backtesting':       true,
      'Paper trading':                        true,
      'Futures signals (Apex/TPT/Topstep)':   true,
      'Options scanner (1+4 morning email)':  'Preview only',
      'Tradier broker integration':           false,
      'Auto-execute options (no manual confirm)': false,
      'Universe size (tickers scanned)':      '500',
      'Historical data':                      '1 year',
      'Support':                              'Community',
    },
  },
  {
    id: 'tier_2',
    name: 'Futures Signals',
    tierLabel: 'Tier 2',
    price: '$49',
    per: 'per month',
    desc: 'For prop-firm traders (Apex, TPT, Topstep, Topstep X). ICT-based signals on ES/NQ/RTY/YM delivered as email + push so you place trades manually inside your prop rules.',
    highlight: false,
    cta: 'Get Started',
    features: {
      'Strategy builder + backtesting':       true,
      'Paper trading':                        true,
      'Futures signals (Apex/TPT/Topstep)':   true,
      'Options scanner (1+4 morning email)':  false,
      'Tradier broker integration':           false,
      'Auto-execute options (no manual confirm)': false,
      'Universe size (tickers scanned)':      'ES/NQ/RTY/YM',
      'Historical data':                      '2+ years',
      'Support':                              'Email',
    },
  },
  {
    id: 'tier_3',
    name: 'Options Scanner',
    tierLabel: 'Tier 3',
    price: '$99',
    per: 'per month',
    desc: 'The full 3,000+ ticker pre-market scanner. Daily 8:30 ET email with 1 top pick + 4 runners-up across Low-Float Squeeze, 52-Week Breakout, Pre-Market Gap, and Oracle. You place the trade in your broker.',
    highlight: false,
    cta: 'Get Started',
    features: {
      'Strategy builder + backtesting':       true,
      'Paper trading':                        true,
      'Futures signals (Apex/TPT/Topstep)':   true,
      'Options scanner (1+4 morning email)':  true,
      'Tradier broker integration':           false,
      'Auto-execute options (no manual confirm)': false,
      'Universe size (tickers scanned)':      '3,000+',
      'Historical data':                      '2+ years',
      'Support':                              'Email',
    },
  },
  {
    id: 'tier_4',
    name: 'Options Live',
    tierLabel: 'Tier 4',
    price: '$199',
    per: 'per month',
    desc: 'Same scanner — but Confirm now places real orders through your connected Tradier account. Live greeks, real bid/ask, sandbox-to-production with one toggle.',
    highlight: true,
    cta: 'Get Started',
    tag: 'Most Popular',
    features: {
      'Strategy builder + backtesting':       true,
      'Paper trading':                        true,
      'Futures signals (Apex/TPT/Topstep)':   true,
      'Options scanner (1+4 morning email)':  true,
      'Tradier broker integration':           true,
      'Auto-execute options (no manual confirm)': false,
      'Universe size (tickers scanned)':      '3,000+',
      'Historical data':                      '2+ years',
      'Support':                              'Priority Email',
    },
  },
  {
    id: 'tier_5',
    name: 'Fully Automated',
    tierLabel: 'Tier 5',
    price: '$399',
    per: 'per month',
    desc: 'Zero clicks. The bot scans, picks, sizes, places, manages, and exits — automatically. Multi-strategy concurrent execution including the Wheel. For the trader who wants pure passive income.',
    highlight: false,
    cta: 'Get Started',
    features: {
      'Strategy builder + backtesting':       true,
      'Paper trading':                        true,
      'Futures signals (Apex/TPT/Topstep)':   true,
      'Options scanner (1+4 morning email)':  true,
      'Tradier broker integration':           true,
      'Auto-execute options (no manual confirm)': true,
      'Universe size (tickers scanned)':      '3,000+',
      'Historical data':                      '5+ years',
      'Support':                              'Priority + Chat',
    },
  },
]

const ALL_FEATURES = [
  'Strategy builder + backtesting',
  'Paper trading',
  'Futures signals (Apex/TPT/Topstep)',
  'Options scanner (1+4 morning email)',
  'Tradier broker integration',
  'Auto-execute options (no manual confirm)',
  'Universe size (tickers scanned)',
  'Historical data',
  'Support',
]

function FeatureValue({ val }: { val: boolean | string }) {
  if (val === true)  return <CheckCircle2 size={16} className="mx-auto text-green-500"/>
  if (val === false) return <X size={16} className="mx-auto text-slate-300 dark:text-slate-600"/>
  return <span className="text-xs font-medium text-slate-700 dark:text-slate-200">{val}</span>
}

export default function Pricing() {
  return (
    <div className="min-h-screen bg-white dark:bg-slate-900">
      {/* Nav */}
      <nav className="sticky top-0 z-50 bg-white backdrop-blur border-b border-slate-200 dark:bg-slate-900 dark:border-slate-700">
        <div className="max-w-7xl mx-auto px-6 h-16 flex items-center justify-between">
          <Link to="/" className="flex items-center gap-2.5">
            <BarChart2 size={22} className="text-blue-600"/>
            <span className="font-bold text-slate-900 text-lg dark:text-slate-100">Theta Algos</span>
          </Link>
          <div className="flex items-center gap-3">
            <Link to="/login" className="text-sm font-medium text-slate-600 hover:text-slate-900 px-3 py-2 dark:text-slate-300">Sign in</Link>
            <Link to="/register" className="text-sm font-semibold bg-blue-600 hover:bg-blue-700 text-white px-4 py-2 rounded-lg transition-colors">
              Start Free Trial
            </Link>
          </div>
        </div>
      </nav>

      <div className="max-w-7xl mx-auto px-6 py-16">
        {/* Back link */}
        <Link to="/" className="inline-flex items-center gap-1.5 text-sm text-slate-500 hover:text-slate-800 mb-8 transition-colors dark:text-slate-400">
          <ArrowLeft size={14}/> Back to home
        </Link>

        {/* Header */}
        <div className="text-center mb-14">
          <div className="text-sm font-semibold text-blue-600 uppercase tracking-widest mb-3">Pricing</div>
          <h1 className="text-5xl font-extrabold text-slate-900 mb-4 dark:text-slate-100">Simple, transparent pricing</h1>
          <p className="text-slate-500 text-lg max-w-xl mx-auto dark:text-slate-400">Start for free. Upgrade as you scale. All plans include a 30-day free trial on signup.</p>
        </div>

        {/* Plan cards */}
        <div className="grid grid-cols-1 md:grid-cols-3 lg:grid-cols-5 gap-4 mb-20">
          {TIERS.map((tier) => (
            <div key={tier.id} className={`relative rounded-2xl border p-5 flex flex-col ${ tier.highlight ? 'border-blue-500 bg-blue-600 text-white shadow-xl shadow-blue-100' : 'border-slate-200 bg-white' }`}>
              {tier.tag && (
                <div className="absolute -top-3 left-1/2 -translate-x-1/2 bg-amber-400 text-amber-900 text-xs font-bold px-3 py-1 rounded-full whitespace-nowrap">
                  {tier.tag}
                </div>
              )}

              <div className={`text-base font-bold mb-0.5 ${tier.highlight ? 'text-white' : 'text-slate-900'}`}>
                {tier.name}
              </div>
              <div className={`text-xs font-semibold uppercase tracking-wider mb-3 ${tier.highlight ? 'text-blue-200' : 'text-blue-600'}`}>
                {tier.tierLabel}
              </div>
              <div className="mb-0.5">
                {tier.originalPrice && (
                  <div className={`text-sm font-semibold line-through leading-none mb-1 ${tier.highlight ? 'text-blue-200' : 'text-slate-400'}`}>
                    {tier.originalPrice}
                  </div>
                )}
                <div className={`text-2xl font-extrabold leading-tight ${tier.highlight ? 'text-white' : 'text-slate-900'}`}>
                  {tier.price}
                </div>
              </div>
              {tier.per && (
                <div className={`text-xs mb-1 ${tier.highlight ? 'text-blue-200' : 'text-slate-500'}`}>{tier.per}</div>
              )}
              {tier.promoNote && (
                <div className={`text-[11px] font-semibold mb-3 ${tier.highlight ? 'text-amber-200' : 'text-amber-600'}`}>
                  {tier.promoNote}
                </div>
              )}
              <div className={`text-xs leading-relaxed mb-5 flex-1 ${tier.highlight ? 'text-blue-100' : 'text-slate-600'}`}>
                {tier.desc}
              </div>

              <Link
                to="/register"
                className={`text-center text-sm font-semibold py-2.5 rounded-xl transition-colors ${ tier.highlight ? 'bg-white text-blue-700 hover:bg-blue-50' : 'bg-blue-600 text-white hover:bg-blue-700' }`}
              >
                {tier.cta}
              </Link>
            </div>
          ))}
        </div>

        {/* Feature comparison table */}
        <div className="mb-16">
          <h2 className="text-2xl font-bold text-slate-900 mb-6 dark:text-slate-100">Full feature comparison</h2>
          <div className="overflow-x-auto rounded-2xl border border-slate-200 shadow-sm dark:border-slate-700">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-slate-200 bg-slate-50 dark:bg-slate-900 dark:border-slate-700">
                  <th className="text-left px-5 py-4 font-semibold text-slate-700 w-48 dark:text-slate-200">Feature</th>
                  {TIERS.map((t) => (
                    <th key={t.id} className={`px-4 py-4 font-semibold text-center ${t.highlight ? 'bg-blue-600 text-white' : 'text-slate-700'}`}>
                      {t.name}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {ALL_FEATURES.map((feat, fi) => (
                  <tr key={feat} className={`border-b border-slate-100 ${fi % 2 === 0 ? 'bg-white' : 'bg-slate-50/50'} dark:border-slate-800`}>
                    <td className="px-5 py-3.5 font-medium text-slate-700 dark:text-slate-200">{feat}</td>
                    {TIERS.map((t) => (
                      <td key={t.id} className={`px-4 py-3.5 text-center ${t.highlight ? 'bg-blue-50' : ''}`}>
                        <FeatureValue val={(t.features as any)[feat]}/>
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>

        {/* FAQ */}
        <div className="max-w-2xl mx-auto mb-20">
          <h2 className="text-2xl font-bold text-slate-900 mb-8 text-center dark:text-slate-100">Frequently asked questions</h2>
          <div className="space-y-6">
            {[
              {
                q: 'What is included in the free trial?',
                a: 'The 30-day free trial includes paper trading and backtesting on up to 1 year of historical data. No live trading access and no credit card required to start.',
              },
              {
                q: 'Which brokers are supported?',
                a: 'The platform currently integrates with Tradovate for live execution, including both demo and live environments. Additional broker integrations (Rithmic, NinjaTrader) are on the roadmap.',
              },
              {
                q: 'What instruments can I trade?',
                a: 'The MVP supports ES (S&P 500 futures) and NQ (Nasdaq futures). Additional futures instruments will be added in future releases.',
              },
              {
                q: 'Can I cancel at any time?',
                a: 'Yes. All plans are month-to-month with no long-term commitment. Cancel anytime and your access continues until the end of your billing period.',
              },
              {
                q: 'How are my broker credentials stored?',
                a: 'All broker API credentials are encrypted at rest using Fernet symmetric encryption before being stored in the database. They are never stored in plaintext.',
              },
            ].map(({ q, a }) => (
              <div key={q} className="border border-slate-200 rounded-xl p-5 dark:border-slate-700">
                <h3 className="font-semibold text-slate-900 mb-2 dark:text-slate-100">{q}</h3>
                <p className="text-sm text-slate-500 leading-relaxed dark:text-slate-400">{a}</p>
              </div>
            ))}
          </div>
        </div>

        {/* CTA */}
        <div className="bg-blue-600 rounded-3xl p-10 text-center text-white">
          <h2 className="text-3xl font-bold mb-3">Start your free trial today</h2>
          <p className="text-blue-100 mb-7 max-w-md mx-auto">30 days free, no credit card required. Paper trading included from day one.</p>
          <Link to="/register" className="inline-flex items-center gap-2 bg-white text-blue-700 font-bold px-7 py-3.5 rounded-xl hover:bg-blue-50 transition-colors text-sm dark:bg-slate-900">
            Create Free Account
          </Link>
        </div>

        {/* Risk disclaimer */}
        <div className="mt-10 p-4 bg-amber-50 border border-amber-200 rounded-xl text-xs text-amber-700">
          <strong>Risk Disclosure:</strong> Futures and options trading involve substantial risk of loss and are not appropriate for every investor. Options can expire worthless — you can lose the entire premium paid on each trade. Past performance, backtest results, and paper trading do not guarantee future returns. Theta Algos LLC is a software platform, not a registered investment adviser, broker-dealer, or futures commission merchant — nothing on this site is investment advice or a recommendation to trade any security or contract. Prop-firm account rules vary and change frequently; account closures resulting from rule violations are your sole responsibility. Always trade with capital you can afford to lose.
        </div>
      </div>

      {/* Footer */}
      <footer className="border-t border-slate-200 bg-slate-50 dark:bg-slate-900 dark:border-slate-700">
        <div className="max-w-6xl mx-auto px-6 py-8 flex flex-col md:flex-row items-center justify-between gap-4">
          <div className="flex items-center gap-2">
            <BarChart2 size={16} className="text-blue-600"/>
            <span className="font-semibold text-slate-700 dark:text-slate-200">Theta Algos</span>
          </div>
          <div className="flex gap-6 text-sm text-slate-500 dark:text-slate-400">
            <Link to="/" className="hover:text-slate-800">Home</Link>
            <Link to="/login" className="hover:text-slate-800">Sign In</Link>
            <Link to="/register" className="hover:text-slate-800">Register</Link>
          </div>
          <p className="text-xs text-slate-400 dark:text-slate-500">© {new Date().getFullYear()} Theta Algos</p>
        </div>
      </footer>
    </div>
  )
}
