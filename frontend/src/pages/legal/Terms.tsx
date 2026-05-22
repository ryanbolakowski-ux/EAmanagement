import { Link } from 'react-router-dom'

export default function Terms() {
  return (
    <div className="max-w-3xl mx-auto px-6 py-12 text-slate-800 dark:text-slate-200">
      <Link to="/" className="text-sm text-violet-600 dark:text-violet-400 hover:underline">← Theta Algos</Link>
      <h1 className="text-3xl font-extrabold mt-4 mb-2 text-slate-900 dark:text-white">Terms of Service</h1>
      <p className="text-sm text-slate-500 dark:text-slate-400 mb-8">Effective May 2026 · Last updated {new Date().toLocaleDateString('en-US', { month: 'long', day: 'numeric', year: 'numeric' })}</p>

      <div className="prose dark:prose-invert prose-sm space-y-6">

        <section className="rounded-xl bg-amber-50 dark:bg-amber-900/20 border border-amber-200 dark:border-amber-900 p-4">
          <strong className="text-amber-900 dark:text-amber-200">Read this carefully.</strong> Theta Algos is a software publisher, not a registered investment adviser. We do not custody your money. Trading involves risk of total loss.
        </section>

        <section>
          <h2 className="text-xl font-bold">1. What we are</h2>
          <p>Theta Algos LLC operates a software platform that lets you build algorithmic trading strategies, backtest them on historical data, simulate them in paper mode, and (optionally) connect your own brokerage account so the bot can place orders on your behalf according to rules you configure. We are a <strong>software publisher</strong>. We are not your investment adviser, broker, custodian, or fiduciary. We never hold your money. All orders are placed against accounts you own with FINRA/CFTC-registered third-party brokers.</p>
        </section>

        <section>
          <h2 className="text-xl font-bold">2. Eligibility</h2>
          <p>You must be (a) at least 18 years old, (b) legally able to enter contracts in your jurisdiction, (c) responsible for complying with all securities, tax, and prop-firm rules that apply to you. We do not sell to residents of countries embargoed by OFAC.</p>
        </section>

        <section>
          <h2 className="text-xl font-bold">3. Subscription, billing, refunds</h2>
          <ul className="list-disc list-inside space-y-1">
            <li>Tiers and pricing are listed at <Link to="/pricing" className="text-violet-600 underline">/pricing</Link>. Free trial does not require a card.</li>
            <li>Paid plans bill monthly via Stripe. Cancel anytime from your Profile — cancellation takes effect at the end of the current billing period.</li>
            <li><strong>Refund policy</strong>: full refund within 7 days of first paid charge for any reason, prorated thereafter only if we materially fail to deliver promised functionality.</li>
            <li>We may suspend service immediately for non-payment, violation of these terms, or suspected fraud.</li>
          </ul>
        </section>

        <section>
          <h2 className="text-xl font-bold">4. Your responsibilities</h2>
          <ul className="list-disc list-inside space-y-1">
            <li>Verify that the bot is doing what you expect — start in paper mode or sandbox before live.</li>
            <li>Configure risk limits (position sizing, daily loss caps) before connecting a live broker.</li>
            <li>Comply with your broker's terms — many prop firms ban algorithmic execution; that's your risk to manage.</li>
            <li>Keep your account credentials secure. We are not liable for losses caused by unauthorized use of your account.</li>
            <li>Pay your own taxes. We do not issue 1099s for trading P&L — your broker does.</li>
          </ul>
        </section>

        <section>
          <h2 className="text-xl font-bold">5. Not investment advice</h2>
          <p>Signals, scanner picks, backtest results, and any other output from the platform are <strong>generated mechanically from publicly available data</strong> and your own configuration. They are not personalized to your circumstances and are not investment advice. We are not a registered investment adviser, broker-dealer, or commodity trading adviser. Past performance and simulated performance do not predict future results. You alone decide whether to act on any signal.</p>
        </section>

        <section>
          <h2 className="text-xl font-bold">6. Risk of loss</h2>
          <p>Trading futures, options, and securities involves substantial risk of loss. You can lose more than you deposit, especially on margin. Theta Algos does not guarantee profits, and we explicitly disclaim any expectation that the strategies in our library will be profitable in any specific account.</p>
        </section>

        <section>
          <h2 className="text-xl font-bold">7. Limitation of liability</h2>
          <p>To the maximum extent permitted by law, Theta Algos LLC's aggregate liability to you for any claim arising from your use of the Service is limited to the fees you have paid us in the 12 months preceding the claim. We are not liable for (a) trading losses, (b) data outages or stale data, (c) broker failures or rejected orders, (d) errors in third-party data feeds, (e) consequential, incidental, or punitive damages.</p>
        </section>

        <section>
          <h2 className="text-xl font-bold">8. Acceptable use</h2>
          <p>You will not (a) reverse-engineer, decompile, or scrape the platform, (b) use the platform to manipulate markets or violate exchange rules, (c) resell our signals or screenshots without permission, (d) use the API at a rate that degrades service for others. Violations result in immediate termination without refund.</p>
        </section>

        <section>
          <h2 className="text-xl font-bold">9. Intellectual property</h2>
          <p>The platform code, strategy library defaults, scanner logic, and brand are owned by Theta Algos LLC. Strategies you build using the platform are owned by you. You grant us a license to host and execute your strategies on your behalf solely to provide the Service.</p>
        </section>

        <section>
          <h2 className="text-xl font-bold">10. Dispute resolution + governing law</h2>
          <p>These terms are governed by Florida law. Any dispute will be resolved by binding arbitration in Sarasota County, Florida, under JAMS rules, except that either party may seek injunctive relief in court for IP claims. <strong>You waive your right to jury trial and to participate in class actions.</strong></p>
        </section>

        <section>
          <h2 className="text-xl font-bold">11. Changes to these terms</h2>
          <p>We may update these terms with 14 days' notice by email. Continued use after the effective date constitutes acceptance.</p>
        </section>

        <section>
          <h2 className="text-xl font-bold">12. Contact</h2>
          <p>Theta Algos LLC · 19839 Bridgetown Lp, Venice FL 34293 · <a href="mailto:legal@thetaalgos.com" className="text-violet-600">legal@thetaalgos.com</a></p>
        </section>

      </div>
    </div>
  )
}
