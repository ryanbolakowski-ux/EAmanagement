import { useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { Search, X, ChevronDown, Menu, ArrowUp, Mail } from 'lucide-react'

/**
 * Help & FAQ page.
 *
 * Comprehensive, searchable knowledge base.  Written to be the first place a
 * confused user lands when the AI chat is off (which it is by default).
 *
 * Layout:
 *   - Hero (title, intro, search input)
 *   - 2-column grid on desktop: sticky sidebar (categories) + main content
 *   - Mobile: search bar, chip rail of categories, full-width accordions
 *
 * Each FAQ item is a controlled accordion (not <details>) so the search-result
 * "auto-open matched items" behavior works smoothly.
 */

type Faq = {
  category: string
  question: string
  answer: React.ReactNode
}

const CATEGORIES = [
  'Getting Started',
  'Accounts & KYC',
  'Broker Connections',
  'Paper Trading',
  'Live Trading',
  'Risk & Position Sizing',
  'Saro',
  'Strategies',
  'Backtesting & Optimization',
  'Emails & Alerts',
  'Market Data & Pricing',
  'Trading Hours',
  'Billing & Subscription',
  'Security & Privacy',
  'Legal & Risk',
  'Troubleshooting',
  'Support',
] as const

// Helper: render an external/internal link with consistent styling.
const A = ({ href, children, external = false }: { href: string; children: React.ReactNode; external?: boolean }) =>
  external ? (
    <a href={href} target="_blank" rel="noopener noreferrer" className="text-violet-600 dark:text-violet-400 underline hover:no-underline">{children}</a>
  ) : (
    <Link to={href} className="text-violet-600 dark:text-violet-400 underline hover:no-underline">{children}</Link>
  )

const FAQS: Faq[] = [
  // ── Getting Started ─────────────────────────────────────────────────────
  {
    category: 'Getting Started',
    question: 'What is Theta Algos?',
    answer: (
      <p>Theta Algos is an algorithmic-trading software platform for active US-based retail and prop traders. We give you a strategy builder, a historical backtester, a paper-trading simulator, a daily premarket scanner, and a one-click live-trading bridge to your brokerage. You build the rules; the platform watches the market, fires entries, manages stops, and emails or auto-executes signals.</p>
    ),
  },
  {
    category: 'Getting Started',
    question: 'Who is Theta Algos for?',
    answer: (
      <p>Active traders who want quantitative consistency without writing code. We focus on US stocks &amp; ETFs, options chains, and CME futures (ES / NQ / RTY / YM and their micros). If you trade discretionarily today but want rule-based execution, paper-first validation, and a daily premarket pick, this is for you.</p>
    ),
  },
  {
    category: 'Getting Started',
    question: 'Is Theta Algos available outside the USA?',
    answer: (
      <p><strong>Not yet — we are US-only at launch.</strong> Registration is restricted to US residents because our identity verification (Stripe Identity), broker integrations, and regulatory posture are all US-focused. Other markets are on the roadmap but we have no timeline yet.</p>
    ),
  },
  {
    category: 'Getting Started',
    question: 'Why USA only?',
    answer: (
      <p>Three reasons: (1) compliance — our KYC partner Stripe Identity is US-tuned and reads US driver&apos;s licenses / passports natively; (2) broker integrations — Tradier (and the brokers on the roadmap like Schwab, Alpaca, TastyTrade, Tradovate, Webull, Interactive Brokers) are all US-domiciled; (3) regulatory landscape — SEC and CFTC rules drive disclosures, prop-firm policy, and tax reporting that we want to get exactly right before expanding. See our <A href="/disclosures">Disclosures</A> page for the full regulatory footing.</p>
    ),
  },
  {
    category: 'Getting Started',
    question: 'How do I sign up?',
    answer: (
      <p>Go to <A href="/register">/register</A>, create an account with an email and password, then complete identity verification on the KYC page. After KYC is verified you can connect a broker on the Profile page and start paper trading. Live trading unlocks once KYC is &quot;verified&quot; and a broker account is linked.</p>
    ),
  },
  {
    category: 'Getting Started',
    question: 'What does a typical workflow look like?',
    answer: (
      <ol className="list-decimal pl-5 space-y-1">
        <li>Sign up and complete KYC.</li>
        <li>Connect a broker (start with the free Tradier sandbox).</li>
        <li>Build or import a strategy on the Strategies page (or use the Plain-English builder).</li>
        <li>Backtest the strategy across 1–2 years of history; optimize parameters if needed.</li>
        <li>Paper trade the strategy on live market data for a few sessions.</li>
        <li>Deploy live with a small allocation — the bot manages entries, stops, and EOD close automatically.</li>
      </ol>
    ),
  },

  // ── Accounts & KYC ──────────────────────────────────────────────────────
  {
    category: 'Accounts & KYC',
    question: 'What is KYC and why do you require it?',
    answer: (
      <p>KYC (Know Your Customer) is government-mandated identity verification for financial-software platforms. We use <strong>Stripe Identity</strong> to verify a real human is behind each account. It protects the platform from fraud and bots, and lets us comply with the regulatory baseline expected of any service that touches brokerage accounts in the US.</p>
    ),
  },
  {
    category: 'Accounts & KYC',
    question: 'What documents do I need for KYC?',
    answer: (
      <p>A government-issued photo ID — a US driver&apos;s license, state ID, or passport — plus a quick selfie that Stripe&apos;s system matches against the ID photo. The whole flow takes under two minutes on your phone.</p>
    ),
  },
  {
    category: 'Accounts & KYC',
    question: 'What happens if KYC is pending?',
    answer: (
      <p>Stripe typically returns a decision within a few minutes, but it can take a few hours during peak load. The dashboard polls Stripe in the background and auto-updates your status — you don&apos;t have to refresh. If pending for more than a day, email <A href="mailto:support@thetaalgos.com" external>support@thetaalgos.com</A>.</p>
    ),
  },
  {
    category: 'Accounts & KYC',
    question: 'What if KYC needs more information?',
    answer: (
      <p>Stripe will flip your status to <code>requires action</code>. The KYC page shows a <strong>Continue verification</strong> button — click it to be sent back through Stripe&apos;s flow with the missing piece flagged (usually a clearer document photo).</p>
    ),
  },
  {
    category: 'Accounts & KYC',
    question: 'What if KYC fails or is rejected?',
    answer: (
      <p>Email <A href="mailto:support@thetaalgos.com" external>support@thetaalgos.com</A> with the date you submitted and we will manually review. Most rejections come from a blurry document photo and clear up on a re-submit.</p>
    ),
  },
  {
    category: 'Accounts & KYC',
    question: 'Can I trade without completing KYC?',
    answer: (
      <p>No. Both live and paper trading require <code>kyc_status = verified</code>. We gate this server-side so all execution endpoints (live orders, paper sessions, broker linking) check verification before doing anything. This is a regulatory requirement, not a UX one.</p>
    ),
  },
  {
    category: 'Accounts & KYC',
    question: 'How do I reset my login password?',
    answer: (
      <p>Go to <A href="/forgot-password">/forgot-password</A>, enter your email, and we&apos;ll send a reset link. The link is valid for 60 minutes. If you don&apos;t see the email, check spam — it&apos;s sent through Resend from <code>no-reply@thetaalgos.com</code>.</p>
    ),
  },
  {
    category: 'Accounts & KYC',
    question: 'How do I reset my admin unlock passcode?',
    answer: (
      <p>Email <A href="mailto:support@thetaalgos.com" external>support@thetaalgos.com</A> from the email on your account. Admin passcodes are reset manually by an operator — we don&apos;t expose a self-serve flow for security reasons.</p>
    ),
  },

  // ── Broker Connections ──────────────────────────────────────────────────
  {
    category: 'Broker Connections',
    question: 'Which brokers does Theta Algos support?',
    answer: (
      <p>Today: <strong>Tradier</strong> (sandbox + live), for both stocks and options. On the roadmap: Alpaca, Charles Schwab, Tradovate (futures), TastyTrade, Webull, and Interactive Brokers. For prop-firm accounts (Apex, TPT, Topstep, etc.) you use Email Signals and place orders manually — see the <A href="/app/prop-firms">Prop Firms</A> page for compliance notes.</p>
    ),
  },
  {
    category: 'Broker Connections',
    question: 'How do I connect Tradier?',
    answer: (
      <p>From <A href="/app/profile">Profile</A> → Broker Accounts → <strong>Connect Tradier</strong>. You&apos;ll be sent through Tradier&apos;s OAuth flow and bounced back here with the account linked. We store only the OAuth refresh token (encrypted at rest with Fernet) — never your Tradier login.</p>
    ),
  },
  {
    category: 'Broker Connections',
    question: 'Sandbox vs live — what is the difference?',
    answer: (
      <p>Tradier sandbox accounts trade with fake money against a delayed simulated order book. They are perfect for proving a strategy works end-to-end before risking real capital. Live accounts trade real money on real markets. We label sandbox accounts clearly in the UI (orange pill) so you never confuse the two.</p>
    ),
  },
  {
    category: 'Broker Connections',
    question: 'How do I refresh my broker balance?',
    answer: (
      <p>On the Live Trading page, each broker account card has a <strong>Refresh balance</strong> button. Click it to pull the current balance, cash, and buying power from Tradier. The result is cached for performance until you click again.</p>
    ),
  },
  {
    category: 'Broker Connections',
    question: 'Why is my balance stale?',
    answer: (
      <p>Balances are cached aggressively because hitting the broker on every render would burn rate-limit budget. If the cached balance is older than 5 minutes, the Live Trading page blocks new orders until you refresh — that&apos;s a guardrail to make sure you don&apos;t size a position against a stale equity number.</p>
    ),
  },
  {
    category: 'Broker Connections',
    question: 'Why does my balance not match my brokerage app exactly?',
    answer: (
      <p>Tradier&apos;s sandbox has known quirks — its account-history endpoint omits some activity, so reconciliation can drift from what the sandbox UI shows. Real Tradier live accounts reconcile perfectly. If you see a discrepancy in a live account that persists after a refresh, email support with the account ID and the figures.</p>
    ),
  },

  // ── Paper Trading ───────────────────────────────────────────────────────
  {
    category: 'Paper Trading',
    question: 'What is paper trading?',
    answer: (
      <p>Paper trading runs your strategy against <strong>real, live market data</strong> but executes orders against a simulated broker — no real money changes hands. Use it to validate that a strategy actually fires in live conditions (and behaves the way the backtest predicted) before deploying it live.</p>
    ),
  },
  {
    category: 'Paper Trading',
    question: 'How do I start a paper trading session?',
    answer: (
      <p>From <A href="/app/paper">Paper Trading</A>, click <strong>Start Session</strong>, pick a strategy, pick an asset, and click Run. The session streams live prices, evaluates your rules every bar, and shows fills + P&amp;L in real time.</p>
    ),
  },
  {
    category: 'Paper Trading',
    question: 'Which strategies can I use for paper trading?',
    answer: (
      <p>Any strategy in <code>active</code>, <code>draft</code>, or <code>paused</code> state can be paper-traded. Paper is the safe playground — we intentionally do not gate it by &quot;activated&quot; status so you can iterate freely on drafts.</p>
    ),
  },
  {
    category: 'Paper Trading',
    question: 'Can I paper trade options?',
    answer: (
      <p>Yes. Options paper trading uses your stock-underlying strategy to pick direction, then prices the corresponding option contract off the live underlying. You&apos;ll see the option premium fill, Greeks at entry, and P&amp;L as the underlying moves.</p>
    ),
  },
  {
    category: 'Paper Trading',
    question: 'Can I paper trade futures?',
    answer: (
      <p>Yes. Futures paper supports ES, NQ, RTY, YM and their micros (MES, MNQ, M2K, MYM). Tick values and contract specs are baked in so the P&amp;L matches what you&apos;d see on a real fill.</p>
    ),
  },

  // ── Live Trading ────────────────────────────────────────────────────────
  {
    category: 'Live Trading',
    question: 'What is live trading?',
    answer: (
      <p>Live trading is real-money execution. The bot watches market data, fires entries when your rules match, places bracket orders (entry + stop + target) through your connected broker, and manages the trade from there. All orders show up immediately in your broker&apos;s UI just like manual orders.</p>
    ),
  },
  {
    category: 'Live Trading',
    question: 'How do I deploy a strategy live?',
    answer: (
      <p>From <A href="/app/live">Live Trading</A>, click <strong>Deploy Strategy</strong>, pick the strategy, pick the broker account, pick the instrument, and confirm. The deployment goes active immediately — the bot is now watching and will fire on the next setup.</p>
    ),
  },
  {
    category: 'Live Trading',
    question: 'Which strategies are eligible for live deployment?',
    answer: (
      <p>Only strategies with <code>status = active</code> AND an asset class compatible with the chosen broker account. A futures strategy needs a futures-capable broker; an options strategy needs an options-approved broker. The deploy modal filters the list for you automatically.</p>
    ),
  },
  {
    category: 'Live Trading',
    question: 'How do I set position size for a trade?',
    answer: (
      <p>On the Live Trading page, use the <strong>Sizing Preview</strong> card: enter a ticker, entry price, stop price, and a dollar allocation. The bot calculates share count, dollar risk, and position cost, and validates that you have enough cash / buying power before letting you submit.</p>
    ),
  },
  {
    category: 'Live Trading',
    question: 'What is the difference between % risk and $ allocation?',
    answer: (
      <p><strong>% risk</strong> sizes the trade so a stop-out loses exactly N% of your account equity (e.g., 1% per trade). <strong>$ allocation</strong> sizes so the position itself costs exactly N dollars (e.g., $2,000 of shares), regardless of where the stop is. Both modes are supported — pick the one that matches how you think about risk.</p>
    ),
  },
  {
    category: 'Live Trading',
    question: 'Why can I not exceed my account balance?',
    answer: (
      <p>Cash accounts are limited to available cash; margin accounts are limited to buying power. The Live Trading server-side validator rejects any order that would exceed the limit, so you can&apos;t accidentally over-commit. If you have margin enabled at Tradier, your buying power is roughly 2× equity for stocks and 4× for day-trades — the validator uses whichever number Tradier returns.</p>
    ),
  },
  {
    category: 'Live Trading',
    question: 'How does the app handle stop losses and take profits?',
    answer: (
      <p>Stops are managed by an in-platform trail watcher that runs every minute during market hours. We support a <strong>trailing stop with a hard-stop floor</strong>: the stop ratchets up as the trade moves in your favor, but never below the floor you set. Take-profits are placed as broker-side limit orders so they fill even if the watcher misses a tick.</p>
    ),
  },
  {
    category: 'Live Trading',
    question: 'What is the end-of-day close?',
    answer: (
      <p>At <strong>3:55 PM ET</strong> the platform automatically closes any open Saro positions with a market sell. This is to prevent overnight risk on a strategy designed for intraday only. You can disable EOD close per-strategy in settings if you want to hold overnight.</p>
    ),
  },

  // ── Risk & Position Sizing ──────────────────────────────────────────────
  {
    category: 'Risk & Position Sizing',
    question: 'What sizing modes are supported?',
    answer: (
      <p>Three modes: <strong>% of equity</strong> (risk N% of equity per trade), <strong>fixed $ per trade</strong> (risk exactly $X dollars), and <strong>fixed $ allocation</strong> (put exactly $X into the position regardless of stop distance). Each strategy and each Saro pick can be configured independently.</p>
    ),
  },
  {
    category: 'Risk & Position Sizing',
    question: 'What is the default risk per trade?',
    answer: (
      <p>1% of equity per trade unless you override it. This is the classic Van Tharp baseline — survivable through a long losing streak and aggressive enough to compound when the strategy is working.</p>
    ),
  },
  {
    category: 'Risk & Position Sizing',
    question: 'What is the max position cap?',
    answer: (
      <p>Configurable per broker account. The default cap is the lesser of (a) available cash / buying power, and (b) any custom dollar cap you set on the Profile page. Saro has its own allocation field so you can give it a dedicated bucket separate from your manual deployments.</p>
    ),
  },
  {
    category: 'Risk & Position Sizing',
    question: 'Cash vs margin — how does the app treat them?',
    answer: (
      <p><strong>Cash accounts:</strong> limited to settled cash. The bot will never short or place orders that exceed cash. <strong>Margin accounts:</strong> limited to broker-reported buying power (which already accounts for margin maintenance). The validator always uses the value Tradier returns, not a calculated estimate.</p>
    ),
  },

  // ── Saro ───────────────────────────────────────────────────────
  {
    category: 'Saro',
    question: 'What is Saro?',
    answer: (
      <p>Saro — the daily stock scanner — is our premarket stock-pick algorithm. Every US trading day, starting at 4 AM ET, it scores the universe of premarket gappers by gap %, volume, relative volume, and catalyst quality, then picks the highest-scoring setup to email and (optionally) auto-trade.</p>
    ),
  },
  {
    category: 'Saro',
    question: 'What does it look for?',
    answer: (
      <p>Stocks gapping <strong>between 5% and 25%</strong> on the open vs. yesterday&apos;s close, with high relative volume (real interest, not just a thin print), and a news / PR catalyst on the wire (earnings, FDA, M&amp;A, contract). All three boxes have to check.</p>
    ),
  },
  {
    category: 'Saro',
    question: 'How is the score calculated?',
    answer: (
      <p>The exact formula is <code>gap × log(volume) × catalyst_weight × min(rel_vol, 10) / 100</code>. The <code>log(volume)</code> term keeps mega-cap volume from dominating; the <code>min(rel_vol, 10)</code> cap prevents one spike from skewing the ranking. Higher score = better setup.</p>
    ),
  },
  {
    category: 'Saro',
    question: 'When does it run?',
    answer: (
      <p>Premarket, from <strong>4 AM ET onward</strong> on US trading days. The first qualifying setup of the day fires — the scanner doesn&apos;t wait for the bell. Earlier fires require a higher score threshold (a 4 AM pick needs to be more decisive than a 9:25 AM pick).</p>
    ),
  },
  {
    category: 'Saro',
    question: 'Why was a specific stock selected?',
    answer: (
      <p>The Live Trading page shows a <strong>criteria card</strong> next to each scanner pick that breaks down the inputs — gap %, volume, rel vol, catalyst headline, and the final score. If the pick surprises you, the card tells you why it scored highest.</p>
    ),
  },
  {
    category: 'Saro',
    question: 'Is the Saro pick a recommendation?',
    answer: (
      <p><strong>No.</strong> The pick is an algorithmic ranking of premarket setups, not investment advice. You decide whether to take the trade. See our <A href="/disclosures">Disclosures</A> page for the full statement.</p>
    ),
  },
  {
    category: 'Saro',
    question: 'Does Saro work for options?',
    answer: (
      <p>Currently stock-only. We do have an options paper engine that can replay scanner picks against the corresponding ATM option, and a dedicated options scanner is in development — but the stock pick is the production output today.</p>
    ),
  },

  // ── Strategies ──────────────────────────────────────────────────────────
  {
    category: 'Strategies',
    question: 'How do I create a strategy?',
    answer: (
      <p>From <A href="/app/strategies">Strategies</A>, click <strong>New Strategy</strong>. You can either build rules in the visual builder, paste a description into the Plain-English builder, or import a JSON config. Save as a draft, backtest, iterate, then mark it active when you&apos;re happy.</p>
    ),
  },
  {
    category: 'Strategies',
    question: 'What is the Plain English builder?',
    answer: (
      <p>Describe your strategy in natural language — &quot;buy when price breaks above the previous day&apos;s high with RSI&gt;60, stop below the breakout candle&apos;s low, target 2R&quot; — and the AI translates it into structured rules you can review, edit, and save. Use it as a starting point; you almost always want to refine the output before deploying.</p>
    ),
  },
  {
    category: 'Strategies',
    question: 'Can I backtest my strategy?',
    answer: (
      <p>Yes. From the Strategies page, click <strong>Backtest</strong> on any strategy. The engine replays 1–2 years of historical bars, applies your rules, and reports win rate, profit factor, max drawdown, expectancy, total P&amp;L, and a trade-by-trade log.</p>
    ),
  },
  {
    category: 'Strategies',
    question: 'Can I optimize my strategy?',
    answer: (
      <p>Yes. The Optimize button runs a parameter grid (multiple stop sizes, target multiples, etc.) in parallel and ranks the results by profit factor. Heads up: optimization is prone to overfitting — always sanity-check the winner on a held-out window.</p>
    ),
  },
  {
    category: 'Strategies',
    question: 'What instruments can I trade?',
    answer: (
      <p>US stocks &amp; ETFs (~500 liquid names + the full S&amp;P 500), CME futures (ES, NQ, YM, RTY plus micros), and options chains on supported underlyings. Asset class is set per-strategy and the deploy modal filters compatible broker accounts.</p>
    ),
  },
  {
    category: 'Strategies',
    question: 'What is the difference between stock, options, and futures strategies?',
    answer: (
      <p>Asset class drives execution and risk modeling. Stock strategies size in shares and risk in % of equity. Options strategies pick a contract (delta, DTE) and size in contracts. Futures strategies size in contracts using tick value (ES = $50/pt, NQ = $20/pt, MES = $5/pt, MNQ = $2/pt). The deploy modal only shows broker accounts that support the strategy&apos;s asset class.</p>
    ),
  },

  // ── Backtesting & Optimization ──────────────────────────────────────────
  {
    category: 'Backtesting & Optimization',
    question: 'How does the backtester work?',
    answer: (
      <p>The backtester walks historical OHLCV bars in chronological order, applies your strategy&apos;s entry rules on every bar, and simulates fills at the close (or the next-bar open for next-bar entries). Stops and targets fire intra-bar based on high/low. It reports win rate, profit factor, expectancy, max drawdown, total P&amp;L, and a full trade log.</p>
    ),
  },
  {
    category: 'Backtesting & Optimization',
    question: 'What data does it use?',
    answer: (
      <p>Polygon REST for stocks and options, TwelveData for futures, and yfinance as a fallback for thin-data symbols. All data is timezone-normalized to ET and adjusted for splits and dividends where applicable.</p>
    ),
  },
  {
    category: 'Backtesting & Optimization',
    question: 'Why is my backtest different from live?',
    answer: (
      <p>Real fills include slippage, partial fills, and live bid/ask spreads — backtests assume clean fills at the bar price. We model conservative slippage by default but it&apos;s a model, not reality. Paper trading on live data is the right next step between backtest and live, since it surfaces real fill behavior.</p>
    ),
  },
  {
    category: 'Backtesting & Optimization',
    question: 'What metrics does optimization output?',
    answer: (
      <p>For each parameter combination: win rate, profit factor, max drawdown, total P&amp;L, expectancy per trade, and trade count. The results table is sortable and exportable. Best practice: use profit factor to rank, but cross-check max drawdown — high PF with massive drawdown is usually overfit.</p>
    ),
  },

  // ── Emails & Alerts ─────────────────────────────────────────────────────
  {
    category: 'Emails & Alerts',
    question: 'What is the daily Saro email?',
    answer: (
      <p>The Saro email goes out around <strong>9:25 AM ET</strong> on US trading days with that day&apos;s top premarket pick — ticker, gap %, entry, stop, target, score, and the catalyst headline. Use it as a heads-up even if you don&apos;t auto-trade.</p>
    ),
  },
  {
    category: 'Emails & Alerts',
    question: 'What are futures emails?',
    answer: (
      <p>Real-time alerts for futures setups (ICT-style FVG, liquidity sweep, opening-range break) on ES / NQ / RTY / YM. Sent the moment the setup fires during the relevant kill zone, with entry, stop, target, and rationale.</p>
    ),
  },
  {
    category: 'Emails & Alerts',
    question: 'What are options swing emails?',
    answer: (
      <p>Multi-day options setups — typically 30–60 DTE long calls or puts on stocks showing strong directional bias. Sent end-of-day with entry rationale, suggested strike, and a target / stop.</p>
    ),
  },
  {
    category: 'Emails & Alerts',
    question: 'What is the Email Signals page?',
    answer: (
      <p><A href="/app/email-signals">Email Signals</A> is the live feed of every signal we&apos;ve sent you, plus the outcome (filled, hit target, hit stop, expired). Each row shows the original setup and a colored outcome dot so you can scan history at a glance.</p>
    ),
  },
  {
    category: 'Emails & Alerts',
    question: 'What do the outcome dots mean?',
    answer: (
      <ul className="list-disc pl-5 space-y-1">
        <li><strong>Green</strong> — win (hit take profit)</li>
        <li><strong>Red</strong> — loss (hit stop)</li>
        <li><strong>White</strong> — break-even close</li>
        <li><strong>Yellow</strong> — expired or timed out</li>
        <li><strong>Black</strong> — still pending (open)</li>
      </ul>
    ),
  },
  {
    category: 'Emails & Alerts',
    question: 'Why are some emails marked suppressed?',
    answer: (
      <p>Suppressed rows are signals that were deduplicated, retry-blocked, or admin-only debug entries. They&apos;re hidden from the default Email Signals view because they aren&apos;t user-actionable. Admins can toggle them on.</p>
    ),
  },
  {
    category: 'Emails & Alerts',
    question: 'I am not getting emails — what should I check?',
    answer: (
      <p>(1) Check spam — especially Yahoo, which aggressively bulk-folders new senders. Add <code>no-reply@thetaalgos.com</code> to contacts. (2) Confirm your account is active. (3) Confirm the relevant signal type is enabled on your subscription tier. If all three check out, email <A href="mailto:support@thetaalgos.com" external>support</A> and we&apos;ll trace the delivery log.</p>
    ),
  },

  // ── Market Data & Pricing ───────────────────────────────────────────────
  {
    category: 'Market Data & Pricing',
    question: 'Where does the price data come from?',
    answer: (
      <p>Polygon REST + streaming for stocks and options, TwelveData and yfinance for futures and fallback. The scanner pulls quotes directly; backtests pull historical aggregates. Data routing is automatic — you don&apos;t need to configure anything.</p>
    ),
  },
  {
    category: 'Market Data & Pricing',
    question: 'Why are some prices delayed?',
    answer: (
      <p>Free / sandbox data feeds carry a 15-minute delay by default. Paid tiers include real-time feeds. If you&apos;re paper trading on a delayed feed you&apos;ll see entries that look stale relative to your broker&apos;s real-time chart — that&apos;s the source, not a bug.</p>
    ),
  },
  {
    category: 'Market Data & Pricing',
    question: 'Why do futures show QQQ prices sometimes?',
    answer: (
      <p>When futures data is briefly stale (TwelveData rate-limit, yfinance hiccup) we fall back to an ETF-proxy: QQQ × scale factor for NQ, SPY × scale factor for ES. This keeps the engine running rather than stopping a live trade on a stale tick. It&apos;s fixed in production and should be a rare occurrence.</p>
    ),
  },
  {
    category: 'Market Data & Pricing',
    question: 'What are slippage and fills?',
    answer: (
      <p><strong>Slippage</strong> = the difference between the price your strategy signaled and the price you actually filled at — caused by price movement between signal generation and order arrival. <strong>Fills</strong> = the actual execution from the broker (price, qty, timestamp). Both are tracked per-trade.</p>
    ),
  },

  // ── Trading Hours ───────────────────────────────────────────────────────
  {
    category: 'Trading Hours',
    question: 'What hours does the scanner run?',
    answer: (
      <p>Premarket <strong>4 AM ET</strong> through the open at <strong>9:30 AM ET</strong>, then regular trading hours through <strong>4 PM ET</strong>. Saro picks fire premarket; intraday strategies run during RTH.</p>
    ),
  },
  {
    category: 'Trading Hours',
    question: 'What about overnight?',
    answer: (
      <p>Saro positions auto-close at <strong>3:55 PM ET</strong> via the EOD close routine to prevent overnight risk on intraday setups. Strategies marked as swing or position-trade hold through the close normally.</p>
    ),
  },
  {
    category: 'Trading Hours',
    question: 'Time zones — what is the app using?',
    answer: (
      <p>Eastern Time (ET) throughout for all trading logic — premarket windows, RTH boundaries, EOD close, signal timestamps. Your UI displays times in ET regardless of your local timezone so there&apos;s never ambiguity about when a fill happened.</p>
    ),
  },

  // ── Billing & Subscription ──────────────────────────────────────────────
  {
    category: 'Billing & Subscription',
    question: 'Is Theta Algos free?',
    answer: (
      <p>There&apos;s a free tier with limited features (paper trading, basic backtests, a daily scanner preview). Paid tiers unlock live trading, more strategies, and full signal access. See <A href="/pricing">/pricing</A> for the current breakdown.</p>
    ),
  },
  {
    category: 'Billing & Subscription',
    question: 'What does each tier include?',
    answer: (
      <p>Tier 1 (Free) — paper, basic backtests. Tier 2 (Futures Signals, $49/mo) — ICT futures signals via email. Tier 3 (Options Scanner, $99/mo) — daily Saro picks. Tier 4 (Options Live, $199/mo) — broker integration + one-click execution. Tier 5 (Fully Automated, $399/mo) — full auto-trade across all strategies. Full breakdown on the <A href="/pricing">Pricing page</A>.</p>
    ),
  },
  {
    category: 'Billing & Subscription',
    question: 'How do I cancel?',
    answer: (
      <p>From <A href="/app/profile">Profile</A> → Subscription → <strong>Cancel</strong>. Your subscription stays active through the end of the current billing period, then lapses. No retention dark patterns.</p>
    ),
  },
  {
    category: 'Billing & Subscription',
    question: 'Refunds?',
    answer: (
      <p>Refunds are handled case-by-case for billing issues — duplicate charges, post-cancellation renewals, etc. Email <A href="mailto:support@thetaalgos.com" external>support@thetaalgos.com</A> with the charge details. See our <A href="/terms">Terms of Service</A> for the full policy.</p>
    ),
  },

  // ── Security & Privacy ──────────────────────────────────────────────────
  {
    category: 'Security & Privacy',
    question: 'How do you handle my data?',
    answer: (
      <p>See our <A href="/privacy">Privacy Policy</A> for the full rundown. Short version: we collect what we need to operate the service (email, KYC verification result, broker connection metadata, trade history), encrypt sensitive fields at rest, never sell user data, and let you export or delete it on request.</p>
    ),
  },
  {
    category: 'Security & Privacy',
    question: 'Do you store my broker password?',
    answer: (
      <p><strong>No.</strong> Broker connections use OAuth — we never see your broker login. We store only the OAuth refresh token, encrypted at rest with Fernet (AES-128). The token only authorizes the scopes you grant during the OAuth flow.</p>
    ),
  },
  {
    category: 'Security & Privacy',
    question: 'Can I export my data?',
    answer: (
      <p>Yes. From <A href="/app/profile">Profile</A> → Export, you can download your strategies, trade history, and signal log as JSON. Full account deletion (right-to-be-forgotten) is also supported on request.</p>
    ),
  },

  // ── Legal & Risk ────────────────────────────────────────────────────────
  {
    category: 'Legal & Risk',
    question: 'Is Theta Algos investment advice?',
    answer: (
      <p><strong>No.</strong> Theta Algos LLC is a software publisher — not a registered investment adviser, broker-dealer, futures commission merchant, or commodity trading adviser. Nothing on the platform is a recommendation to buy or sell any security. See the full <A href="/disclosures">Disclosures</A> page.</p>
    ),
  },
  {
    category: 'Legal & Risk',
    question: 'Are there guaranteed returns?',
    answer: (
      <p><strong>No.</strong> Past performance does not predict future results. All trading involves substantial risk of loss. Backtest, paper, and live results shown on the platform are individual results that are not typical and do not guarantee any other user&apos;s experience.</p>
    ),
  },
  {
    category: 'Legal & Risk',
    question: 'What are the risks of stocks / options / futures?',
    answer: (
      <p>Standard disclosures apply: with margin or short positions you can lose more than your initial deposit. Options can expire worthless — you can lose 100% of premium paid. Futures carry leveraged exposure and can move sharply against you. Read the full <A href="/disclosures">Risk &amp; Regulatory Disclosures</A> before trading live.</p>
    ),
  },
  {
    category: 'Legal & Risk',
    question: 'Are prop-firm accounts supported?',
    answer: (
      <p>Yes, with caveats. <strong>Most prop firms (Apex, TPT, Topstep, Tradeify, FundedNext) prohibit algorithmic execution</strong> on their accounts. Theta Algos keeps you compliant by routing setups to Email Signals — you receive the alert and place the trade manually inside the prop firm&apos;s rules. See the <A href="/app/prop-firms">Prop Firms</A> page for per-firm compliance notes.</p>
    ),
  },

  // ── Troubleshooting ─────────────────────────────────────────────────────
  {
    category: 'Troubleshooting',
    question: 'The chat bubble is not showing — why?',
    answer: (
      <p>The AI chat assistant is <strong>temporarily disabled while we tune things</strong>. Use this FAQ for product questions, and email <A href="mailto:support@thetaalgos.com" external>support@thetaalgos.com</A> for anything not covered here. We&apos;ll re-enable the chat once we&apos;ve cost-tuned the model.</p>
    ),
  },
  {
    category: 'Troubleshooting',
    question: 'I see "balance is stale" — what do I do?',
    answer: (
      <p>Click <strong>Refresh balance</strong> on the Live Trading page. This pulls the current balance, cash, and buying power straight from Tradier and unblocks order placement.</p>
    ),
  },
  {
    category: 'Troubleshooting',
    question: 'My KYC is stuck — what should I do?',
    answer: (
      <p>Check your email for a Stripe verification link — that&apos;s usually what&apos;s pending. If your status has been &quot;pending&quot; or &quot;requires action&quot; for more than 24 hours, email <A href="mailto:support@thetaalgos.com" external>support</A> with your account email and we&apos;ll trace the Stripe session.</p>
    ),
  },
  {
    category: 'Troubleshooting',
    question: 'My trade did not close — what happened?',
    answer: (
      <p>Most often: the broker rejected the close order (rare), or the trail watcher hit a transient network error. Admins can pull the trail-watcher logs from Settings → Debug. For non-admins, email <A href="mailto:support@thetaalgos.com" external>support</A> with the trade ID and we&apos;ll investigate.</p>
    ),
  },

  // ── Support ─────────────────────────────────────────────────────────────
  {
    category: 'Support',
    question: 'How do I contact support?',
    answer: (
      <p>Email <A href="mailto:support@thetaalgos.com" external>support@thetaalgos.com</A>. Typical response time is within one business day; faster during US business hours.</p>
    ),
  },
  {
    category: 'Support',
    question: 'Where can I report a bug?',
    answer: (
      <p>Use the in-app feedback form (the lightbulb icon in the corner of every page) or email <A href="mailto:support@thetaalgos.com" external>support@thetaalgos.com</A> with reproduction steps. The feedback form auto-includes your user ID and the current URL so we can jump straight to the issue.</p>
    ),
  },
  {
    category: 'Support',
    question: 'Where are status updates?',
    answer: (
      <p>Check the dashboard banner — outages, planned maintenance, and major feature drops are posted there. We also post critical incident updates from <A href="mailto:support@thetaalgos.com" external>support@thetaalgos.com</A> via email when an outage affects live trading.</p>
    ),
  },
]


// ── Highlight matched substring inside a string ─────────────────────────────
function highlight(text: string, query: string): React.ReactNode {
  if (!query) return text
  const q = query.toLowerCase()
  const lower = text.toLowerCase()
  const out: React.ReactNode[] = []
  let i = 0
  let last = 0
  while ((i = lower.indexOf(q, last)) !== -1) {
    if (i > last) out.push(text.slice(last, i))
    out.push(
      <mark key={i} className="bg-amber-200 dark:bg-amber-700/60 dark:text-amber-50 rounded px-0.5">
        {text.slice(i, i + q.length)}
      </mark>
    )
    last = i + q.length
  }
  if (last < text.length) out.push(text.slice(last))
  return out
}


function FaqItem({ faq, query, open, onToggle }: {
  faq: Faq
  query: string
  open: boolean
  onToggle: () => void
}) {
  return (
    <div className="bg-white dark:bg-slate-900 border border-slate-200 dark:border-slate-800 rounded-2xl overflow-hidden">
      <button
        type="button"
        onClick={onToggle}
        aria-expanded={open}
        className="w-full text-left px-5 py-4 flex items-center justify-between gap-4 hover:bg-slate-50 dark:hover:bg-slate-800/50 transition-colors"
      >
        <span className="text-base font-semibold text-slate-900 dark:text-slate-100">
          {highlight(faq.question, query)}
        </span>
        <ChevronDown
          size={18}
          className={`flex-shrink-0 text-slate-500 dark:text-slate-400 transition-transform ${open ? 'rotate-180' : ''}`}
        />
      </button>
      {open && (
        <div className="px-5 pb-5 pt-1 text-sm leading-relaxed text-slate-700 dark:text-slate-200 border-t border-slate-100 dark:border-slate-800 space-y-3">
          {faq.answer}
        </div>
      )}
    </div>
  )
}


export default function Help() {
  const [query, setQuery] = useState('')
  const [openIds, setOpenIds] = useState<Set<string>>(new Set())
  const [mobileSidebar, setMobileSidebar] = useState(false)

  // Smooth-scroll to category section
  const scrollToCategory = (cat: string) => {
    setMobileSidebar(false)
    const el = document.getElementById(`cat-${cat.replace(/[^a-z0-9]/gi, '-')}`)
    if (el) el.scrollIntoView({ behavior: 'smooth', block: 'start' })
  }

  // Filter logic — case-insensitive match on question + answer-as-text + category.
  // We stringify React nodes by reading textContent off a temp container only for
  // matching; rendering still uses the original ReactNode answer.
  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase()
    if (!q) return FAQS
    return FAQS.filter((f) => {
      const hayQ = f.question.toLowerCase()
      const hayC = f.category.toLowerCase()
      // Build a plaintext approximation of the answer for searching
      const answerText = (() => {
        const walk = (node: any): string => {
          if (node === null || node === undefined) return ''
          if (typeof node === 'string' || typeof node === 'number') return String(node)
          if (Array.isArray(node)) return node.map(walk).join(' ')
          if (node.props && node.props.children) return walk(node.props.children)
          return ''
        }
        return walk(f.answer).toLowerCase()
      })()
      return hayQ.includes(q) || hayC.includes(q) || answerText.includes(q)
    })
  }, [query])

  // When a search is active, auto-open all matched items so the user can read
  // them inline instead of clicking each one.
  useEffect(() => {
    if (query.trim()) {
      const ids = new Set<string>()
      filtered.forEach((f) => ids.add(`${f.category}::${f.question}`))
      setOpenIds(ids)
    }
  }, [query, filtered])

  // Group filtered FAQs by category, preserving the canonical category order.
  const grouped = useMemo(() => {
    const map = new Map<string, Faq[]>()
    filtered.forEach((f) => {
      const arr = map.get(f.category) || []
      arr.push(f)
      map.set(f.category, arr)
    })
    return CATEGORIES.filter((c) => map.has(c)).map((c) => ({ category: c, items: map.get(c)! }))
  }, [filtered])

  const toggle = (id: string) => {
    setOpenIds((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  const scrollTop = () => window.scrollTo({ top: 0, behavior: 'smooth' })

  return (
    <div className="min-h-screen bg-slate-100 dark:bg-slate-950 text-slate-800 dark:text-slate-200">
      {/* ── Hero ───────────────────────────────────────────────────────── */}
      <section className="bg-gradient-to-b from-white to-slate-100 dark:from-slate-900 dark:to-slate-950 border-b border-slate-200 dark:border-slate-800">
        <div className="max-w-6xl mx-auto px-6 py-12 md:py-16">
          <Link to="/" className="text-sm text-violet-600 dark:text-violet-400 hover:underline">
            &larr; Back to Theta Algos
          </Link>
          <h1 className="text-4xl md:text-5xl font-extrabold mt-4 mb-3 text-slate-900 dark:text-white tracking-tight">
            Help &amp; FAQ
          </h1>
          <p className="text-base md:text-lg text-slate-600 dark:text-slate-300 max-w-2xl">
            Answers to common questions about Theta Algos — accounts, brokers, Saro (our daily stock scanner), strategies, billing, and more. Search below or browse by category. Can&apos;t find what you need? Email <a href="mailto:support@thetaalgos.com" className="text-violet-600 dark:text-violet-400 underline">support@thetaalgos.com</a>.
          </p>

          {/* Search input */}
          <div className="mt-7 max-w-2xl">
            <label className="relative block">
              <span className="sr-only">Search FAQ</span>
              <Search size={18} className="absolute left-4 top-1/2 -translate-y-1/2 text-slate-400" />
              <input
                type="search"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder="Search the FAQ (e.g. KYC, Tradier, EOD close)..."
                className="w-full pl-11 pr-11 py-3.5 rounded-2xl bg-white dark:bg-slate-900 border border-slate-200 dark:border-slate-700 text-base text-slate-900 dark:text-slate-100 placeholder-slate-400 focus:outline-none focus:ring-2 focus:ring-violet-500 focus:border-transparent"
              />
              {query && (
                <button
                  type="button"
                  onClick={() => setQuery('')}
                  aria-label="Clear search"
                  className="absolute right-3 top-1/2 -translate-y-1/2 p-1.5 rounded-lg text-slate-400 hover:bg-slate-100 dark:hover:bg-slate-800"
                >
                  <X size={16} />
                </button>
              )}
            </label>
            <p className="mt-2 text-xs text-slate-500 dark:text-slate-400">
              {query
                ? `${filtered.length} of ${FAQS.length} entries match.`
                : `${FAQS.length} entries across ${CATEGORIES.length} categories.`}
            </p>
          </div>
        </div>
      </section>

      {/* ── Body: sidebar + content ────────────────────────────────────── */}
      <div className="max-w-6xl mx-auto px-6 py-10 md:py-12">
        {/* Mobile category chips */}
        <div className="md:hidden mb-6 -mx-1 overflow-x-auto">
          <div className="flex gap-2 px-1 pb-1">
            <button
              onClick={() => setMobileSidebar((v) => !v)}
              className="flex-shrink-0 inline-flex items-center gap-1.5 px-3 py-1.5 rounded-full text-xs font-semibold bg-violet-600 text-white"
            >
              <Menu size={14} /> Categories
            </button>
            {CATEGORIES.map((c) => (
              <button
                key={c}
                onClick={() => scrollToCategory(c)}
                className="flex-shrink-0 px-3 py-1.5 rounded-full text-xs font-medium bg-white dark:bg-slate-900 border border-slate-200 dark:border-slate-700 text-slate-700 dark:text-slate-200 hover:bg-violet-50 dark:hover:bg-violet-900/20"
              >
                {c}
              </button>
            ))}
          </div>
        </div>

        {/* Mobile collapsible sidebar drawer */}
        {mobileSidebar && (
          <div className="md:hidden mb-6 bg-white dark:bg-slate-900 border border-slate-200 dark:border-slate-800 rounded-2xl p-4">
            <div className="flex items-center justify-between mb-3">
              <span className="text-xs font-bold uppercase tracking-wider text-slate-500 dark:text-slate-400">Categories</span>
              <button onClick={() => setMobileSidebar(false)} aria-label="Close" className="p-1 text-slate-400">
                <X size={16} />
              </button>
            </div>
            <ul className="space-y-1">
              {CATEGORIES.map((c) => (
                <li key={c}>
                  <button
                    onClick={() => scrollToCategory(c)}
                    className="block w-full text-left text-sm px-3 py-2 rounded-lg text-slate-700 dark:text-slate-200 hover:bg-violet-50 dark:hover:bg-violet-900/20"
                  >
                    {c}
                  </button>
                </li>
              ))}
            </ul>
          </div>
        )}

        <div className="grid grid-cols-1 md:grid-cols-[16rem_1fr] gap-8">
          {/* ── Desktop sticky sidebar ── */}
          <aside className="hidden md:block">
            <div className="sticky top-6 bg-white dark:bg-slate-900 border border-slate-200 dark:border-slate-800 rounded-2xl p-4">
              <div className="text-xs font-bold uppercase tracking-wider text-slate-500 dark:text-slate-400 mb-3 px-1">
                Categories
              </div>
              <nav>
                <ul className="space-y-0.5">
                  {CATEGORIES.map((c) => {
                    const count = FAQS.filter((f) => f.category === c).length
                    const matched = grouped.find((g) => g.category === c)
                    const dim = query.trim() && !matched
                    return (
                      <li key={c}>
                        <button
                          onClick={() => scrollToCategory(c)}
                          className={`w-full flex items-center justify-between text-left text-sm px-2.5 py-1.5 rounded-md transition-colors ${
                            dim
                              ? 'text-slate-400 dark:text-slate-600'
                              : 'text-slate-700 dark:text-slate-200 hover:bg-violet-50 dark:hover:bg-violet-900/20'
                          }`}
                        >
                          <span>{c}</span>
                          <span className="text-[10px] text-slate-400 dark:text-slate-500">{count}</span>
                        </button>
                      </li>
                    )
                  })}
                </ul>
              </nav>
              <div className="mt-5 pt-4 border-t border-slate-100 dark:border-slate-800">
                <a
                  href="mailto:support@thetaalgos.com"
                  className="flex items-center gap-2 text-xs text-violet-600 dark:text-violet-400 hover:underline"
                >
                  <Mail size={14} />
                  Email support
                </a>
              </div>
            </div>
          </aside>

          {/* ── Main content ── */}
          <main className="min-w-0">
            {grouped.length === 0 ? (
              <div className="bg-white dark:bg-slate-900 border border-slate-200 dark:border-slate-800 rounded-2xl p-10 text-center">
                <h2 className="text-lg font-bold text-slate-900 dark:text-slate-100 mb-2">No results</h2>
                <p className="text-sm text-slate-600 dark:text-slate-300">
                  Nothing matched &ldquo;<strong>{query}</strong>&rdquo;. Try a different search, browse categories on the left, or email{' '}
                  <a href="mailto:support@thetaalgos.com" className="text-violet-600 dark:text-violet-400 underline">
                    support@thetaalgos.com
                  </a>.
                </p>
              </div>
            ) : (
              <div className="space-y-10">
                {grouped.map(({ category, items }) => (
                  <section
                    key={category}
                    id={`cat-${category.replace(/[^a-z0-9]/gi, '-')}`}
                    className="scroll-mt-6"
                  >
                    <h2 className="text-xl md:text-2xl font-extrabold text-slate-900 dark:text-slate-100 mb-4">
                      {highlight(category, query)}
                    </h2>
                    <div className="space-y-3">
                      {items.map((faq) => {
                        const id = `${faq.category}::${faq.question}`
                        return (
                          <FaqItem
                            key={id}
                            faq={faq}
                            query={query}
                            open={openIds.has(id)}
                            onToggle={() => toggle(id)}
                          />
                        )
                      })}
                    </div>
                  </section>
                ))}
              </div>
            )}

            {/* Footer CTA */}
            <div className="mt-12 rounded-2xl bg-gradient-to-br from-violet-600 to-indigo-700 text-white p-6 md:p-8 text-center">
              <h3 className="text-xl font-bold mb-2">Still stuck?</h3>
              <p className="text-violet-100 text-sm mb-5 max-w-xl mx-auto">
                If the answer to your question isn&apos;t here, the support team reads every email and replies within one business day.
              </p>
              <a
                href="mailto:support@thetaalgos.com"
                className="inline-flex items-center gap-2 bg-white text-violet-700 hover:bg-violet-50 font-semibold px-5 py-2.5 rounded-xl text-sm transition-colors"
              >
                <Mail size={16} />
                Email support@thetaalgos.com
              </a>
            </div>
          </main>
        </div>
      </div>

      {/* Back-to-top */}
      <button
        onClick={scrollTop}
        aria-label="Back to top"
        className="fixed bottom-5 right-5 z-40 bg-white dark:bg-slate-900 border border-slate-200 dark:border-slate-700 text-slate-700 dark:text-slate-200 rounded-full w-11 h-11 flex items-center justify-center shadow-lg hover:bg-violet-50 dark:hover:bg-violet-900/20"
      >
        <ArrowUp size={18} />
      </button>
    </div>
  )
}
