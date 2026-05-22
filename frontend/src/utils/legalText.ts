// Long-form legal text shown in the acknowledgment modals. Versioned so the
// backend can pin the user's recorded acknowledgment to the exact text they
// saw. Bump the version string here AND in legal.py CURRENT_VERSIONS when
// changing any of these documents.

export const TERMS_OF_SERVICE_VERSION = 'v1'
export const RISK_DISCLOSURE_VERSION  = 'v1'
export const LIVE_TRADING_VERSION     = 'v1'

export const TERMS_OF_SERVICE_TEXT = `EDGE ASSET MANAGEMENT LLC — TERMS OF SERVICE

Last updated: 2026-05-08 · Version 1

1. ACCEPTANCE OF TERMS

By creating an account, accessing, or using any part of the Theta Algos software platform ("Service"), you agree to be bound by these Terms of Service. If you do not agree, do not create an account or use the Service.

2. WHAT EDGE ASSET MANAGEMENT IS — AND ISN'T

Theta Algos LLC ("EAM," "we," "us") provides software tools — strategy builders, backtesters, paper-trading simulators, signal-generation engines, and live-execution adapters — that you may use to research and execute your own trading decisions. EAM is NOT a registered investment adviser, broker-dealer, futures commission merchant, commodity trading advisor, or any other registered financial entity. Nothing on the platform constitutes investment advice, a recommendation to buy or sell any security, futures contract, options contract, currency, cryptocurrency, or other financial instrument, or a solicitation to engage in any trading activity.

You are solely responsible for every trading decision you make. Every signal, backtest result, ranked strategy, and recommendation produced by the Service is informational only and the result of automated computation against historical or live market data — not professional advice.

3. TRADING RISK

Trading futures, options, equities, options/futures, and cryptocurrencies involves substantial risk of loss. Possible outcomes include but are not limited to:
  • Loss of your entire deposited capital
  • Margin calls requiring additional deposits beyond your initial principal
  • Negative account balances in certain instrument classes (e.g., futures)
  • Total loss of premium paid on options contracts (options can and frequently do expire worthless)
  • Slippage, partial fills, gap risk, and outsized losses during volatile or illiquid market conditions
  • Technology failures including but not limited to data-feed outages, broker API outages, server outages, internet outages, and software bugs that may delay, prevent, or alter trades

Past performance — including backtested results, simulated results, paper-trading metrics, win rates, profit factors, Sharpe ratios, and any other historical statistic — is NOT indicative of future results. Backtests use approximations of historical conditions and may not reflect real-world slippage, fills, or market impact. Real-world results will likely differ, often materially.

Trade only with capital you can afford to lose in full. Never trade with borrowed funds, retirement funds, or funds required for living expenses.

4. PROP FIRM ACCOUNT RULES

Many prop trading firms (including but not limited to Apex Trader Funding, Topstep, Take Profit Trader, MyFundedFutures, Bulenox, and others) prohibit or restrict automated, algorithmic, or third-party-platform trading on funded accounts. These rules change frequently and without notice.

You are solely responsible for verifying — in writing, with your specific prop firm — whether use of EAM's automated trading features is permitted on your account BEFORE using them. Account closures, withheld payouts, denied withdrawals, banned access, legal action, or any other consequence resulting from rule violations are entirely your responsibility. EAM has no relationship with any prop firm and cannot intervene in disputes with them.

For prop-firm accounts where automation is prohibited, EAM offers the "Account Signals" feature — the bot generates the signal, you place the order manually. This stays compliant with manual-trading rules.

5. LIVE EXECUTION & SANDBOX MODE

Every broker account connected to EAM defaults to "Sandbox Mode" — the bot simulates orders without routing them to your broker. You must explicitly toggle the account to "Live Mode" before real orders are placed. By toggling Live Mode you acknowledge that real money is now at risk and that EAM is not liable for losses incurred.

6. NO GUARANTEE OF UPTIME OR ACCURACY

EAM provides the Service on an "as-is" basis with no guarantee of uptime, accuracy, availability, freedom from bugs, or freedom from data corruption. Market data is sourced from third parties (Yahoo, Polygon, Tradier, Tradovate, broker APIs) and may be delayed, incomplete, incorrect, or temporarily unavailable. Signals generated against bad data may produce incorrect or harmful trades.

7. ACCOUNT & SECURITY

You are responsible for all activity on your account. You agree to use a strong password, enable two-factor authentication, and never share credentials. Compromise of your account through your own negligence is your responsibility. You agree not to attempt to reverse-engineer, scrape, abuse, or circumvent rate limits on the Service.

8. BILLING & REFUNDS

Paid subscriptions auto-renew until cancelled. You may cancel at any time from Profile → Billing; cancellation takes effect at the end of the current billing period. EAM does not generally provide refunds for partial periods.

9. LIMITATION OF LIABILITY

To the maximum extent permitted by law, EAM, its members, employees, contractors, and affiliates shall not be liable for any direct, indirect, incidental, consequential, special, exemplary, or punitive damages — including without limitation lost profits, lost opportunity, loss of data, account closures, or business interruption — arising from or related to your use of the Service, even if EAM has been advised of the possibility of such damages.

EAM's total cumulative liability to you for all claims arising from these Terms or your use of the Service shall not exceed the amount you have paid EAM in subscription fees over the twelve months preceding the claim.

10. INDEMNIFICATION

You agree to indemnify and hold harmless EAM, its members, and affiliates from any claim, loss, liability, or expense (including attorneys' fees) arising from your use of the Service, your violation of these Terms, your violation of any third-party rights or applicable law, your trading losses, or your violation of prop-firm rules.

11. GOVERNING LAW

These Terms are governed by the laws of the State of Delaware, United States, without regard to conflict-of-laws principles. Any dispute shall be resolved exclusively in the state or federal courts located in Delaware, and you consent to that jurisdiction.

12. MODIFICATIONS

EAM may modify these Terms at any time. Material changes will be communicated by email and require re-acknowledgment on next login. Continued use of the Service after modification constitutes acceptance.

13. CONTACT

support@thetaalgos.com`


export const LIVE_TRADING_CONSENT_TEXT = `LIVE TRADING ACKNOWLEDGMENT

By proceeding, I confirm that:

  1. I have read and understood the Theta Algos Terms of Service and Risk Disclosure.

  2. I understand that switching this broker account from Sandbox to Live mode will cause the bot to place REAL orders with REAL money in my brokerage account. Losses incurred will be real.

  3. I have verified that automated trading is permitted on this specific account type. If this is a prop firm account, I have confirmation in writing from the firm that algorithmic / third-party-platform trading is allowed.

  4. I understand that trading futures, options, equities, options/futures, and cryptocurrencies involves substantial risk of loss including possible loss of my entire account, margin calls, and (for some products) negative balances beyond my deposit.

  5. I understand that past performance — including backtest results, paper trading results, and win-rate statistics shown by EAM — is NOT indicative of future results. Real-world results will likely differ from simulated results.

  6. I understand that EAM is a software tool only and is not a registered investment adviser, broker-dealer, or futures commission merchant. Nothing on the platform is investment advice. I alone am responsible for every trade placed through my account.

  7. I accept full responsibility for monitoring my account, managing risk, setting appropriate stops and daily-loss limits, and intervening manually whenever necessary. EAM does not guarantee uptime, accuracy, or freedom from bugs.

  8. I will not hold EAM, its members, employees, or affiliates liable for any losses, account closures, prop-firm rule violations, or other adverse consequences resulting from my use of the live-trading features.

This acknowledgment is logged with the date, time, my IP address, and the version of the Terms in force. It can be reviewed in Profile → Acknowledgments.`


export const RISK_CHANGE_TEXT = (oldPct: number, newPct: number) => `CHANGING RISK PER TRADE

You are about to change the percentage of account equity risked on every trade from ${oldPct.toFixed(2)}% to ${newPct.toFixed(2)}%.

What this means:
  • At ${newPct.toFixed(2)}%, a single losing trade will reduce your account balance by approximately ${newPct.toFixed(2)}% before commissions and slippage.
  • A losing streak of 10 trades at this risk level will reduce the account by approximately ${(newPct * 10).toFixed(0)}% (compounded slightly more).
  • Industry best practice is 1% per trade for most retail traders. Going above 2% is considered aggressive.
  • Risk above 5% per trade significantly increases the probability of account ruin from normal losing streaks.

By proceeding you confirm:
  1. You understand the math above.
  2. You accept full responsibility for the higher loss potential.
  3. You can afford to lose ${newPct.toFixed(2)}% of your trading capital on any single trade.
  4. EAM is not liable for losses resulting from your choice of risk level.`
