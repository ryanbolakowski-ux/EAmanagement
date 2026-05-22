import { Link } from 'react-router-dom'

export default function Disclosures() {
  return (
    <div className="max-w-3xl mx-auto px-6 py-12 text-slate-800 dark:text-slate-200">
      <Link to="/" className="text-sm text-violet-600 dark:text-violet-400 hover:underline">← Theta Algos</Link>
      <h1 className="text-3xl font-extrabold mt-4 mb-2 text-slate-900 dark:text-white">Risk & Regulatory Disclosures</h1>
      <p className="text-sm text-slate-500 dark:text-slate-400 mb-8">Effective May 2026</p>

      <div className="prose dark:prose-invert prose-sm space-y-6">

        <section className="rounded-xl bg-rose-50 dark:bg-rose-900/20 border border-rose-200 dark:border-rose-900 p-4">
          <strong className="text-rose-900 dark:text-rose-200 block mb-2">Substantial risk of loss</strong>
          Trading futures, options, and securities involves substantial risk of loss and is not suitable for every investor. Past performance, simulated performance, and backtest results are not indicative of future results. You may lose more than your initial deposit. Do not trade with money you cannot afford to lose.
        </section>

        <section>
          <h2 className="text-xl font-bold">1. We are a software publisher, not an investment adviser</h2>
          <p>Theta Algos LLC is a software publisher. We are not a registered investment adviser (RIA), broker-dealer, futures commission merchant (FCM), or commodity trading adviser (CTA). Nothing on this site is investment advice. Signals are generated mechanically from public market data and your own configuration; they are not personalized to your circumstances, finances, or risk tolerance. Under <em>Lowe v. SEC</em>, 472 U.S. 181 (1985), we operate as a financial publisher.</p>
        </section>

        <section>
          <h2 className="text-xl font-bold">2. Backtest results disclaimer</h2>
          <p>Hypothetical or simulated performance results have inherent limitations. They are prepared with the benefit of hindsight. Simulations do not account for (a) actual slippage, (b) commissions beyond those modeled, (c) order rejections, (d) market impact, (e) liquidity constraints, (f) emotional factors, (g) capital constraints, (h) survivorship bias in the universe selected. <strong>No representation is made that any account will or is likely to achieve profits or losses similar to those shown.</strong></p>
        </section>

        <section>
          <h2 className="text-xl font-bold">3. Auto-execution disclaimer</h2>
          <p>When you connect a brokerage account and enable live trading, the bot places orders on your behalf based on rules you configure. You are responsible for monitoring the bot, your account, and your risk. We are not liable for orders placed in error due to (a) software bugs, (b) data feed failures, (c) broker API failures, (d) network outages, (e) third-party service failures (Stripe, Resend, Polygon, Databento, etc.).</p>
        </section>

        <section>
          <h2 className="text-xl font-bold">4. Prop-firm trading</h2>
          <p>Many prop firms (Apex, TPT, Topstep, Tradeify, FundedNext, etc.) prohibit algorithmic execution on their accounts. If you use Theta Algos signals to trade manually inside prop firm rules ("copy-trade by hand"), that is your responsibility — we do not police or guarantee prop-firm compliance. Violating prop-firm rules can result in immediate account termination, loss of profits, and forfeit of evaluation fees.</p>
        </section>

        <section>
          <h2 className="text-xl font-bold">5. Options trading</h2>
          <p>Options trading carries additional risks beyond directional securities: (a) time decay (theta), (b) implied volatility crush, (c) total loss of premium paid, (d) assignment risk on short positions, (e) pin risk at expiration. Before trading options live, read the <a href="https://www.theocc.com/company-information/documents-and-archives/options-disclosure-document" target="_blank" rel="noopener" className="text-violet-600 underline">Characteristics and Risks of Standardized Options (OCC ODD)</a>.</p>
        </section>

        <section>
          <h2 className="text-xl font-bold">6. Market data</h2>
          <p>Market data displayed on the platform is sourced from Polygon.io, Databento, and the brokers you connect. We make no warranty as to data accuracy, completeness, or timeliness. Decisions should be verified against your broker's quotes. Data may be delayed by 15+ minutes on free tiers.</p>
        </section>

        <section>
          <h2 className="text-xl font-bold">7. Testimonials + screenshots</h2>
          <p>Any P&L screenshots, testimonials, or case studies appearing on marketing pages are individual results that are not typical and do not guarantee that any other user will achieve similar results. Past performance does not predict future performance.</p>
        </section>

        <section>
          <h2 className="text-xl font-bold">8. Tax</h2>
          <p>We do not issue tax forms. Your broker (Tradier, Tradovate, Alpaca, etc.) issues 1099s for your trading activity. You are solely responsible for tax compliance.</p>
        </section>

        <section>
          <h2 className="text-xl font-bold">9. Conflicts of interest</h2>
          <p>The owner of Theta Algos LLC trades the same strategies offered on the platform in his own personal accounts. We do not front-run customer signals — signals are generated and emailed to all subscribers simultaneously, regardless of whether the owner has placed a corresponding trade.</p>
        </section>

        <section>
          <h2 className="text-xl font-bold">10. Acknowledgements</h2>
          <p>By creating an account, you are required to accept (i) our <Link to="/terms" className="text-violet-600 underline">Terms of Service</Link>, (ii) our <Link to="/privacy" className="text-violet-600 underline">Privacy Policy</Link>, and (iii) this Risk Disclosure. Before connecting a live brokerage, you must also accept the Live Trading Consent. Before deploying any options strategy live, you must also accept the Options Trading Consent. All acknowledgements are recorded with timestamp and version on the server.</p>
        </section>

        <section>
          <h2 className="text-xl font-bold">11. Contact</h2>
          <p>Theta Algos LLC · 19839 Bridgetown Lp, Venice FL 34293 · <a href="mailto:legal@thetaalgos.com" className="text-violet-600">legal@thetaalgos.com</a></p>
        </section>
      </div>
    </div>
  )
}
