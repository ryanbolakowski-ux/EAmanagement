import { Link } from 'react-router-dom'

export default function Privacy() {
  return (
    <div className="max-w-3xl mx-auto px-6 py-12 text-slate-800 dark:text-slate-200">
      <Link to="/" className="text-sm text-violet-600 dark:text-violet-400 hover:underline">← Theta Algos</Link>
      <h1 className="text-3xl font-extrabold mt-4 mb-2 text-slate-900 dark:text-white">Privacy Policy</h1>
      <p className="text-sm text-slate-500 dark:text-slate-400 mb-8">Effective May 2026 · Last updated {new Date().toLocaleDateString('en-US', { month: 'long', day: 'numeric', year: 'numeric' })}</p>

      <div className="prose dark:prose-invert prose-sm space-y-6">

        <section>
          <h2 className="text-xl font-bold">1. Who we are</h2>
          <p>Theta Algos LLC ("Theta Algos", "we", "us") is a Florida limited liability company that operates a software platform for algorithmic trading strategy design, backtesting, paper trading, and broker connectivity at thetaalgos.com. Contact: <a href="mailto:legal@thetaalgos.com" className="text-violet-600">legal@thetaalgos.com</a>.</p>
        </section>

        <section>
          <h2 className="text-xl font-bold">2. What we collect</h2>
          <ul className="list-disc list-inside space-y-1">
            <li><strong>Account data</strong>: email, username, hashed password, subscription tier, IP at signup.</li>
            <li><strong>Trading data</strong>: strategies you build, backtest runs, paper-trading sessions, live sessions, trade records. We retain this for the life of your account and 6 years after closure (FINRA-aligned).</li>
            <li><strong>Broker credentials</strong>: encrypted with AES-256-GCM before storage. Decrypted only in-memory when placing an order. Never logged. Deleted within 30 days of account closure.</li>
            <li><strong>Usage data</strong>: pages visited, features used, error logs. Used solely for product improvement.</li>
            <li><strong>Payment data</strong>: handled by Stripe; we never see your card number. We retain transaction IDs and last-4 digits for receipts.</li>
          </ul>
        </section>

        <section>
          <h2 className="text-xl font-bold">3. Third-party processors</h2>
          <p>We share the minimum necessary data with the following sub-processors:</p>
          <ul className="list-disc list-inside space-y-1">
            <li><strong>Stripe</strong> — payment processing</li>
            <li><strong>Resend</strong> — transactional email (signal alerts, password resets)</li>
            <li><strong>Polygon.io · Databento</strong> — market data feed (no personal data shared)</li>
            <li><strong>Broker APIs you connect</strong> (Tradier, Tradovate, Alpaca, etc.) — we send order parameters; they return fills</li>
            <li><strong>Cloudflare</strong> — DDoS protection + DNS</li>
            <li><strong>Hetzner</strong> — server hosting (EU-based, GDPR-compliant)</li>
          </ul>
        </section>

        <section>
          <h2 className="text-xl font-bold">4. Your rights (GDPR + CCPA)</h2>
          <p>You can: (a) request a copy of your data; (b) request deletion of your account and data; (c) opt out of marketing emails (transactional emails — signal alerts, receipts — cannot be opted out of while subscribed); (d) request correction of inaccuracies; (e) port your data in JSON format. Submit requests to privacy@thetaalgos.com — we respond within 30 days.</p>
        </section>

        <section>
          <h2 className="text-xl font-bold">5. Cookies</h2>
          <p>We use essential cookies only (authentication tokens, your theme preference). No advertising cookies, no analytics cookies. See our <Link to="/cookies" className="text-violet-600 underline">Cookie Notice</Link> for details.</p>
        </section>

        <section>
          <h2 className="text-xl font-bold">6. Children</h2>
          <p>Theta Algos is not directed at children under 18. We do not knowingly collect data from minors.</p>
        </section>

        <section>
          <h2 className="text-xl font-bold">7. Changes</h2>
          <p>We will email you at least 14 days before any material change to this policy. Continued use after the effective date constitutes acceptance.</p>
        </section>

        <section>
          <h2 className="text-xl font-bold">8. Contact</h2>
          <p>Theta Algos LLC · 19839 Bridgetown Lp, Venice FL 34293 · <a href="mailto:legal@thetaalgos.com" className="text-violet-600">legal@thetaalgos.com</a></p>
        </section>
      </div>
    </div>
  )
}
