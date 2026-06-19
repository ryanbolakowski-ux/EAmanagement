import { useState } from 'react'
import { Link } from 'react-router-dom'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { strategiesApi } from '../api/endpoints'
import type { Strategy, StrategyCreate } from '../types'
import { useAuthStore } from '../stores/authStore'
import { Plus, Edit2, Trash2, TrendingUp, X, BookOpen, Zap, Target, Clock, ChevronDown, ChevronUp, Lightbulb, Shield, AlertTriangle, Check, Copy, Search, Lock, Sparkles, Star, Share2 } from 'lucide-react'
import OptionsStrikePreview from '../components/OptionsStrikePreview'
import OptionsActivateButton from '../components/OptionsActivateButton'

const TIMEFRAMES = ['1m', '2m', '3m', '5m', '15m', '30m', '1H', '4H', '1D']
const INSTRUMENTS = ['ES', 'NQ', 'RTY', 'YM']
const SESSIONS = ['NY', 'LONDON', 'ASIA', 'NY_AM']

const ICT_CONFLUENCES = [
  { id: 'htf_bias', label: 'Higher Time Frame Bias', desc: 'Daily/4H trend direction toward Draw on Liquidity. Price trending toward old highs/lows or HTF FVGs.', tf: 'Daily, H4', howToSee: 'Identify if the overall trend is bullish or bearish and mark the nearest Draw on Liquidity (DOL).', lookFor: 'Price trending toward old highs/lows or HTF Fair Value Gaps (FVG).' },
  { id: 'liquidity_sweep', label: 'Liquidity Sweep (Turtle Soup)', desc: 'Price sweeps beyond key high/low then reverses. Runs above equal highs (BSL) or below equal lows (SSL).', tf: 'H1, M15', howToSee: 'Price briefly moves beyond a key old high or low before sharply reversing.', lookFor: 'Clean runs above equal highs (Buy Side Liquidity) or below equal lows (Sell Side Liquidity).' },
  { id: 'mss_bos', label: 'Market Structure Shift / BOS', desc: 'Break of recent swing high/low with displacement. Energetic candle bodies closing past structural point, leaving FVG.', tf: 'M15, M5, M1', howToSee: 'A sudden break of a recent swing high (for longs) or swing low (for shorts) with displacement.', lookFor: 'Energetic candle bodies closing past the structural point, leaving behind an FVG.' },
  { id: 'premium_discount', label: 'Premium vs Discount', desc: 'Fib retracement of dealing range. Longs in Discount (<50%), Shorts in Premium (>50%).', tf: 'Any', howToSee: 'Use a Fibonacci tool from the recent dealing range high to low.', lookFor: 'Only take longs in Discount (below 50%) and shorts in Premium (above 50%).' },
  { id: 'pd_array', label: 'PD Array Tap (FVG / OB / Breaker)', desc: 'Price retraces into PD Array after displacement. 3-candle gap (FVG), failed order block (Breaker) retest.', tf: 'M15, M5, M1', howToSee: 'Price retraces into a specific Premium/Discount Array after a displacement.', lookFor: 'A 3-candle gap (FVG) or a failed order block (Breaker) that price retests.' },
  { id: 'smt_divergence', label: 'SMT Divergence', desc: 'Correlated assets diverge at key levels. One asset makes new high/low while correlated asset fails to.', tf: 'M15, M5', howToSee: 'Compare correlated assets (e.g., NQ vs. ES, EUR/USD vs. GBP/USD).', lookFor: 'One asset makes a new high/low while the other fails to do so, signaling institutional manipulation.' },
  { id: 'killzones', label: 'Killzones / Macros', desc: 'London 3-4AM, NY 9:30-11AM, London Close 10AM-12PM, NY PM 2-3PM EST.', tf: 'EST windows', howToSee: 'Standard Times (EST): London Open (3–4 AM), NY Open (9:30–11 AM), London Close (10 AM – 12 PM), NY PM Session (2–3 PM).', lookFor: 'Setups occurring specifically within these windows to ensure institutional volume.' },
]

const ORDER_FLOW = [
  { id: 'displacement', label: 'Institutional Sponsorship (Displacement)', desc: 'Large fast-moving impulse candles. Price reacts immediately at OB, does not linger.', howToSee: 'Large, fast-moving candles (impulse moves).', lookFor: 'Market participants protecting a level; price should not linger at an order block but react immediately.' },
  { id: 'ob_validation', label: 'Order Block Validation', desc: 'Last opposing candle before impulse move. Candle bodies respected; close through body = OB disrespected.', howToSee: 'The last down-close candle before an up-move (Bullish OB) or last up-close candle before a down-move (Bearish OB).', lookFor: 'Candle bodies should remain respected; if price closes through the body of an OB, that order flow is disrespected and likely shifting.' },
  { id: 'fvg_respect', label: 'FVG Respect / Rebalance', desc: 'Price returns to FVG and fails to close through. Consequent Encroachment (50% of FVG) acts as S/R.', howToSee: 'Price returns to an FVG and fails to close through it.', lookFor: 'The Consequent Encroachment (50% mark) of the FVG acting as support or resistance.' },
  { id: 'iofed', label: 'IOFED (Institutional Order Flow Entry Drill)', desc: 'LTF tap of HTF PD Array, structure shift, retrace into FVG. Pyramid entries in bias direction.', howToSee: 'On a lower timeframe (e.g., M1/M5), price taps an HTF PD Array, shifts structure, and retraces into an FVG.', lookFor: 'Pyramid entries as price continues to respect newly formed order blocks in the direction of the bias.' },
  { id: 'volume_imbalance', label: 'Volume Imbalance', desc: 'Gap between bodies of consecutive candles where only wicks overlap.', howToSee: 'A gap between the bodies of two consecutive candles where only the wicks overlap.', lookFor: 'Price filling the imbalance and immediately rejecting it, confirming institutional direction.' },
  { id: 'rsi_filter', label: 'RSI Filter (Confirmation)', desc: 'Block longs when RSI is overheated and shorts when RSI is too oversold. Helps avoid entering at exhaustion.', howToSee: 'Look at 14-period RSI on the execution timeframe; default rules block longs above 70 and shorts below 30.', lookFor: 'Trades where bias and RSI agree — i.e. longs while RSI has room to run and shorts while RSI hasn\'t bottomed.' },
  { id: 'vwap_filter', label: 'VWAP Filter (Confirmation)', desc: 'Only take trades on the right side of the session VWAP. Longs require price >= VWAP, shorts require price <= VWAP.', howToSee: 'Anchored VWAP from the start of the current trading day computed from typical price × volume.', lookFor: 'Bias-aligned setups where price is reclaiming or has already cleared VWAP in the trade direction.' },
]

// Convention for every ICT-style template:
//   bias  (htfs)  → 1H or 4H — never lower
//   setup (ptf)   → 5m or 15m — never higher than 15m
//   entry (etf)   → 1m
// The strategy's published methodology decides between 5m vs 15m setup
// and 1H vs 4H bias; we don't randomize it.

const STRATEGY_EXPECTED_WINRATE: Record<string, string> = {
  'ICT Silver Bullet': '65-75%', 'Liquidity Sweep + FVG': '60-70%',
  'SMT Divergence Reversal': '62-72%', 'London Sweep into NY': '68-78%',
  'IOFED Precision Entry': '70-80%', 'NY PM Reversal': '60-68%',
  'Reversal Swing': '62-74%', 'AMD Strategy': '60-72%',
  'Power of 3 (PO3)': '66-76%', 'Judas Swing': '62-72%',
  'ICT 2022 Model (AMD)': '65-78%', 'FVG Inversion Tap': '58-68%',
  'Trend Pullback (Options)': '50-60%', 'Breakout (Options)': '40-50%',
  'Vertical Spread (Options)': '55-65%', 'Earnings/Catalyst (Options)': '45-55%',
  'The Wheel (Options)': '70-80%', 'Pre-Market Gap Runner': '35-45%',
  'Momentum Gappers': '35-45%', 'Low-Float Squeeze': '25-35%',
  '52-Week High Breakout': '40-50%',
  'Oracle — 5-Minute Opening Candle': '55-65%',
  'Futures Signal Scanner (ICT)': '60-70%',
}
function expectedWinRate(name: string): string | null {
  if (!name) return null
  if (STRATEGY_EXPECTED_WINRATE[name]) return STRATEGY_EXPECTED_WINRATE[name]
  const base = name.replace(/\s*\([^)]*\)\s*/g, '').trim()
  return STRATEGY_EXPECTED_WINRATE[base] || null
}

const STRATEGY_TEMPLATES = [
  { name: 'ICT Silver Bullet',     winRate: '65-75%', desc: 'Enter during 10-11 AM EST on FVG after displacement. M1 execution with H1 bias.', confluences: ['htf_bias','mss_bos','pd_array','killzones'], orderFlow: ['displacement','fvg_respect'], rr: 3, instruments: ['ES','NQ'], htfs: ['1H'], ptf: '5m',  etf: '1m' },
  { name: 'Liquidity Sweep + FVG', winRate: '60-70%', desc: 'Wait for sweep of key liquidity, enter on FVG from displacement leg. Classic ICT model.', confluences: ['htf_bias','liquidity_sweep','mss_bos','pd_array'], orderFlow: ['displacement','ob_validation'], rr: 2.5, instruments: ['ES','NQ'], htfs: ['1H'], ptf: '15m', etf: '1m' },
  { name: 'SMT Divergence Reversal',winRate: '62-72%', desc: 'ES/NQ divergence at key levels, enter on structure shift with FVG confirmation.', confluences: ['htf_bias','smt_divergence','mss_bos','premium_discount'], orderFlow: ['displacement','fvg_respect'], rr: 2, instruments: ['ES','NQ'], htfs: ['1H'], ptf: '15m', etf: '1m' },
  { name: 'London Sweep into NY', winRate: '68-78%', desc: 'London sweeps Asian range, NY provides continuation/reversal entry on M1 FVG.', confluences: ['htf_bias','liquidity_sweep','killzones','pd_array'], orderFlow: ['displacement','ob_validation','fvg_respect'], rr: 3, instruments: ['ES','NQ','YM'], htfs: ['4H'], ptf: '15m', etf: '1m' },
  { name: 'IOFED Precision Entry', winRate: '70-80%', desc: 'HTF PD Array tap with H1 bias. 5m structure shift, 1m FVG entry. Highest precision ICT model.', confluences: ['htf_bias','premium_discount','pd_array','mss_bos'], orderFlow: ['iofed','displacement','fvg_respect'], rr: 4, instruments: ['ES','NQ'], htfs: ['1H'], ptf: '5m',  etf: '1m' },
  { name: 'NY PM Reversal',        winRate: '60-68%', desc: 'Afternoon 2-3PM EST reversal after morning exhaustion. FVG + OB confluence.', confluences: ['htf_bias','killzones','pd_array','premium_discount'], orderFlow: ['ob_validation','volume_imbalance'], rr: 2, instruments: ['ES','NQ','YM'], htfs: ['1H'], ptf: '5m',  etf: '1m' },
  { name: 'Reversal Swing',        winRate: '62-74%', desc: '1H/4H bias from HTF FVG respect. Price taps an untapped 15m FVG, rejects, then inverts a 2-3m FVG toward an untapped 4H FVG. Entry on 1m IFVG close, stop at 15m rejection wick.', confluences: ['htf_bias','pd_array','mss_bos','liquidity_sweep'], orderFlow: ['displacement','fvg_respect','ob_validation'], rr: 3, instruments: ['ES','NQ'], htfs: ['4H'], ptf: '15m', etf: '1m' },
  { name: 'AMD Strategy',          winRate: '60-72%', desc: 'Accumulation → Manipulation → Distribution. Liquidity sweep, displacement, FVG forms, retrace into FVG, order-flow confirmation entry. Best in London session; use RSI/VWAP confirmation when liquidity is thin.', confluences: ['htf_bias','liquidity_sweep','mss_bos','pd_array','killzones'], orderFlow: ['displacement','ob_validation','fvg_respect'], rr: 3, instruments: ['ES','NQ'], htfs: ['4H'], ptf: '15m', etf: '1m' },
  { name: 'Power of 3 (PO3)',      winRate: '66-76%', desc: 'Accumulation → Manipulation → Distribution at session level. Sweep one extreme, enter on MSS, target the other extreme.', confluences: ['htf_bias','liquidity_sweep','killzones','mss_bos','premium_discount'], orderFlow: ['displacement','ob_validation','fvg_respect'], rr: 3, instruments: ['ES','NQ','YM'], htfs: ['4H'], ptf: '15m', etf: '1m' },
  { name: 'Judas Swing',           winRate: '62-72%', desc: 'False move at session open that traps traders before the real direction begins. Enter on MSS + FVG after the sweep.', confluences: ['htf_bias','liquidity_sweep','killzones','mss_bos'], orderFlow: ['displacement','fvg_respect'], rr: 3, instruments: ['ES','NQ','YM'], htfs: ['1H'], ptf: '5m',  etf: '1m' },
  { name: 'ICT 2022 Model (AMD)',  winRate: '65-78%', desc: 'Asian range accumulation → London manipulation sweep → NY distribution. Enter NY MSS + FVG in opposite direction of the sweep.', confluences: ['htf_bias','liquidity_sweep','mss_bos','killzones','pd_array'], orderFlow: ['displacement','fvg_respect'], rr: 3, instruments: ['ES','NQ'], htfs: ['4H'], ptf: '15m', etf: '1m' },

  // ── Swing options modes (engine ships when Tradier token is in hand) ───
  // Each carries the user-defined risk rules: 1-2% per trade, 30+ DTE, hard
  // stops outside key S/R, earnings-aware, 2x volume confirmation on breakouts.
  { name: 'Trend Pullback (Options)',     winRate: '50-60%', desc: 'Buy options on pullbacks inside a strong existing trend. 50/200 EMA filter, RSI confirmation, 30-50 delta calls/puts at 30-60 DTE.', confluences: ['htf_bias','rsi_filter','ema_alignment'], orderFlow: ['pullback','support_test'], rr: 2.5, instruments: ['SPY','QQQ','NVDA','AAPL','MSFT'], htfs: ['1D'], ptf: '4H', etf: '1H', optionsMode: 'trend_pullback' },
  { name: 'Breakout (Options)',           winRate: '40-50%', desc: 'Enter on confirmed 20-day high/low breaks with 2x average volume. Hard stop 1% below the breakout level. 30-50 delta, 30-60 DTE.', confluences: ['volume_confirmation','range_breakout'], orderFlow: ['volume_spike','breakout_close'], rr: 2.5, instruments: ['SPY','QQQ','NVDA','TSLA','AMD'], htfs: ['1D'], ptf: '4H', etf: '1H', optionsMode: 'breakout' },
  { name: 'Vertical Spread (Options)',    winRate: '55-65%', desc: 'Defined-risk bull call / bear put spreads. Cuts cost and theta drag when IV is elevated. ATM long leg + 5-strike short leg at 30-60 DTE.', confluences: ['htf_bias','iv_elevated'], orderFlow: ['displacement'], rr: 1.5, instruments: ['SPY','QQQ','NVDA','AAPL'], htfs: ['1D'], ptf: '4H', etf: '1H', optionsMode: 'vertical_spread' },
  { name: 'Earnings/Catalyst (Options)',  winRate: '45-55%', desc: 'Buy ATM straddles 1-3 days before earnings or major catalysts. Profits whichever way the move breaks. Tiny size — IV crush is real.', confluences: ['earnings_proximity','high_iv'], orderFlow: ['catalyst_proximity'], rr: 2.0, instruments: ['NVDA','TSLA','AAPL','META','AMZN','GOOGL'], htfs: ['1D'], ptf: '4H', etf: '1H', optionsMode: 'earnings_catalyst' },
  { name: 'The Wheel (Options)',          winRate: '70-80%', desc: 'Sell cash-secured puts on stocks you want to own. If assigned, sell covered calls until the shares get called away. Slow-and-steady premium collection.', confluences: ['blue_chip','dividend_quality'], orderFlow: ['premium_collection'], rr: 1.0, instruments: ['SPY','AAPL','MSFT','JPM','KO'], htfs: ['1D'], ptf: '1D', etf: '1D', optionsMode: 'wheel' },
]

// ─── Known high win-rate strategy knowledge base ───────────────────────────
const STRATEGY_KNOWLEDGE_DB = [
  {
    name: 'ICT 2022 Model (AMD)',
    category: 'ICT',
    winRate: '65-78%',
    description: 'Accumulation, Manipulation, Distribution model. Wait for Asian range accumulation, London manipulation sweep, then enter NY distribution. Use FVG from manipulation leg as entry.',
    rules: 'Identify Asian session range → Wait for London to sweep one side → Enter on NY session MSS + FVG in opposite direction → Target opposite side liquidity.',
    confluences: ['htf_bias', 'liquidity_sweep', 'mss_bos', 'killzones', 'pd_array'],
    orderFlow: ['displacement', 'fvg_respect'],
    rr: 3,
    bestFor: ['ES', 'NQ'],
  },
  {
    name: 'Unicorn Model (OB + FVG Overlap)',
    category: 'ICT',
    winRate: '70-82%',
    description: 'The Unicorn setup occurs when an Order Block and Fair Value Gap overlap in the same price zone, creating an ultra-high probability entry. This is considered the gold standard of ICT entries.',
    rules: 'Find displacement leg that leaves OB → Identify FVG within or overlapping OB zone → Wait for price to retrace into overlap zone → Enter with tight stop below OB low (longs) or above OB high (shorts).',
    confluences: ['htf_bias', 'pd_array', 'mss_bos', 'premium_discount'],
    orderFlow: ['displacement', 'ob_validation', 'fvg_respect'],
    rr: 4,
    bestFor: ['ES', 'NQ'],
  },
  {
    name: 'Judas Swing',
    category: 'ICT',
    winRate: '62-72%',
    description: 'A false move (the "Judas" swing) at London or NY open that traps traders before the real move begins. The market fakes one direction to grab liquidity before reversing hard.',
    rules: 'Mark previous session high/low → At session open, price sweeps into previous session liquidity → Wait for MSS on M5/M1 → Enter on FVG with stop above/below the sweep.',
    confluences: ['htf_bias', 'liquidity_sweep', 'killzones', 'mss_bos'],
    orderFlow: ['displacement', 'fvg_respect'],
    rr: 3,
    bestFor: ['ES', 'NQ', 'YM'],
  },
  {
    name: 'Optimal Trade Entry (OTE)',
    category: 'ICT',
    winRate: '60-70%',
    description: 'Enter at the 62-79% Fibonacci retracement of a displacement leg that has shifted market structure. This is the sweet spot where institutions reload positions.',
    rules: 'Identify displacement leg with MSS → Draw Fib from swing low to swing high (longs) → Wait for retrace to 62-79% zone → Confirm with FVG or OB in that zone → Enter with stop below 100% level.',
    confluences: ['htf_bias', 'mss_bos', 'premium_discount', 'pd_array'],
    orderFlow: ['displacement', 'ob_validation'],
    rr: 2.5,
    bestFor: ['ES', 'NQ'],
  },
  {
    name: 'Breaker Block Reversal',
    category: 'ICT',
    winRate: '63-73%',
    description: 'A failed Order Block becomes a Breaker Block. When an OB is broken through, it flips polarity and becomes a high-probability entry on retest from the opposite side.',
    rules: 'Identify Order Block that gets broken through with displacement → Mark the broken OB as Breaker Block → Wait for price to retrace back to Breaker zone → Enter on rejection with MSS confirmation on LTF.',
    confluences: ['htf_bias', 'pd_array', 'mss_bos', 'liquidity_sweep'],
    orderFlow: ['displacement', 'ob_validation', 'fvg_respect'],
    rr: 3,
    bestFor: ['ES', 'NQ', 'RTY'],
  },
  {
    name: 'Opening Range Breakout + ICT',
    category: 'Hybrid',
    winRate: '58-68%',
    description: 'Combine traditional Opening Range Breakout with ICT concepts. Define the first 15-30 min range, then trade the breakout only when aligned with HTF bias and confirmed by FVG.',
    rules: 'Mark first 15-30 min range after NY open → Determine HTF bias direction → Wait for price to break range in bias direction → Confirm with FVG on M1/M5 → Enter on FVG retest.',
    confluences: ['htf_bias', 'killzones', 'mss_bos', 'pd_array'],
    orderFlow: ['displacement', 'fvg_respect'],
    rr: 2,
    bestFor: ['ES', 'NQ', 'YM'],
  },
  {
    name: 'NDOG/NWOG Gap Fill',
    category: 'ICT',
    winRate: '64-74%',
    description: 'New Day Opening Gap / New Week Opening Gap strategy. These gaps act as magnets — price tends to fill them. Trade the fill with ICT confluence for high probability.',
    rules: 'Identify NDOG (gap between previous close and current open) or NWOG → Determine if gap aligns with HTF bias → Wait for LTF MSS toward gap fill → Enter on FVG with target at gap fill.',
    confluences: ['htf_bias', 'mss_bos', 'pd_array', 'premium_discount'],
    orderFlow: ['displacement', 'fvg_respect', 'volume_imbalance'],
    rr: 2.5,
    bestFor: ['ES', 'NQ'],
  },
  {
    name: 'Power of 3 (PO3)',
    category: 'ICT',
    winRate: '66-76%',
    description: 'Every candle has an open, high, low, close. PO3 says price accumulates at open, manipulates to one extreme, then distributes to the other. Apply this fractal at session level.',
    rules: 'Identify session opening price → Wait for manipulation (sweep of Asian high/low or previous session high/low) → Enter on MSS after manipulation → Target distribution to opposite extreme.',
    confluences: ['htf_bias', 'liquidity_sweep', 'killzones', 'mss_bos', 'premium_discount'],
    orderFlow: ['displacement', 'ob_validation', 'fvg_respect'],
    rr: 3,
    bestFor: ['ES', 'NQ', 'YM'],
  },
  {
    name: 'Reversal Swing',
    category: 'ICT',
    winRate: '62-74%',
    description: 'Multi-timeframe IFVG continuation. Bias is determined by how price respects 1H and 4H Fair Value Gaps. Price taps an untapped 15m FVG against bias, rejects it, then inverts a 2-3m FVG back in the bias direction toward an untapped 4H FVG target. Entry triggers on the 2-3m candle close of the IFVG, stop at the 15m rejection wick.',
    rules: '1) BIAS: long or short day, based on the trend and whether 1H/4H FVGs are being respected or disrespected. 2) PRICE ACTION: on a shorts day, price rallies up into an untapped 15m FVG, leaving a 2-3m FVG on the way up. On a longs day, price drops into an untapped 15m FVG, leaving a 2-3m FVG on the way down. 3) REJECTION: the 15m FVG rejects price. 4) IFVG: the 2-3m FVG inverts (becomes an IFVG) and points back toward an untapped 4H FVG. 5) ENTRY: on the 2-3m candle close of the IFVG, in bias direction. 6) STOP: at the 15m rejection wick — high for shorts, low for longs. 7) TARGET: the 4H untapped FVG. 8) BREAK-EVEN (use sparingly): once price closes past a previous swing high/low with clear continuation, move SL to +$1. If the move is one-sided and obviously running to TP, do not move to BE — let the trade run.',
    confluences: ['htf_bias', 'pd_array', 'mss_bos', 'liquidity_sweep'],
    orderFlow: ['displacement', 'fvg_respect', 'ob_validation'],
    rr: 3,
    bestFor: ['ES', 'NQ'],
  },
  {
    name: 'AMD Strategy',
    category: 'ICT',
    winRate: '60-72%',
    description: 'Accumulation → Manipulation → Distribution model. Map a liquidity target, wait for the stop-hunt sweep, watch for displacement away from it, take the entry on the FVG retrace once order flow confirms. Trades best during the London session — when London is slow, use RSI / VWAP as a confirmation filter.',
    rules: '1) LIQUIDITY: identify a clear liquidity pool (equal highs/lows, prior session high/low, swing high/low) — this is your target. 2) STOP HUNT: wait for price to run into that liquidity. 3) DISPLACEMENT: a strong impulsive move away from the swept level. 4) FVG: the displacement leaves a Fair Value Gap. 5) RETRACE: price retraces back into the FVG. 6) ENTRY: take the entry on the retrace once order flow (CHoCH / clean wicks / aggressive close) confirms in the displacement direction. 7) SESSION FILTER: prefer London open. If London is sluggish, require alignment with RSI (confluence with overbought/oversold turn) or VWAP (price reclaiming or rejecting VWAP) before pulling the trigger.',
    confluences: ['htf_bias', 'liquidity_sweep', 'mss_bos', 'pd_array', 'killzones'],
    orderFlow: ['displacement', 'ob_validation', 'fvg_respect'],
    rr: 3,
    bestFor: ['ES', 'NQ'],
  },
]

// ─── Component ─────────────────────────────────────────────────────────────

type FormTab = 'setup' | 'confluences' | 'orderflow' | 'risk' | 'notes'

const emptyForm = {
  name: '',
  description: '',
  instruments: ['ES'] as string[],
  primary_timeframe: '15m',
  execution_timeframe: '1m',
  higher_timeframes: ['1H'] as string[],
  risk_reward_ratio: 2.5,
  stop_loss_type: 'structure',
  stop_loss_ticks: 8,
  breakeven_mode: 'structure',
  breakeven_at_r: 1.0,
  max_contracts: 1,
  session_filters: ['NY'] as string[],
  fvg_min_size_ticks: 4,
  fvg_max_size_ticks: 20,
  max_daily_loss: 500,
  max_trades_per_day: 3,
  engine_version: 'v1' as 'v1' | 'v2',
  v2_available: false,
  // Extended ICT fields
  selectedConfluences: [] as string[],
  selectedOrderFlow: [] as string[],
  confluenceNotes: {} as Record<string, string>,
  orderFlowNotes: {} as Record<string, string>,
  entryRules: '',
  exitRules: '',
  additionalNotes: '',
}

export default function StrategyBuilder() {
  const qc = useQueryClient()
  const [showForm, setShowForm] = useState(false)
  const [editId, setEditId] = useState<string | null>(null)
  const [activeTab, setActiveTab] = useState<FormTab>('setup')
  const [form, setForm] = useState({ ...emptyForm })
  const [expandedConfluence, setExpandedConfluence] = useState<string | null>(null)
  const [expandedOrderFlow, setExpandedOrderFlow] = useState<string | null>(null)
  const [showTemplates, setShowTemplates] = useState(false)
  // Tab filter at top of the strategy list — Futures vs Options vs Forex.
  // Decides which strategies render based on each row's instruments / options_mode.
  const [assetTab, setAssetTab] = useState<'futures' | 'options' | 'forex'>('futures')
  const [showKnowledgeDB, setShowKnowledgeDB] = useState(false)
  const [knowledgeSearch, setKnowledgeSearch] = useState('')
  const [deleteConfirm, setDeleteConfirm] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  const { data: strategies = [], isLoading } = useQuery({
    queryKey: ['strategies'],
    queryFn: () => strategiesApi.list().then(r => r.data),
  })

  const createMutation = useMutation({
    mutationFn: (data: StrategyCreate) => strategiesApi.create(data),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ['strategies'] }); closeForm() },
    onError: (e: any) => setError(e?.response?.data?.detail || 'Failed to create strategy'),
  })

  const updateMutation = useMutation({
    mutationFn: ({ id, data }: { id: string; data: StrategyCreate }) => strategiesApi.update(id, data),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ['strategies'] }); closeForm() },
    onError: (e: any) => setError(e?.response?.data?.detail || 'Failed to update strategy'),
  })

  const deleteMutation = useMutation({
    mutationFn: (id: string) => strategiesApi.delete(id),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ['strategies'] }); setDeleteConfirm(null) },
  })

  const starMutation = useMutation({
    mutationFn: ({ id, starred }: { id: string; starred: boolean }) =>
      (strategiesApi as any).setStarred(id, starred),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['strategies'] }),
  })

  const [shareModal, setShareModal] = useState<{ name: string; url: string } | null>(null)
  const [shareCopied, setShareCopied] = useState(false)
  const [shareErrorId, setShareErrorId] = useState<string | null>(null)
  const shareMutation = useMutation({
    mutationFn: (s: any) => (strategiesApi as any).share(s.id).then((r: any) => ({ ...r, _stratName: s.name })),
    onSuccess: (r: any) => {
      setShareErrorId(null)
      setShareCopied(false)
      setShareModal({ name: r._stratName, url: r.data.share_url })
    },
    onError: (_e: any, variables: any) => {
      setShareErrorId(variables?.id || 'unknown')
    },
  })
  function copyShareUrl() {
    if (!shareModal) return
    try {
      navigator.clipboard.writeText(shareModal.url)
      setShareCopied(true)
      setTimeout(() => setShareCopied(false), 2200)
    } catch {
      // Fallback for browsers that block the clipboard API
      const tmp = document.createElement('textarea')
      tmp.value = shareModal.url
      document.body.appendChild(tmp)
      tmp.select()
      document.execCommand('copy')
      document.body.removeChild(tmp)
      setShareCopied(true)
      setTimeout(() => setShareCopied(false), 2200)
    }
  }

  function closeForm() {
    setShowForm(false)
    setEditId(null)
    setActiveTab('setup')
    setForm({ ...emptyForm })
    setError(null)
  }

  function openCreate() {
    setForm({ ...emptyForm })
    setEditId(null)
    setActiveTab('setup')
    setShowForm(true)
    setError(null)
  }

  function openEdit(s: Strategy) {
    // Parse extended ICT data from description
    const rawDesc = s.description || ''
    const descParts = rawDesc.split('\n---')
    const baseDesc = descParts[0]?.trim() || ''

    // Extract confluences
    const confSection = rawDesc.match(/--- ICT Confluences ---\n([\s\S]*?)(?=\n\n---|$)/)
    const selectedConfluences: string[] = []
    if (confSection) {
      ICT_CONFLUENCES.forEach(c => {
        if (confSection[1].includes(c.label)) selectedConfluences.push(c.id)
      })
    }

    // Extract order flow
    const ofSection = rawDesc.match(/--- Order Flow ---\n([\s\S]*?)(?=\n\n---|$)/)
    const selectedOrderFlow: string[] = []
    if (ofSection) {
      ORDER_FLOW.forEach(o => {
        if (ofSection[1].includes(o.label)) selectedOrderFlow.push(o.id)
      })
    }

    // Extract rules and notes
    const entryMatch = rawDesc.match(/--- Entry Rules ---\n([\s\S]*?)(?=\n\n---|$)/)
    const exitMatch = rawDesc.match(/--- Exit Rules ---\n([\s\S]*?)(?=\n\n---|$)/)
    const notesMatch = rawDesc.match(/--- Additional Notes ---\n([\s\S]*?)(?=\n\n---|$)/)

    setForm({
      ...emptyForm,
      name: s.name,
      description: baseDesc,
      instruments: s.instruments || ['ES'],
      primary_timeframe: s.primary_timeframe,
      execution_timeframe: s.execution_timeframe,
      higher_timeframes: s.higher_timeframes || ['1H', '4H'],
      risk_reward_ratio: s.risk_reward_ratio,
      stop_loss_type: s.stop_loss_type,
      breakeven_mode: (s.breakeven_mode as string) || 'structure',
      breakeven_at_r: s.breakeven_at_r ?? 1.0,
      stop_loss_ticks: s.stop_loss_ticks || 8,
      max_contracts: s.max_contracts || 1,
      session_filters: s.session_filters || [],
      fvg_min_size_ticks: s.fvg_min_size_ticks || 4,
      fvg_max_size_ticks: s.fvg_max_size_ticks || 20,
      max_daily_loss: s.max_daily_loss || 500,
      max_trades_per_day: s.max_trades_per_day || 3,
      engine_version: ((s as any).engine_version === 'v2' ? 'v2' : 'v1') as 'v1' | 'v2',
      v2_available: !!(s as any).v2_available,
      selectedConfluences,
      selectedOrderFlow,
      entryRules: entryMatch ? entryMatch[1].trim() : '',
      exitRules: exitMatch ? exitMatch[1].trim() : '',
      additionalNotes: notesMatch ? notesMatch[1].trim() : '',
    })
    setEditId(s.id)
    setActiveTab('setup')
    setShowForm(true)
    setError(null)
  }

  function applyTemplate(template: typeof STRATEGY_TEMPLATES[0]) {
    setForm(prev => ({
      ...prev,
      name: template.name,
      description: template.desc,
      instruments: template.instruments,
      primary_timeframe: template.ptf,
      execution_timeframe: template.etf,
      higher_timeframes: template.htfs,
      risk_reward_ratio: template.rr,
      selectedConfluences: template.confluences,
      selectedOrderFlow: template.orderFlow,
      session_filters: template.confluences.includes('killzones') ? ['NY'] : prev.session_filters,
    }))
    setShowTemplates(false)
  }

  function applyKnowledgeStrategy(strat: typeof STRATEGY_KNOWLEDGE_DB[0]) {
    setForm(prev => ({
      ...prev,
      name: strat.name,
      description: strat.description,
      instruments: strat.bestFor,
      risk_reward_ratio: strat.rr,
      selectedConfluences: strat.confluences,
      selectedOrderFlow: strat.orderFlow,
      entryRules: strat.rules,
    }))
    setShowKnowledgeDB(false)
  }

  function toggleConfluence(id: string) {
    setForm(prev => ({
      ...prev,
      selectedConfluences: prev.selectedConfluences.includes(id)
        ? prev.selectedConfluences.filter(c => c !== id)
        : [...prev.selectedConfluences, id],
    }))
  }

  function toggleOrderFlow(id: string) {
    setForm(prev => ({
      ...prev,
      selectedOrderFlow: prev.selectedOrderFlow.includes(id)
        ? prev.selectedOrderFlow.filter(o => o !== id)
        : [...prev.selectedOrderFlow, id],
    }))
  }

  function toggleInstrument(inst: string) {
    setForm(prev => ({
      ...prev,
      instruments: prev.instruments.includes(inst)
        ? prev.instruments.filter(i => i !== inst)
        : [...prev.instruments, inst],
    }))
  }

  function toggleSession(sess: string) {
    setForm(prev => ({
      ...prev,
      session_filters: prev.session_filters.includes(sess)
        ? prev.session_filters.filter(s => s !== sess)
        : [...prev.session_filters, sess],
    }))
  }

  function handleSubmit() {
    if (!form.name.trim()) { setError('Strategy name is required'); return }
    if (form.instruments.length === 0) { setError('Select at least one instrument'); return }

    const payload: StrategyCreate = {
      name: form.name.trim(),
      description: form.description || undefined,
      instruments: form.instruments,
      primary_timeframe: form.primary_timeframe,
      execution_timeframe: form.execution_timeframe,
      higher_timeframes: form.higher_timeframes,
      risk_reward_ratio: form.risk_reward_ratio,
      stop_loss_type: form.stop_loss_type,
      stop_loss_ticks: form.stop_loss_type === 'ticks' ? form.stop_loss_ticks : undefined,
      breakeven_mode: form.breakeven_mode,
      breakeven_at_r: form.breakeven_at_r,
      max_contracts: form.max_contracts,
      session_filters: form.session_filters,
      fvg_min_size_ticks: form.fvg_min_size_ticks,
      fvg_max_size_ticks: form.fvg_max_size_ticks || undefined,
      max_daily_loss: form.max_daily_loss || undefined,
      max_trades_per_day: form.max_trades_per_day || undefined,
      rule_tree: {
        // Confluence + order flow toggle state, so the engine can read structured
        // flags instead of grepping the description text.
        confluences: form.selectedConfluences,
        order_flow: form.selectedOrderFlow,
        use_rsi_filter: form.selectedOrderFlow.includes('rsi_filter'),
        use_vwap_filter: form.selectedOrderFlow.includes('vwap_filter'),
        engine_version: form.engine_version,
      },
    }

    // Store extended data in the payload description as structured notes
    const extendedDesc = [
      form.description || '',
      form.selectedConfluences.length > 0 ? `\n\n--- ICT Confluences ---\n${form.selectedConfluences.map(c => {
        const conf = ICT_CONFLUENCES.find(x => x.id === c)
        const note = form.confluenceNotes[c]
        return `• ${conf?.label}${note ? ': ' + note : ''}`
      }).join('\n')}` : '',
      form.selectedOrderFlow.length > 0 ? `\n\n--- Order Flow ---\n${form.selectedOrderFlow.map(o => {
        const of_ = ORDER_FLOW.find(x => x.id === o)
        const note = form.orderFlowNotes[o]
        return `• ${of_?.label}${note ? ': ' + note : ''}`
      }).join('\n')}` : '',
      form.entryRules ? `\n\n--- Entry Rules ---\n${form.entryRules}` : '',
      form.exitRules ? `\n\n--- Exit Rules ---\n${form.exitRules}` : '',
      form.additionalNotes ? `\n\n--- Additional Notes ---\n${form.additionalNotes}` : '',
    ].filter(Boolean).join('')

    payload.description = extendedDesc || undefined

    if (editId) {
      updateMutation.mutate({ id: editId, data: payload })
    } else {
      createMutation.mutate(payload)
    }
  }

  const STATUS_COLORS: Record<string, string> = {
    draft: 'badge-grey',
    active: 'badge-green',
    paused: 'badge-amber',
    archived: 'badge-red',
  }

  const filteredKnowledge = STRATEGY_KNOWLEDGE_DB.filter(s =>
    knowledgeSearch === '' ||
    s.name.toLowerCase().includes(knowledgeSearch.toLowerCase()) ||
    s.description.toLowerCase().includes(knowledgeSearch.toLowerCase()) ||
    s.category.toLowerCase().includes(knowledgeSearch.toLowerCase())
  )

  const tabs: { key: FormTab; label: string; icon: any }[] = [
    { key: 'setup', label: 'Setup', icon: Target },
    { key: 'confluences', label: 'ICT Confluences', icon: BookOpen },
    { key: 'orderflow', label: 'Order Flow', icon: Zap },
    { key: 'risk', label: 'Risk Mgmt', icon: Shield },
    { key: 'notes', label: 'Rules & Notes', icon: Edit2 },
  ]

  return (
    <div className="space-y-6 max-w-7xl mx-auto px-4 sm:px-6 py-6">
      {/* HERO */}
      <div className="rounded-3xl bg-gradient-to-br from-slate-900 via-slate-900 to-violet-950 dark:from-slate-950 dark:via-slate-950 dark:to-violet-950 text-white p-6 md:p-8 shadow-xl">
        <div className="flex items-start justify-between gap-4 flex-wrap">
          <div className="min-w-0 flex-1">
            <div className="text-[10px] uppercase tracking-[0.2em] text-violet-300 font-bold mb-1">Library</div>
            <h1 className="text-2xl md:text-3xl font-extrabold text-white">Strategy Builder</h1>
            <p className="text-sm text-slate-400 mt-1">Build, configure, share, and deploy ICT, momentum, and options strategies</p>
          </div>
          <div className="flex items-center gap-2 flex-wrap">
            <button onClick={() => setShowKnowledgeDB(true)}
              className="inline-flex items-center gap-2 bg-white/10 hover:bg-white/20 text-white px-4 py-2 rounded-xl text-sm font-bold transition-colors border border-white/10">
              <Lightbulb size={14}/> Strategy Database
            </button>
            <button onClick={openCreate}
              className="inline-flex items-center gap-2 bg-violet-500 hover:bg-violet-400 text-white px-4 py-2 rounded-xl text-sm font-bold transition-colors shadow-lg shadow-violet-900/30">
              <Plus size={14}/> New Strategy
            </button>
          </div>
        </div>
        <div className="grid grid-cols-3 gap-4 mt-6 pt-6 border-t border-white/10">
          <div>
            <div className="text-[10px] uppercase tracking-wider text-slate-400 font-bold">Total</div>
            <div className="text-2xl font-extrabold tabular-nums">{strategies.length}</div>
          </div>
          <div>
            <div className="text-[10px] uppercase tracking-wider text-slate-400 font-bold">Active</div>
            <div className="text-2xl font-extrabold tabular-nums text-emerald-300">{strategies.filter((s: any) => (s.status||'').toLowerCase()==='active').length}</div>
          </div>
          <div>
            <div className="text-[10px] uppercase tracking-wider text-slate-400 font-bold">Drafts</div>
            <div className="text-2xl font-extrabold tabular-nums text-amber-300">{strategies.filter((s: any) => (s.status||'').toLowerCase()==='draft').length}</div>
          </div>
        </div>
      </div>

      {/* Asset tab bar — Futures / Options / Forex (Forex coming soon) */}
      <div className="flex items-center gap-2 mb-5 border-b border-slate-200 dark:border-slate-700">
        {([
          { id: 'futures', label: 'Futures',  enabled: true },
          { id: 'options', label: 'Options',  enabled: true },        ] as const).map(t => (
          <button key={t.id}
            disabled={!t.enabled}
            onClick={() => t.enabled && setAssetTab(t.id)}
            className={`px-4 py-2.5 text-sm font-bold transition-colors -mb-px border-b-2 ${
              assetTab === t.id
                ? 'border-blue-600 text-blue-600'
                : t.enabled
                  ? 'border-transparent text-slate-500 hover:text-slate-800 dark:text-slate-400 dark:hover:text-slate-200'
                  : 'border-transparent text-slate-300 dark:text-slate-600 cursor-not-allowed'
            }`}>
            {t.label}
            {!t.enabled && <span className="ml-1.5 text-[9px] font-bold uppercase tracking-widest bg-amber-100 text-amber-700 dark:bg-amber-900/40 dark:text-amber-300 px-1.5 py-0.5 rounded">Soon</span>}
          </button>
        ))}
      </div>

      {/* Forex placeholder when its tab is somehow active */}
      

      {/* Futures + Options share the same list rendering, filtered by tab */}
      {assetTab !== 'forex' && (() => {
        const isOptionsStrat = (s: any) => {
          if (s.options_mode) return true
          const opt = ['SPY','QQQ','NVDA','AAPL','MSFT','TSLA','AMD','META','AMZN','GOOGL','JPM','KO']
          return (s.instruments || []).some((i: string) => opt.includes(i))
        }
        const filtered = strategies.filter((s: any) =>
          assetTab === 'options' ? isOptionsStrat(s) : !isOptionsStrat(s)
        ).slice().sort((a: any, b: any) => {
          // Starred first, then alphabetical
          if (!!a.starred !== !!b.starred) return a.starred ? -1 : 1
          return (a.name || '').localeCompare(b.name || '')
        })
        if (isLoading) {
          return (
            <div className="grid md:grid-cols-2 lg:grid-cols-3 gap-4">
              {[...Array(3)].map((_, i) => (
                <div key={i} className="bg-slate-100 rounded-2xl h-40 animate-pulse dark:bg-slate-800"/>
              ))}
            </div>
          )
        }
        if (filtered.length === 0) {
          return (
            <div className="bg-slate-100 rounded-2xl border border-dashed border-slate-200 p-14 text-center dark:bg-slate-800 dark:border-slate-700">
              <TrendingUp size={40} className="mx-auto text-slate-300 mb-4 dark:text-slate-600"/>
              <p className="font-semibold text-slate-500 text-lg mb-2 dark:text-slate-400">No {assetTab} strategies yet</p>
              <p className="text-sm text-slate-400 mb-5 max-w-md mx-auto dark:text-slate-500">
                Build a new {assetTab} strategy from scratch — pick instruments, timeframes, and risk rules.
              </p>
              <button onClick={openCreate}
                className="inline-flex items-center gap-2 bg-blue-600 hover:bg-blue-700 text-white px-5 py-2.5 rounded-xl text-sm font-semibold transition-colors">
                <Plus size={14}/> Build From Scratch
              </button>
            </div>
          )
        }
        return (
        <div className="grid md:grid-cols-2 lg:grid-cols-3 gap-4">
          <div data-id="historical-disclaimer" className="mb-4 text-xs text-slate-500 dark:text-slate-400 bg-amber-50 dark:bg-amber-900/20 border border-amber-200 dark:border-amber-800/40 rounded-lg px-3 py-2 flex items-start gap-2">
          <span className="text-amber-600 dark:text-amber-400 flex-shrink-0 mt-0.5">⚠</span>
          <span><strong className="text-amber-700 dark:text-amber-300">Expected win rates are historical averages</strong>, not a guarantee. Your results will vary based on market regime, position sizing, slippage, and execution discipline.</span>
        </div>
        {filtered.map((s: any) => (
            <div key={s.id} className={`bg-slate-100 rounded-2xl border p-5 hover:shadow-md transition-all group dark:bg-slate-800 ${s.starred ? 'border-amber-300 dark:border-amber-700/60 ring-1 ring-amber-200/60 dark:ring-amber-900/40' : 'border-slate-200 dark:border-slate-700'}`}>
              <div className="flex items-start justify-between mb-3 gap-2">
                <div className="flex items-start gap-2 flex-1 min-w-0">
                  {/* Star toggle — always visible (not hover-gated) so it's easy to find */}
                  <button
                    onClick={() => starMutation.mutate({ id: s.id, starred: !s.starred })}
                    className={`p-1 rounded-lg transition-colors flex-shrink-0 ${s.starred ? 'text-amber-500 hover:text-amber-600' : 'text-slate-300 dark:text-slate-600 hover:text-amber-500'}`}
                    title={s.starred ? 'Unstar' : 'Star this strategy'}>
                    <Star size={15} fill={s.starred ? 'currentColor' : 'none'}/>
                  </button>
                  <div className="flex-1 min-w-0">
                    <h3 className="font-bold text-slate-900 text-sm truncate dark:text-slate-100">{s.name}</h3>
                    <div className="flex flex-wrap items-center gap-1 mt-1">
                      <span className={`badge ${STATUS_COLORS[s.status] || 'badge-grey'}`}>{s.status}</span>
                      {/* QUALITY-GATE-V1: flag unproven/unstable strategies (best completed-backtest WR + stability). */}
                      {s.quality_label === 'unproven' && (
                        <span className="text-[10px] font-bold uppercase px-2 py-0.5 rounded bg-amber-100 text-amber-700 dark:bg-amber-900/40 dark:text-amber-300"
                          title={(s.quality_reasons || []).join('; ') || 'Not yet proven by a clean backtest'}>Unproven</span>
                      )}
                      {s.quality_label === 'unstable' && (
                        <span className="text-[10px] font-bold uppercase px-2 py-0.5 rounded bg-red-100 text-red-700 dark:bg-red-900/40 dark:text-red-300"
                          title={(s.quality_reasons || []).join('; ')}>Unstable</span>
                      )}
                      {s.quality_label === 'proven' && (
                        <span className="text-[10px] font-bold uppercase px-2 py-0.5 rounded bg-emerald-100 text-emerald-700 dark:bg-emerald-900/40 dark:text-emerald-300"
                          title={`Best backtest win-rate ${Math.round((s.best_win_rate || 0) * 100)}% over ${s.best_total_trades} trades`}>Proven {Math.round((s.best_win_rate || 0) * 100)}%</span>
                      )}
                      {/* STRAT-CARD-HONESTY-V1 + PE-HONESTY-V2: distinguish compiled vs truly-empty */}
                      {!(String(s.engine_version).toLowerCase() === 'v2' && s.v2_available) && (!s.rule_tree || Object.keys(s.rule_tree).length === 0) && (
                        <span className="badge badge-grey"
                          title="No compiled rules for this strategy — it runs our generic default ICT logic in backtests and live trading.">generic default</span>
                      )}
                      {!(String(s.engine_version).toLowerCase() === 'v2' && s.v2_available) && s.rule_tree && Object.keys(s.rule_tree).length > 0 && (
                        <span className="badge badge-blue"
                          title="Runs the V1 ICT engine with your compiled filters (e.g. VWAP, range take-profit) + break-even.">compiled</span>
                      )}
                    </div>
                  </div>
                </div>
                <div className="flex items-center gap-1">
                  {/* Share — always visible. Edit/Delete still hover-gated below. */}
                  <button onClick={(e) => { e.stopPropagation(); shareMutation.mutate(s) }}
                    disabled={shareMutation.isPending}
                    title="Share this strategy as a link"
                    className="p-1.5 rounded-lg hover:bg-blue-50 dark:hover:bg-blue-900/30 text-slate-400 hover:text-blue-600 transition-colors dark:text-slate-500">
                    {shareMutation.isPending && shareMutation.variables?.id === s.id ? (
                      <span className="inline-block w-3.5 h-3.5 border-2 border-blue-600 border-r-transparent rounded-full animate-spin"/>
                    ) : (
                      <Share2 size={13}/>
                    )}
                  </button>
                  {shareErrorId === s.id && (
                    <span className="text-[10px] font-bold text-red-500 mr-1">retry</span>
                  )}
                </div>
                <div className="flex items-center gap-1 opacity-0 group-hover:opacity-100 transition-opacity">
                  <button onClick={() => openEdit(s)} className="p-1.5 rounded-lg hover:bg-slate-200 text-slate-400 hover:text-blue-600 transition-colors dark:text-slate-500 dark:hover:bg-slate-700">
                    <Edit2 size={13}/>
                  </button>
                  <button onClick={() => setDeleteConfirm(s.id)} className="p-1.5 rounded-lg hover:bg-red-50 text-slate-400 hover:text-red-500 transition-colors dark:text-slate-500">
                    <Trash2 size={13}/>
                  </button>
                </div>
              </div>
              {s.description && (
                <p className="text-xs text-slate-500 mb-3 line-clamp-2 dark:text-slate-400">{s.description.split('\n---')[0]}</p>
              )}
              <div className="flex flex-wrap gap-1.5 mb-3">
                {s.instruments.map((inst: string) => (
                  <span key={inst} className="badge badge-blue">{inst}</span>
                ))}
                <span className="badge badge-grey">{s.primary_timeframe}</span>
                <span className="badge badge-grey">R:R {s.risk_reward_ratio}</span>
              </div>
              <div className="flex items-center justify-between text-xs text-slate-400 dark:text-slate-500">
                <span>{s.session_filters?.join(', ') || 'All sessions'}</span>
                <span>{new Date(s.created_at).toLocaleDateString()}</span>
              </div>
              {/* Options-mode strategies show what the strike picker would pick today */}
              {assetTab === 'options' && (s.options_mode || (s.instruments || []).some((i: string) => ['SPY','QQQ','NVDA','AAPL','MSFT','TSLA','AMD','META','AMZN','GOOGL','JPM','KO'].includes(i))) && (
                <div className="mt-3 space-y-2">
                  <OptionsStrikePreview
                    strategyId={s.id}
                    underlying={(s.instruments || ['SPY'])[0]}
                  />
                  <div className="flex justify-end">
                    <OptionsActivateButton
                      strategyId={s.id}
                      underlyings={s.instruments || ['SPY']}
                    />
                  </div>
                </div>
              )}
            </div>
          ))}
        </div>
        )
      })()}

      {/* Share-link modal */}
      {shareModal && (
        <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50 p-4">
          <div className="bg-white dark:bg-slate-900 rounded-2xl shadow-2xl w-full max-w-md p-6">
            <div className="flex items-center gap-2 mb-1">
              <Share2 size={16} className="text-blue-600"/>
              <h3 className="font-extrabold text-slate-900 dark:text-slate-100">Share "{shareModal.name}"</h3>
            </div>
            <p className="text-xs text-slate-500 dark:text-slate-400 mb-4">
              Anyone with this link who's signed in can preview the strategy and click <strong>Import</strong> to copy it into their own library. They can't see your trades, backtests, or any other account data.
            </p>
            <div className="flex gap-2 mb-3">
              <input readOnly value={shareModal.url}
                onClick={(e) => e.currentTarget.select()}
                onFocus={(e) => e.currentTarget.select()}
                className="flex-1 border border-slate-300 dark:border-slate-700 dark:bg-slate-800 rounded-lg px-3 py-2 text-xs text-slate-700 dark:text-slate-200 font-mono"/>
              <button onClick={copyShareUrl}
                className={`text-xs font-bold px-4 py-2 rounded-lg inline-flex items-center gap-1.5 transition-colors ${shareCopied ? 'bg-green-600 hover:bg-green-700 text-white' : 'bg-blue-600 hover:bg-blue-700 text-white'}`}>
                {shareCopied ? (<><Check size={12}/> Copied!</>) : (<><Copy size={12}/> Copy</>)}
              </button>
            </div>
            <div className="text-[10.5px] text-slate-400 dark:text-slate-500 mb-4 leading-relaxed">
              The link stays active until you revoke it. To rotate, click Share again with the regenerate option — coming next.
            </div>
            <div className="flex justify-end">
              <button onClick={() => setShareModal(null)}
                className="text-sm font-semibold text-slate-600 dark:text-slate-300 px-4 py-2 rounded-lg hover:bg-slate-100 dark:hover:bg-slate-800">
                Done
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Delete confirmation */}
      {deleteConfirm && (
        <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50 p-4">
          <div className="bg-slate-50 rounded-2xl shadow-2xl w-full max-w-sm p-6 text-center dark:bg-slate-900">
            <AlertTriangle size={32} className="mx-auto text-amber-500 mb-3"/>
            <h3 className="font-bold text-slate-900 mb-2 dark:text-slate-100">Delete Strategy?</h3>
            <p className="text-sm text-slate-500 mb-5 dark:text-slate-400">This will permanently remove this strategy and cannot be undone.</p>
            <div className="flex gap-3">
              <button onClick={() => setDeleteConfirm(null)} className="flex-1 border border-slate-200 text-slate-600 py-2.5 rounded-xl text-sm font-medium dark:text-slate-300 dark:border-slate-700">Cancel</button>
              <button onClick={() => deleteMutation.mutate(deleteConfirm)} className="flex-1 bg-red-500 hover:bg-red-600 text-white py-2.5 rounded-xl text-sm font-semibold transition-colors">Delete</button>
            </div>
          </div>
        </div>
      )}

      {/* ─── Strategy Knowledge Database Modal ─── */}
      {showKnowledgeDB && (
        <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50 p-4">
          <div className="bg-slate-50 rounded-2xl shadow-2xl w-full max-w-3xl max-h-[85vh] flex flex-col dark:bg-slate-900">
            <div className="flex items-center justify-between px-6 py-5 border-b border-slate-200 flex-shrink-0 dark:border-slate-700">
              <div>
                <h2 className="text-base font-bold text-slate-900 dark:text-slate-100">Strategy Knowledge Database</h2>
                <p className="text-xs text-slate-500 mt-0.5 dark:text-slate-400">Proven strategies with 60-80%+ claimed win rates — use as starting points</p>
              </div>
              <button onClick={() => setShowKnowledgeDB(false)} className="p-1.5 rounded-lg hover:bg-slate-200 text-slate-400 dark:text-slate-500 dark:hover:bg-slate-700"><X size={16}/></button>
            </div>
            <div className="px-6 py-3 border-b border-slate-100 flex-shrink-0 dark:border-slate-800">
              <div className="relative">
                <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-slate-400 dark:text-slate-500"/>
                <input type="text" value={knowledgeSearch} onChange={e => setKnowledgeSearch(e.target.value)}
                  placeholder="Search strategies..." className="w-full border border-slate-300 rounded-lg pl-9 pr-3.5 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 dark:border-slate-700"/>
              </div>
            </div>
            <div className="flex-1 overflow-y-auto px-6 py-4 space-y-3">
              {filteredKnowledge.map((strat, idx) => (
                <div key={idx} className="bg-white rounded-xl border border-slate-200 p-4 hover:shadow-sm transition-shadow dark:bg-slate-800 dark:border-slate-700">
                  <div className="flex items-start justify-between mb-2">
                    <div>
                      <h3 className="font-bold text-slate-900 text-sm dark:text-slate-100">{strat.name}</h3>
                      <div className="flex items-center gap-2 mt-1">
                        <span className="badge badge-grey" title="Illustrative range for the published setup — NOT a backtest of your data. Run a backtest to get the real win rate, which depends on your break-even setting.">~{strat.winRate} typical</span>
                        <span className="badge badge-blue">{strat.category}</span>
                        <span className="badge badge-grey">R:R {strat.rr}</span>
                      </div>
                    </div>
                    <button onClick={() => { openCreate(); applyKnowledgeStrategy(strat) }}
                      className="flex items-center gap-1.5 bg-blue-600 hover:bg-blue-700 text-white px-3 py-1.5 rounded-lg text-xs font-semibold transition-colors flex-shrink-0">
                      <Copy size={11}/> Use
                    </button>
                  </div>
                  <p className="text-xs text-slate-600 mb-2 dark:text-slate-300">{strat.description}</p>
                  <div className="bg-slate-50 rounded-lg p-2.5 text-xs text-slate-500 dark:bg-slate-900 dark:text-slate-400">
                    <span className="font-semibold text-slate-700 dark:text-slate-200">Rules: </span>{strat.rules}
                  </div>
                  <div className="flex flex-wrap gap-1 mt-2">
                    {strat.bestFor.map(inst => <span key={inst} className="badge badge-blue text-[10px]">{inst}</span>)}
                    {strat.confluences.map(c => {
                      const conf = ICT_CONFLUENCES.find(x => x.id === c)
                      return conf ? <span key={c} className="badge badge-grey text-[10px]">{conf.label}</span> : null
                    })}
                  </div>
                </div>
              ))}
            </div>
          </div>
        </div>
      )}

      {/* ─── Create/Edit Strategy Modal ─── */}
      {showForm && (
        <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50 p-4">
          <div className="bg-slate-50 rounded-2xl shadow-2xl w-full max-w-3xl max-h-[90vh] flex flex-col dark:bg-slate-900">
            {/* Modal header */}
            <div className="flex items-center justify-between px-6 py-4 border-b border-slate-200 flex-shrink-0 dark:border-slate-700">
              <h2 className="text-base font-bold text-slate-900 dark:text-slate-100">{editId ? 'Edit Strategy' : 'Build New Strategy'}</h2>
              <button onClick={closeForm} className="p-1.5 rounded-lg hover:bg-slate-200 text-slate-400 dark:text-slate-500 dark:hover:bg-slate-700"><X size={16}/></button>
            </div>

            {/* Tab bar */}
            <div className="flex px-6 gap-1 border-b border-slate-200 flex-shrink-0 overflow-x-auto dark:border-slate-700">
              {tabs.map(({ key, label, icon: Icon }) => (
                <button key={key} onClick={() => setActiveTab(key)}
                  className={`flex items-center gap-1.5 px-3 py-3 text-xs font-medium border-b-2 transition-colors whitespace-nowrap ${ activeTab === key ? 'border-blue-600 text-blue-600' : 'border-transparent text-slate-400 hover:text-slate-600' } dark:text-slate-500`}>
                  <Icon size={13}/> {label}
                  {key === 'confluences' && form.selectedConfluences.length > 0 && (
                    <span className="ml-1 bg-blue-600 text-white text-[10px] w-4 h-4 rounded-full flex items-center justify-center">{form.selectedConfluences.length}</span>
                  )}
                  {key === 'orderflow' && form.selectedOrderFlow.length > 0 && (
                    <span className="ml-1 bg-blue-600 text-white text-[10px] w-4 h-4 rounded-full flex items-center justify-center">{form.selectedOrderFlow.length}</span>
                  )}
                </button>
              ))}
            </div>

            {/* Error */}
            {error && (
              <div className="mx-6 mt-3 bg-red-50 dark:bg-red-900/20 border border-red-200 text-red-600 text-xs px-3 py-2 rounded-lg flex items-center gap-2">
                <AlertTriangle size={13}/> {error}
              </div>
            )}

            {/* Tab content */}
            <div className="flex-1 overflow-y-auto px-6 py-5">

              {/* ── Setup Tab ── */}
              {activeTab === 'setup' && (
                <div className="space-y-5">
                  <div>
                    <label className="text-xs font-semibold text-slate-600 uppercase tracking-wider block mb-1.5 dark:text-slate-300">Strategy Name *</label>
                    <input type="text" value={form.name} onChange={e => setForm({...form, name: e.target.value})} placeholder="e.g. ICT Silver Bullet - NY Session"
                      className="w-full border border-slate-300 rounded-lg px-3.5 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 bg-white dark:bg-slate-800 dark:border-slate-700"/>
                  </div>

                  <div>
                    <label className="text-xs font-semibold text-slate-600 uppercase tracking-wider block mb-1.5 dark:text-slate-300">Description</label>
                    <textarea value={form.description} onChange={e => setForm({...form, description: e.target.value})} rows={3}
                      placeholder="Describe your strategy — what it does, when it triggers, what makes it unique..."
                      className="w-full border border-slate-300 rounded-lg px-3.5 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 resize-none bg-white dark:bg-slate-800 dark:border-slate-700"/>
                  </div>

                  <div>
                    <label className="text-xs font-semibold text-slate-600 uppercase tracking-wider block mb-2 dark:text-slate-300">Instruments</label>
                    <div className="flex flex-wrap gap-2">
                      {INSTRUMENTS.map(inst => (
                        <button key={inst} onClick={() => toggleInstrument(inst)}
                          className={`px-4 py-2 rounded-lg text-xs font-semibold border transition-all ${ form.instruments.includes(inst) ? 'bg-blue-600 text-white border-blue-600 shadow-sm' : 'bg-white dark:bg-slate-800 text-slate-600 border-slate-200 hover:border-blue-300' } dark:text-slate-300 dark:border-slate-700`}>
                          {inst}
                        </button>
                      ))}
                    </div>
                  </div>

                  <div className="grid grid-cols-2 gap-4">
                    <div>
                      <label className="text-xs font-semibold text-slate-600 uppercase tracking-wider block mb-1.5 dark:text-slate-300">Primary Timeframe</label>
                      <select value={form.primary_timeframe} onChange={e => setForm({...form, primary_timeframe: e.target.value})}
                        className="w-full border border-slate-300 rounded-lg px-3.5 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 bg-white dark:bg-slate-800 dark:border-slate-700">
                        {TIMEFRAMES.map(tf => <option key={tf} value={tf}>{tf}</option>)}
                      </select>
                    </div>
                    <div>
                      <label className="text-xs font-semibold text-slate-600 uppercase tracking-wider block mb-1.5 dark:text-slate-300">Execution Timeframe</label>
                      <select value={form.execution_timeframe} onChange={e => setForm({...form, execution_timeframe: e.target.value})}
                        className="w-full border border-slate-300 rounded-lg px-3.5 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 bg-white dark:bg-slate-800 dark:border-slate-700">
                        {TIMEFRAMES.map(tf => <option key={tf} value={tf}>{tf}</option>)}
                      </select>
                    </div>
                  </div>

                  <div>
                    <label className="text-xs font-semibold text-slate-600 uppercase tracking-wider block mb-2 dark:text-slate-300">Session Filters</label>
                    <div className="flex flex-wrap gap-2">
                      {SESSIONS.map(sess => (
                        <button key={sess} onClick={() => toggleSession(sess)}
                          className={`px-4 py-2 rounded-lg text-xs font-semibold border transition-all ${ form.session_filters.includes(sess) ? 'bg-blue-600 text-white border-blue-600 shadow-sm' : 'bg-white dark:bg-slate-800 text-slate-600 border-slate-200 hover:border-blue-300' } dark:text-slate-300 dark:border-slate-700`}>
                          {sess === 'NY_AM' ? 'NY AM' : sess}
                        </button>
                      ))}
                    </div>
                  </div>
                </div>
              )}

              {/* ── ICT Confluences Tab ── */}
              {activeTab === 'confluences' && (
                <div className="space-y-3">
                  <p className="text-xs text-slate-500 mb-2 dark:text-slate-400">
                    Select the ICT confluences your strategy requires. Click each for details. Add notes to customize how you apply each confluence.
                  </p>
                  {ICT_CONFLUENCES.map(conf => {
                    const isSelected = form.selectedConfluences.includes(conf.id)
                    const isExpanded = expandedConfluence === conf.id
                    return (
                      <div key={conf.id} className={`rounded-xl border transition-all ${isSelected ? 'border-blue-300 bg-blue-50/30' : 'border-slate-200 bg-white dark:bg-slate-800'}`}>
                        <div className="flex items-center gap-3 p-3.5 cursor-pointer" onClick={() => setExpandedConfluence(isExpanded ? null : conf.id)}>
                          <button onClick={(e) => { e.stopPropagation(); toggleConfluence(conf.id) }}
                            className={`w-5 h-5 rounded-md border-2 flex items-center justify-center flex-shrink-0 transition-all ${ isSelected ? 'bg-blue-600 border-blue-600' : 'border-slate-300 hover:border-blue-400' }`}>
                            {isSelected && <Check size={12} className="text-white"/>}
                          </button>
                          <div className="flex-1 min-w-0">
                            <div className="flex items-center gap-2">
                              <span className="font-semibold text-slate-900 text-sm dark:text-slate-100">{conf.label}</span>
                              <span className="badge badge-grey text-[10px]">{conf.tf}</span>
                            </div>
                            <p className="text-xs text-slate-500 mt-0.5 line-clamp-1 dark:text-slate-400">{conf.desc}</p>
                          </div>
                          {isExpanded ? <ChevronUp size={14} className="text-slate-400 dark:text-slate-500"/> : <ChevronDown size={14} className="text-slate-400 dark:text-slate-500"/>}
                        </div>
                        {isExpanded && (
                          <div className="px-3.5 pb-3.5 space-y-2 border-t border-slate-100 pt-3 ml-8 dark:border-slate-800">
                            <div className="bg-slate-50 rounded-lg p-3 space-y-2 text-xs dark:bg-slate-900">
                              <div><span className="font-semibold text-slate-700 dark:text-slate-200">How to see it: </span><span className="text-slate-600 dark:text-slate-300">{conf.howToSee}</span></div>
                              <div><span className="font-semibold text-slate-700 dark:text-slate-200">What to look for: </span><span className="text-slate-600 dark:text-slate-300">{conf.lookFor}</span></div>
                            </div>
                            <div>
                              <label className="text-[11px] font-medium text-slate-500 block mb-1 dark:text-slate-400">Your notes for this confluence:</label>
                              <textarea
                                value={form.confluenceNotes[conf.id] || ''}
                                onChange={e => setForm(prev => ({ ...prev, confluenceNotes: { ...prev.confluenceNotes, [conf.id]: e.target.value } }))}
                                rows={2}
                                placeholder="How do you specifically apply this in your strategy? Any custom rules or tweaks..."
                                className="w-full border border-slate-200 rounded-lg px-3 py-2 text-xs focus:outline-none focus:ring-2 focus:ring-blue-500 resize-none bg-white dark:bg-slate-800 dark:border-slate-700"
                              />
                            </div>
                          </div>
                        )}
                      </div>
                    )
                  })}
                </div>
              )}

              {/* ── Order Flow Tab ── */}
              {activeTab === 'orderflow' && (
                <div className="space-y-3">
                  <p className="text-xs text-slate-500 mb-2 dark:text-slate-400">
                    Order flow identifies the actual sponsorship or intent of institutions. Select which order flow confirmations your strategy uses.
                  </p>
                  {ORDER_FLOW.map(of_ => {
                    const isSelected = form.selectedOrderFlow.includes(of_.id)
                    const isExpanded = expandedOrderFlow === of_.id
                    return (
                      <div key={of_.id} className={`rounded-xl border transition-all ${isSelected ? 'border-blue-300 bg-blue-50/30' : 'border-slate-200 bg-white dark:bg-slate-800'}`}>
                        <div className="flex items-center gap-3 p-3.5 cursor-pointer" onClick={() => setExpandedOrderFlow(isExpanded ? null : of_.id)}>
                          <button onClick={(e) => { e.stopPropagation(); toggleOrderFlow(of_.id) }}
                            className={`w-5 h-5 rounded-md border-2 flex items-center justify-center flex-shrink-0 transition-all ${ isSelected ? 'bg-blue-600 border-blue-600' : 'border-slate-300 hover:border-blue-400' }`}>
                            {isSelected && <Check size={12} className="text-white"/>}
                          </button>
                          <div className="flex-1 min-w-0">
                            <span className="font-semibold text-slate-900 text-sm dark:text-slate-100">{of_.label}</span>
                            <p className="text-xs text-slate-500 mt-0.5 line-clamp-1 dark:text-slate-400">{of_.desc}</p>
                          </div>
                          {isExpanded ? <ChevronUp size={14} className="text-slate-400 dark:text-slate-500"/> : <ChevronDown size={14} className="text-slate-400 dark:text-slate-500"/>}
                        </div>
                        {isExpanded && (
                          <div className="px-3.5 pb-3.5 space-y-2 border-t border-slate-100 pt-3 ml-8 dark:border-slate-800">
                            <div className="bg-slate-50 rounded-lg p-3 space-y-2 text-xs dark:bg-slate-900">
                              <div><span className="font-semibold text-slate-700 dark:text-slate-200">How to see it: </span><span className="text-slate-600 dark:text-slate-300">{of_.howToSee}</span></div>
                              <div><span className="font-semibold text-slate-700 dark:text-slate-200">What to look for: </span><span className="text-slate-600 dark:text-slate-300">{of_.lookFor}</span></div>
                            </div>
                            <div>
                              <label className="text-[11px] font-medium text-slate-500 block mb-1 dark:text-slate-400">Your notes for this order flow signal:</label>
                              <textarea
                                value={form.orderFlowNotes[of_.id] || ''}
                                onChange={e => setForm(prev => ({ ...prev, orderFlowNotes: { ...prev.orderFlowNotes, [of_.id]: e.target.value } }))}
                                rows={2}
                                placeholder="Describe how you confirm this in real-time..."
                                className="w-full border border-slate-200 rounded-lg px-3 py-2 text-xs focus:outline-none focus:ring-2 focus:ring-blue-500 resize-none bg-white dark:bg-slate-800 dark:border-slate-700"
                              />
                            </div>
                          </div>
                        )}
                      </div>
                    )
                  })}
                </div>
              )}

              {/* ── Risk Management Tab ── */}
              {activeTab === 'risk' && (
                <div className="space-y-5">
                  {form.v2_available && (
                    <div>
                      <label className="text-xs font-semibold text-slate-600 uppercase tracking-wider block mb-1.5 dark:text-slate-300">Engine</label>
                      <select value={form.engine_version} onChange={e => setForm({...form, engine_version: (e.target.value === 'v2' ? 'v2' : 'v1')})}
                        className="w-full border border-slate-300 rounded-lg px-3.5 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 bg-white dark:bg-slate-800 dark:border-slate-700">
                        <option value="v1">V1 — Generic ICT engine (default)</option>
                        <option value="v2">V2 — Dedicated {form.name} setup</option>
                      </select>
                      <p className="text-[11px] text-slate-500 dark:text-slate-400 mt-1">V1 is the battle-tested generic engine. V2 runs this strategy’s dedicated ICT setup (more selective). Switch anytime — it only affects this strategy.</p>
                    </div>
                  )}
                  <div className="grid grid-cols-2 gap-4">
                    <div>
                      <label className="text-xs font-semibold text-slate-600 uppercase tracking-wider block mb-1.5 dark:text-slate-300">Risk:Reward Ratio</label>
                      <input type="number" step="0.5" min="0.5" value={form.risk_reward_ratio} onChange={e => setForm({...form, risk_reward_ratio: parseFloat(e.target.value) || 2})}
                        className="w-full border border-slate-300 rounded-lg px-3.5 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 bg-white dark:bg-slate-800 dark:border-slate-700"/>
                    </div>
                    <div>
                      <label className="text-xs font-semibold text-slate-600 uppercase tracking-wider block mb-1.5 dark:text-slate-300">Max Contracts</label>
                      <input type="number" min="1" value={form.max_contracts} onChange={e => setForm({...form, max_contracts: parseInt(e.target.value) || 1})}
                        className="w-full border border-slate-300 rounded-lg px-3.5 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 bg-white dark:bg-slate-800 dark:border-slate-700"/>
                    </div>
                  </div>

                  <div>
                    <label className="text-xs font-semibold text-slate-600 uppercase tracking-wider block mb-2 dark:text-slate-300">Stop Loss Type</label>
                    <div className="flex gap-3">
                      {['structure', 'ticks'].map(type => (
                        <button key={type} onClick={() => setForm({...form, stop_loss_type: type})}
                          className={`flex-1 px-4 py-3 rounded-xl border text-sm font-medium transition-all ${ form.stop_loss_type === type ? 'bg-blue-600 text-white border-blue-600 shadow-sm' : 'bg-white dark:bg-slate-800 text-slate-600 border-slate-200 hover:border-blue-300' } dark:text-slate-300 dark:border-slate-700`}>
                          {type === 'structure' ? 'Structure-Based' : 'Fixed Ticks'}
                        </button>
                      ))}
                    </div>
                  </div>

                  {form.stop_loss_type === 'ticks' && (
                    <div>
                      <label className="text-xs font-semibold text-slate-600 uppercase tracking-wider block mb-1.5 dark:text-slate-300">Stop Loss Ticks</label>
                      <input type="number" min="1" value={form.stop_loss_ticks} onChange={e => setForm({...form, stop_loss_ticks: parseInt(e.target.value) || 8})}
                        className="w-full border border-slate-300 rounded-lg px-3.5 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 bg-white dark:bg-slate-800 dark:border-slate-700"/>
                    </div>
                  )}

                  <div>
                    <label className="text-xs font-semibold text-slate-600 uppercase tracking-wider block mb-2 dark:text-slate-300">Break-Even Management</label>
                    <div className="flex gap-3">
                      {([['structure','Structure'],['r','Fixed +R'],['off','Off']] as [string,string][]).map(([mode,lbl]) => (
                        <button key={mode} type="button" onClick={() => setForm({...form, breakeven_mode: mode})}
                          className={`flex-1 px-4 py-3 rounded-xl border text-sm font-medium transition-all ${ form.breakeven_mode === mode ? 'bg-blue-600 text-white border-blue-600 shadow-sm' : 'bg-white dark:bg-slate-800 text-slate-600 border-slate-200 hover:border-blue-300' } dark:text-slate-300 dark:border-slate-700`}>
                          {lbl}
                        </button>
                      ))}
                    </div>
                    {form.breakeven_mode === 'r' && (
                      <div className="mt-3">
                        <label className="text-xs font-semibold text-slate-600 uppercase tracking-wider block mb-1.5 dark:text-slate-300">Move to break-even at (R multiple)</label>
                        <input type="number" step="0.5" min="0.5" value={form.breakeven_at_r} onChange={e => setForm({...form, breakeven_at_r: parseFloat(e.target.value) || 1})}
                          className="w-full border border-slate-300 rounded-lg px-3.5 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 bg-white dark:bg-slate-800 dark:border-slate-700"/>
                      </div>
                    )}
                    <p className="text-[11px] text-slate-500 dark:text-slate-400 mt-1.5">
                      <b>Structure</b>: stop slides to entry once price breaks the prior swing — how these setups are actually managed. <b>Fixed +R</b>: move to break-even at a set R multiple. <b>Off</b>: always take the full stop (lowest win rate). Break-even exits count as scratches, not losses, in the win rate.
                    </p>
                  </div>

                  <div className="grid grid-cols-2 gap-4">
                    <div>
                      <label className="text-xs font-semibold text-slate-600 uppercase tracking-wider block mb-1.5 dark:text-slate-300">FVG Min Size (Ticks)</label>
                      <input type="number" min="1" value={form.fvg_min_size_ticks} onChange={e => setForm({...form, fvg_min_size_ticks: parseInt(e.target.value) || 4})}
                        className="w-full border border-slate-300 rounded-lg px-3.5 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 bg-white dark:bg-slate-800 dark:border-slate-700"/>
                    </div>
                    <div>
                      <label className="text-xs font-semibold text-slate-600 uppercase tracking-wider block mb-1.5 dark:text-slate-300">FVG Max Size (Ticks)</label>
                      <input type="number" min="1" value={form.fvg_max_size_ticks || ''} onChange={e => setForm({...form, fvg_max_size_ticks: parseInt(e.target.value) || 0})}
                        className="w-full border border-slate-300 rounded-lg px-3.5 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 bg-white dark:bg-slate-800 dark:border-slate-700" placeholder="No max"/>
                    </div>
                  </div>

                  <div className="grid grid-cols-2 gap-4">
                    <div>
                      <label className="text-xs font-semibold text-slate-600 uppercase tracking-wider block mb-1.5 dark:text-slate-300">Max Daily Loss ($)</label>
                      <input type="number" min="0" value={form.max_daily_loss || ''} onChange={e => setForm({...form, max_daily_loss: parseFloat(e.target.value) || 0})}
                        className="w-full border border-slate-300 rounded-lg px-3.5 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 bg-white dark:bg-slate-800 dark:border-slate-700" placeholder="e.g. 500"/>
                    </div>
                    <div>
                      <label className="text-xs font-semibold text-slate-600 uppercase tracking-wider block mb-1.5 dark:text-slate-300">Max Trades / Day</label>
                      <input type="number" min="1" value={form.max_trades_per_day || ''} onChange={e => setForm({...form, max_trades_per_day: parseInt(e.target.value) || 0})}
                        className="w-full border border-slate-300 rounded-lg px-3.5 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 bg-white dark:bg-slate-800 dark:border-slate-700" placeholder="e.g. 3"/>
                    </div>
                  </div>
                </div>
              )}

              {/* ── Rules & Notes Tab ── */}
              {activeTab === 'notes' && (
                <div className="space-y-5">
                  <div>
                    <label className="text-xs font-semibold text-slate-600 uppercase tracking-wider block mb-1.5 dark:text-slate-300">Entry Rules</label>
                    <textarea value={form.entryRules} onChange={e => setForm({...form, entryRules: e.target.value})} rows={5}
                      placeholder="Describe your exact entry rules step by step. For example:&#10;1. Confirm HTF bias on Daily/4H&#10;2. Wait for liquidity sweep on M15&#10;3. Look for MSS on M5 with displacement&#10;4. Enter on FVG retrace in discount zone&#10;5. Only during NY killzone (9:30-11 AM EST)"
                      className="w-full border border-slate-300 rounded-lg px-3.5 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 resize-none bg-white font-mono dark:bg-slate-800 dark:border-slate-700"/>
                  </div>

                  <div>
                    <label className="text-xs font-semibold text-slate-600 uppercase tracking-wider block mb-1.5 dark:text-slate-300">Exit Rules</label>
                    <textarea value={form.exitRules} onChange={e => setForm({...form, exitRules: e.target.value})} rows={4}
                      placeholder="Describe your exit rules:&#10;- Take profit at opposite liquidity pool&#10;- Stop loss below/above displacement candle&#10;- Trail stop after 1R in profit&#10;- Close at session end if not hit"
                      className="w-full border border-slate-300 rounded-lg px-3.5 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 resize-none bg-white font-mono dark:bg-slate-800 dark:border-slate-700"/>
                  </div>

                  <div>
                    <label className="text-xs font-semibold text-slate-600 uppercase tracking-wider block mb-1.5 dark:text-slate-300">Additional Notes</label>
                    <textarea value={form.additionalNotes} onChange={e => setForm({...form, additionalNotes: e.target.value})} rows={4}
                      placeholder="Any additional notes, market conditions to avoid, personal observations, or psychological rules..."
                      className="w-full border border-slate-300 rounded-lg px-3.5 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 resize-none bg-white dark:bg-slate-800 dark:border-slate-700"/>
                  </div>

                  {/* Strategy summary */}
                  {(form.selectedConfluences.length > 0 || form.selectedOrderFlow.length > 0) && (
                    <div className="bg-slate-100 rounded-xl border border-slate-200 p-4 dark:bg-slate-800 dark:border-slate-700">
                      <h4 className="text-xs font-bold text-slate-700 uppercase tracking-wider mb-3 dark:text-slate-200">Strategy Summary</h4>
                      {form.selectedConfluences.length > 0 && (
                        <div className="mb-3">
                          <span className="text-[11px] font-semibold text-slate-600 dark:text-slate-300">ICT Confluences: </span>
                          <div className="flex flex-wrap gap-1 mt-1">
                            {form.selectedConfluences.map(c => {
                              const conf = ICT_CONFLUENCES.find(x => x.id === c)
                              return <span key={c} className="badge badge-blue text-[10px]">{conf?.label}</span>
                            })}
                          </div>
                        </div>
                      )}
                      {form.selectedOrderFlow.length > 0 && (
                        <div>
                          <span className="text-[11px] font-semibold text-slate-600 dark:text-slate-300">Order Flow: </span>
                          <div className="flex flex-wrap gap-1 mt-1">
                            {form.selectedOrderFlow.map(o => {
                              const of_ = ORDER_FLOW.find(x => x.id === o)
                              return <span key={o} className="badge badge-grey text-[10px]">{of_?.label}</span>
                            })}
                          </div>
                        </div>
                      )}
                    </div>
                  )}
                </div>
              )}
            </div>

            {/* Footer */}
            <div className="flex items-center justify-between px-6 py-4 border-t border-slate-200 flex-shrink-0 dark:border-slate-700">
              <div className="flex items-center gap-2 text-xs text-slate-400 dark:text-slate-500">
                {form.selectedConfluences.length > 0 && <span>{form.selectedConfluences.length} confluences</span>}
                {form.selectedConfluences.length > 0 && form.selectedOrderFlow.length > 0 && <span>·</span>}
                {form.selectedOrderFlow.length > 0 && <span>{form.selectedOrderFlow.length} order flow</span>}
              </div>
              <div className="flex gap-3">
                <button onClick={closeForm} className="border border-slate-200 text-slate-600 px-5 py-2.5 rounded-xl text-sm font-medium dark:text-slate-300 dark:border-slate-700">Cancel</button>
                <button onClick={handleSubmit} disabled={createMutation.isPending || updateMutation.isPending}
                  className="bg-blue-600 hover:bg-blue-700 disabled:opacity-50 text-white px-5 py-2.5 rounded-xl text-sm font-semibold transition-colors">
                  {createMutation.isPending || updateMutation.isPending ? 'Saving...' : editId ? 'Update Strategy' : 'Create Strategy'}
                </button>
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

function StrategyBuilderPaywall() {
  const features = [
    'Build unlimited ICT-based futures strategies',
    'Pick from 50+ vetted high-win-rate templates',
    'Configure confluences, order flow, and risk rules',
    'Run backtests on every strategy you create',
  ]
  return (
    <div className="p-8 max-w-3xl mx-auto">
      <div className="rounded-2xl border border-slate-200 bg-white shadow-sm overflow-hidden dark:bg-slate-800 dark:border-slate-700">
        <div className="bg-gradient-to-br from-blue-600 to-indigo-600 text-white px-8 py-10 text-center">
          <div className="inline-flex items-center justify-center w-14 h-14 rounded-2xl bg-white dark:bg-slate-800/15 mb-4">
            <Lock size={26} />
          </div>
          <h1 className="text-2xl font-extrabold">Strategy Builder is a paid feature</h1>
          <p className="text-blue-100 text-sm mt-2 max-w-md mx-auto">
            Upgrade to Tier 2 or above to design, save, and backtest your own ICT trading strategies.
          </p>
        </div>
        <div className="px-8 py-7">
          <ul className="space-y-3 mb-7">
            {features.map((f) => (
              <li key={f} className="flex items-start gap-3 text-sm text-slate-700 dark:text-slate-200">
                <span className="mt-0.5 w-5 h-5 rounded-full bg-blue-50 dark:bg-blue-900/20 text-blue-600 flex items-center justify-center flex-shrink-0">
                  <Check size={12} strokeWidth={3} />
                </span>
                {f}
              </li>
            ))}
          </ul>
          <div className="flex flex-col sm:flex-row gap-3">
            <Link
              to="/pricing"
              className="flex-1 inline-flex items-center justify-center gap-2 bg-blue-600 hover:bg-blue-700 text-white px-5 py-3 rounded-xl text-sm font-semibold transition-colors shadow-sm shadow-blue-200"
            >
              <Sparkles size={14} /> View plans
            </Link>
            <Link
              to="/app"
              className="flex-1 inline-flex items-center justify-center border border-slate-200 hover:border-slate-300 text-slate-700 hover:text-slate-900 px-5 py-3 rounded-xl text-sm font-medium transition-colors dark:text-slate-200 dark:border-slate-700"
            >
              Back to dashboard
            </Link>
          </div>
        </div>
      </div>
    </div>
  )
}
