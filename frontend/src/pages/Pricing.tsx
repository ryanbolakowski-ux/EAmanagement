import { Link } from 'react-router-dom'
import { CheckCircle2, X, BarChart2, ArrowLeft } from 'lucide-react'

const TIERS = [
  {
    id: 'free_trial',
    name: 'Free Trial',
    price: '$0',
    per: '30 days',
    desc: 'Explore the platform with no commitment.',
    highlight: false,
    cta: 'Start Free Trial',
    features: {
      'Strategy builder': true,
      'Backtesting': 'Limited',
      'Optimization engine': false,
      'Paper trading': true,
      'Live trading': false,
      'Broker accounts': '0',
      'Historical data': '6 months',
      'All timeframes': true,
      'Support': 'Community',
    },
  },
  {
    id: 'tier_1',
    name: 'Backtest',
    price: '$49',
    per: 'per month',
    desc: 'Full historical analysis without live trading.',
    highlight: false,
    cta: 'Get Started',
    features: {
      'Strategy builder': true,
      'Backtesting': true,
      'Optimization engine': true,
      'Paper trading': false,
      'Live trading': false,
      'Broker accounts': '0',
      'Historical data': '2+ years',
      'All timeframes': true,
      'Support': 'Email',
    },
  },
  {
    id: 'tier_3',
    name: 'Live Trader',
    price: '$149',
    per: 'per month',
    desc: 'Full suite with live execution on up to 5 accounts.',
    highlight: true,
    cta: 'Get Started',
    tag: 'Most Popular',
    features: {
      'Strategy builder': true,
      'Backtesting': true,
      'Optimization engine': true,
      'Paper trading': true,
      'Live trading': true,
      'Broker accounts': 'Up to 5',
      'Historical data': '2+ years',
      'All timeframes': true,
      'Support': 'Priority Email',
    },
  },
  {
    id: 'tier_4',
    name: 'Advanced',
    price: '$349',
    per: 'per month',
    desc: 'Scale to 20 live accounts with everything included.',
    highlight: false,
    cta: 'Get Started',
    features: {
      'Strategy builder': true,
      'Backtesting': true,
      'Optimization engine': true,
      'Paper trading': true,
      'Live trading': true,
      'Broker accounts': 'Up to 20',
      'Historical data': '2+ years',
      'All timeframes': true,
      'Support': 'Priority + Chat',
    },
  },
  {
    id: 'tier_5',
    name: 'Enterprise',
    price: 'Custom',
    per: '',
    desc: 'Unlimited accounts. White-label options available.',
    highlight: false,
    cta: 'Contact Us',
    features: {
      'Strategy builder': true,
      'Backtesting': true,
      'Optimization engine': true,
      'Paper trading': true,
      'Live trading': true,
      'Broker accounts': 'Unlimited',
      'Historical data': 'Full archive',
      'All timeframes': true,
      'Support': 'Dedicated',
    },
  },
]

const ALL_FEATURES = [
  'Strategy builder',
  'Backtesting',
  'Optimization engine',
  'Paper trading',
  'Live trading',
  'Broker accounts',
  'Historical data',
  'All timeframes',
  'Support',
]

function FeatureValue({ val }: { val: boolean | string }) {
  if (val === true)  return <CheckCircle2 size={16} className="mx-auto text-green-500"/>
  if (val === false) return <X size={16} className="mx-auto text-slate-300"/>
  return <span className="text-xs font-medium text-slate-700">{val}</span>
}

export default function Pricing() {
  return (
    <div className="min-h-screen bg-white">
      {/* Nav */}
      <nav className="sticky top-0 z-50 bg-white/90 backdrop-blur border-b border-slate-200">
        <div className="max-w-7xl mx-auto px-6 h-16 flex items-center justify-between">
          <Link to="/" className="flex items-center gap-2.5">
            <BarChart2 size={22} className="text-blue-600"/>
            <span className="font-bold text-slate-900 text-lg">Edge Asset Management</span>
          </Link>
          <div className="flex items-center gap-3">
            <Link to="/login" className="text-sm font-medium text-slate-600 hover:text-slate-900 px-3 py-2">Sign in</Link>
            <Link to="/register" className="text-sm font-semibold bg-blue-600 hover:bg-blue-700 text-white px-4 py-2 rounded-lg transition-colors">
              Start Free Trial
            </Link>
          </div>
        </div>
      </nav>

      <div className="max-w-7xl mx-auto px-6 py-16">
        {/* Back link */}
        <Link to="/" className="inline-flex items-center gap-1.5 text-sm text-slate-500 hover:text-slate-800 mb-8 transition-colors">
          <ArrowLeft size={14}/> Back to home
        </Link>

        {/* Header */}
        <div className="text-center mb-14">
          <div className="text-sm font-semibold text-blue-600 uppercase tracking-widest mb-3">Pricing</div>
          <h1 className="text-5xl font-extrabold text-slate-900 mb-4">Simple, transparent pricing</h1>
          <p className="text-slate-500 text-lg max-w-xl mx-auto">Start for free. Upgrade as you scale. All plans include a 30-day free trial on signup.</p>
        </div>

        {/* Plan cards */}
        <div className="grid grid-cols-1 md:grid-cols-3 lg:grid-cols-5 gap-4 mb-20">
          {TIERS.map((tier) => (
            <div key={tier.id} className={`relative rounded-2xl border p-5 flex flex-col ${
              tier.highlight
                ? 'border-blue-500 bg-blue-600 text-white shadow-xl shadow-blue-100'
                : 'border-slate-200 bg-white'
            }`}>
              {tier.tag && (
                <div className="absolute -top-3 left-1/2 -translate-x-1/2 bg-amber-400 text-amber-900 text-xs font-bold px-3 py-1 rounded-full whitespace-nowrap">
                  {tier.tag}
                </div>
              )}

              <div className={`text-xs font-semibold uppercase tracking-wider mb-2 ${tier.highlight ? 'text-blue-200' : 'text-blue-600'}`}>
                {tier.id === 'free_trial' ? 'Free' : tier.id.replace('_', ' ').toUpperCase()}
              </div>
              <div className={`text-2xl font-extrabold mb-0.5 ${tier.highlight ? 'text-white' : 'text-slate-900'}`}>
                {tier.price}
              </div>
              {tier.per && (
                <div className={`text-xs mb-3 ${tier.highlight ? 'text-blue-200' : 'text-slate-400'}`}>{tier.per}</div>
              )}
              <div className={`text-xs leading-relaxed mb-5 flex-1 ${tier.highlight ? 'text-blue-100' : 'text-slate-500'}`}>
                {tier.desc}
              </div>

              <Link
                to="/register"
                className={`text-center text-sm font-semibold py-2.5 rounded-xl transition-colors ${
                  tier.highlight
                    ? 'bg-white text-blue-700 hover:bg-blue-50'
                    : 'bg-blue-600 text-white hover:bg-blue-700'
                }`}
              >
                {tier.cta}
              </Link>
            </div>
          ))}
        </div>

        {/* Feature comparison table */}
        <div className="mb-16">
          <h2 className="text-2xl font-bold text-slate-900 mb-6">Full feature comparison</h2>
          <div className="overflow-x-auto rounded-2xl border border-slate-200 shadow-sm">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-slate-200 bg-slate-50">
                  <th className="text-left px-5 py-4 font-semibold text-slate-700 w-48">Feature</th>
                  {TIERS.map((t) => (
                    <th key={t.id} className={`px-4 py-4 font-semibold text-center ${t.highlight ? 'bg-blue-600 text-white' : 'text-slate-700'}`}>
                      {t.name}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {ALL_FEATURES.map((feat, fi) => (
                  <tr key={feat} className={`border-b border-slate-100 ${fi % 2 === 0 ? 'bg-white' : 'bg-slate-50/50'}`}>
                    <td className="px-5 py-3.5 font-medium text-slate-700">{feat}</td>
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
          <h2 className="text-2xl font-bold text-slate-900 mb-8 text-center">Frequently asked questions</h2>
          <div className="space-y-6">
            {[
              {
                q: 'What is included in the free trial?',
                a: 'The 30-day free trial includes paper trading and limited backtesting (6 months of data). No live trading access and no credit card required to start.',
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
              <div key={q} className="border border-slate-200 rounded-xl p-5">
                <h3 className="font-semibold text-slate-900 mb-2">{q}</h3>
                <p className="text-sm text-slate-500 leading-relaxed">{a}</p>
              </div>
            ))}
          </div>
        </div>

        {/* CTA */}
        <div className="bg-blue-600 rounded-3xl p-10 text-center text-white">
          <h2 className="text-3xl font-bold mb-3">Start your free trial today</h2>
          <p className="text-blue-100 mb-7 max-w-md mx-auto">30 days free, no credit card required. Paper trading included from day one.</p>
          <Link to="/register" className="inline-flex items-center gap-2 bg-white text-blue-700 font-bold px-7 py-3.5 rounded-xl hover:bg-blue-50 transition-colors text-sm">
            Create Free Account
          </Link>
        </div>

        {/* Risk disclaimer */}
        <div className="mt-10 p-4 bg-amber-50 border border-amber-200 rounded-xl text-xs text-amber-700">
          <strong>Risk Disclosure:</strong> Futures trading involves substantial risk of loss and is not appropriate for all investors. Past performance is not necessarily indicative of future results. Edge Asset Management is a software platform, not a registered investment adviser.
        </div>
      </div>

      {/* Footer */}
      <footer className="border-t border-slate-200 bg-slate-50">
        <div className="max-w-6xl mx-auto px-6 py-8 flex flex-col md:flex-row items-center justify-between gap-4">
          <div className="flex items-center gap-2">
            <BarChart2 size={16} className="text-blue-600"/>
            <span className="font-semibold text-slate-700">Edge Asset Management</span>
          </div>
          <div className="flex gap-6 text-sm text-slate-500">
            <Link to="/" className="hover:text-slate-800">Home</Link>
            <Link to="/login" className="hover:text-slate-800">Sign In</Link>
            <Link to="/register" className="hover:text-slate-800">Register</Link>
          </div>
          <p className="text-xs text-slate-400">© {new Date().getFullYear()} Edge Asset Management</p>
        </div>
      </footer>
    </div>
  )
}
