import { Link } from 'react-router-dom'

export default function Cookies() {
  return (
    <div className="max-w-3xl mx-auto px-6 py-12 text-slate-800 dark:text-slate-200">
      <Link to="/" className="text-sm text-violet-600 dark:text-violet-400 hover:underline">← Theta Algos</Link>
      <h1 className="text-3xl font-extrabold mt-4 mb-2 text-slate-900 dark:text-white">Cookie Notice</h1>
      <p className="text-sm text-slate-500 dark:text-slate-400 mb-8">Last updated {new Date().toLocaleDateString('en-US', { month: 'long', day: 'numeric', year: 'numeric' })}</p>

      <div className="prose dark:prose-invert prose-sm space-y-6">

        <section>
          <h2 className="text-xl font-bold">We use essential cookies only</h2>
          <p>Theta Algos uses cookies and similar storage technologies that are <strong>strictly necessary</strong> for the platform to function. We do not run advertising trackers, behavioral analytics, or third-party cookie networks.</p>
        </section>

        <section>
          <h2 className="text-xl font-bold">What we set</h2>
          <table className="w-full text-xs border border-slate-200 dark:border-slate-700 rounded-lg overflow-hidden">
            <thead className="bg-slate-100 dark:bg-slate-800">
              <tr>
                <th className="px-3 py-2 text-left">Name</th>
                <th className="px-3 py-2 text-left">Purpose</th>
                <th className="px-3 py-2 text-left">Lifetime</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100 dark:divide-slate-800">
              <tr>
                <td className="px-3 py-2 font-mono">edge_token</td>
                <td className="px-3 py-2">Session authentication (signed JWT)</td>
                <td className="px-3 py-2">30 days or until logout</td>
              </tr>
              <tr>
                <td className="px-3 py-2 font-mono">edge_refresh</td>
                <td className="px-3 py-2">Refresh token to renew session</td>
                <td className="px-3 py-2">30 days</td>
              </tr>
              <tr>
                <td className="px-3 py-2 font-mono">edge_theme</td>
                <td className="px-3 py-2">Light / dark mode preference</td>
                <td className="px-3 py-2">365 days</td>
              </tr>
              <tr>
                <td className="px-3 py-2 font-mono">edge_device_pref</td>
                <td className="px-3 py-2">Browser vs mobile layout choice</td>
                <td className="px-3 py-2">365 days</td>
              </tr>
            </tbody>
          </table>
        </section>

        <section>
          <h2 className="text-xl font-bold">What we don't set</h2>
          <ul className="list-disc list-inside space-y-1">
            <li>Google Analytics or any other analytics cookies</li>
            <li>Facebook Pixel or any advertising tracker</li>
            <li>Third-party share buttons that leak referrer data</li>
            <li>Re-targeting or attribution cookies</li>
            <li>Heatmap or session-replay cookies</li>
          </ul>
        </section>

        <section>
          <h2 className="text-xl font-bold">Third-party cookies you may encounter</h2>
          <p>When you go through Stripe checkout or accept a broker OAuth flow (Schwab, IBKR, Alpaca), those services may set their own cookies governed by their privacy policies. We do not control these cookies.</p>
        </section>

        <section>
          <h2 className="text-xl font-bold">Opting out</h2>
          <p>Because we only use strictly-necessary cookies, browser cookie-blocking will prevent you from staying logged in. You can clear all Theta Algos cookies by logging out and clearing site data in your browser settings.</p>
        </section>

        <section>
          <h2 className="text-xl font-bold">Questions</h2>
          <p>Email <a href="mailto:privacy@thetaalgos.com" className="text-violet-600">privacy@thetaalgos.com</a>.</p>
        </section>
      </div>
    </div>
  )
}
