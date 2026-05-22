import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Sparkles, Edit2, Trash2, Plus, Check, AlertCircle, ChevronRight, BookOpen, X } from 'lucide-react'
import { strategiesApi } from '../api/endpoints'
import api from '../api/client'

type Step = {
  id: string
  kind: 'bias' | 'setup' | 'entry' | 'stop' | 'target' | 'risk' | 'session' | 'custom'
  title: string
  body: string
  // Auto-detected tokens we surface as labels
  timeframes: string[]
  concepts: string[]
  levels: string[]
  // Clarifications the parser couldn't resolve — user must answer
  questions: string[]
}

const STEP_LABELS: Record<Step['kind'], string> = {
  bias:    'Bias',
  setup:   'Setup',
  entry:   'Entry',
  stop:    'Stop Loss',
  target:  'Take Profit',
  risk:    'Risk',
  session: 'Session Filter',
  custom:  'Custom Step',
}

// Tiny client-side parser — pulls timeframes/concepts/levels out of the
// free-text. Same logic on the server for richer detection; this is a
// preview the user sees as they type.
function quickScan(text: string) {
  const lo = text.toLowerCase()
  const tfRe = /\b(\d{1,2})\s*-?\s*(\d{1,2})?\s*(m|min|minute|h|hr|hour|d|day|w|week)s?\b/gi
  const tfs = new Set<string>()
  let m
  while ((m = tfRe.exec(text))) {
    const a = parseInt(m[1])
    const unit = (m[3] || '').toLowerCase()
    const norm = unit.startsWith('m') ? `${a}m` : unit.startsWith('h') ? `${a}h` : unit.startsWith('d') ? `${a}d` : `${a}w`
    tfs.add(norm)
    if (m[2]) {
      const b = parseInt(m[2])
      const normB = unit.startsWith('m') ? `${b}m` : unit.startsWith('h') ? `${b}h` : unit.startsWith('d') ? `${b}d` : `${b}w`
      tfs.add(normB)
    }
  }
  const concepts: string[] = []
  const map: [RegExp, string][] = [
    [/fvg\b|fair value gap/, 'FVG'],
    [/ifvg\b|inverse.*fvg|inversion/, 'IFVG / Inversion'],
    [/displacement/, 'Displacement'],
    [/sweep|stop.?hunt|liquidity.?grab/, 'Liquidity Sweep'],
    [/bias\b/, 'Bias'],
    [/order ?block|\bob\b/, 'Order Block'],
    [/breaker/, 'Breaker'],
    [/mss\b|market structure shift|choch/, 'MSS / CHoCH'],
    [/bos\b|break of structure/, 'BOS'],
    [/fib(?:onacci)?/, 'Fibonacci'],
    [/premium|discount|pd array/, 'PD Array'],
    [/ema|moving average/, 'EMA'],
    [/rsi\b/, 'RSI'],
    [/vwap\b/, 'VWAP'],
    [/london/, 'London Session'],
    [/asia(?:n)?/, 'Asian Session'],
    [/new york|ny\s|killzone/, 'NY Session'],
    [/previous (?:session|day)/, 'Previous Session'],
    [/equal highs|equal lows|eqh|eql/, 'Equal Highs/Lows'],
    [/range/, 'Range'],
  ]
  for (const [re, label] of map) {
    if (re.test(lo) && !concepts.includes(label)) concepts.push(label)
  }
  const levels: string[] = []
  const fibRe = /\b(0?\.?\d{1,3})\s*%?\s*(?:fib|retrace)/gi
  while ((m = fibRe.exec(text))) levels.push(`Fib ${m[1]}`)
  if (/\b50%?\s*(?:level|line|line)?\b/.test(lo) && !levels.includes('Fib 50')) levels.push('Fib 50')
  return { timeframes: Array.from(tfs), concepts, levels }
}

const TERMS_NEEDING_CLARIFICATION = [
  'mark out', 'mark the range', 'draw a',
  'somewhere around', 'around the', 'roughly', 'kinda', 'sort of',
]

function detectQuestions(body: string): string[] {
  const lo = body.toLowerCase()
  const qs: string[] = []
  for (const term of TERMS_NEEDING_CLARIFICATION) {
    if (lo.includes(term)) {
      qs.push(`You said "${term}…" — can you mark this on a chart so I capture it exactly? (interactive chart annotation coming soon — for now please add the precise rule in the description)`)
    }
  }
  if (/some|maybe|usually|sometimes|sort of/.test(lo)) {
    qs.push('"' + (body.match(/(some|maybe|usually|sometimes|sort of)/i)?.[1] || '') + '" is ambiguous. Pin it down: under what condition exactly?')
  }
  return qs
}

const STEP_TEMPLATES: { kind: Step['kind']; title: string; placeholder: string }[] = [
  { kind: 'bias',    title: 'Step 1 — Bias',         placeholder: 'e.g. Bias is determined on the 1H or 4H chart using EMA crossover. Bullish when 9 EMA above 21 EMA.' },
  { kind: 'setup',   title: 'Step 2 — Setup',        placeholder: 'e.g. Mark a 15m FVG opposite to bias direction. If bullish bias, look for a 15m FVG on the downside.' },
  { kind: 'entry',   title: 'Step 3 — Entry',        placeholder: 'e.g. Wait for price to tap into the 15m FVG. Entry is on the closure of the 1-3m IFVG candle on the way back.' },
  { kind: 'stop',    title: 'Step 4 — Stop Loss',    placeholder: 'e.g. Stop loss = the reversal-point low (longs) or high (shorts).' },
  { kind: 'target',  title: 'Step 5 — Take Profit',  placeholder: 'e.g. Target the nearest untapped 1H or 4H FVG. If none, use the previous session high (longs) or low (shorts).' },
  { kind: 'risk',    title: 'Step 6 — Risk',         placeholder: 'e.g. Risk 1% of equity per trade. Max 2 trades per day.' },
  { kind: 'session', title: 'Step 7 — Session',      placeholder: 'e.g. Only trade during NY AM (9:30-12 ET) and London open (3-5 ET).' },
]

export default function AIStrategyBuilder() {
  const navigate = useNavigate()
  const qc = useQueryClient()

  const [name, setName] = useState('')
  const [instruments, setInstruments] = useState<string[]>([])
  const [instrumentDraft, setInstrumentDraft] = useState('')
  const [showInstrumentField, setShowInstrumentField] = useState(true)

  function saveInstrument() {
    const cleaned = instrumentDraft.trim().toUpperCase()
    if (!cleaned) return
    if (instruments.includes(cleaned)) {
      setInstrumentDraft('')
      setShowInstrumentField(false)
      return
    }
    setInstruments(p => [...p, cleaned])
    setInstrumentDraft('')
    setShowInstrumentField(false)
  }
  function removeInstrument(inst: string) {
    setInstruments(p => p.filter(i => i !== inst))
  }
  const [steps, setSteps] = useState<Step[]>(STEP_TEMPLATES.slice(0, 5).map((t, i) => ({
    id: String(i),
    kind: t.kind,
    title: t.title,
    body: '',
    timeframes: [],
    concepts: [],
    levels: [],
    questions: [],
  })))
  const [editingStep, setEditingStep] = useState<string | null>('0')
  const [previewMode, setPreviewMode] = useState(false)

  function updateStep(id: string, body: string) {
    setSteps(prev => prev.map(s => {
      if (s.id !== id) return s
      const scan = quickScan(body)
      const questions = detectQuestions(body)
      return { ...s, body, ...scan, questions }
    }))
  }
  function addCustom() {
    const id = String(Date.now())
    setSteps(prev => [...prev, { id, kind: 'custom', title: `Step ${prev.length + 1} — Custom`, body: '', timeframes: [], concepts: [], levels: [], questions: [] }])
    setEditingStep(id)
  }
  function removeStep(id: string) {
    setSteps(prev => prev.filter(s => s.id !== id))
  }

  const saveMutation = useMutation({
    mutationFn: async () => {
      // Pull the highest timeframe mentioned anywhere as the bias TF,
      // smallest as execution. Mid value as primary/setup. Fallbacks if nothing parsed.
      const allTfs = Array.from(new Set(steps.flatMap(s => s.timeframes)))
      const tfRank = (tf: string) => {
        const n = parseInt(tf)
        const u = tf.slice(-1)
        if (u === 'm') return n
        if (u === 'h') return n * 60
        if (u === 'd') return n * 60 * 24
        return n * 60 * 24 * 7
      }
      const sorted = allTfs.sort((a, b) => tfRank(a) - tfRank(b))
      const exec = sorted[0] || '1m'
      const primary = sorted.find(t => tfRank(t) >= 5 && tfRank(t) <= 60) || '15m'
      const htfs = sorted.filter(t => tfRank(t) >= 60).slice(0, 2)
      const fullDescription = steps.filter(s => s.body.trim()).map(s => `${s.title}\n${s.body}`).join('\n\n---\n\n')
      return strategiesApi.create({
        name: name.trim() || 'My Custom Strategy',
        description: fullDescription,
        instruments,
        primary_timeframe: primary,
        execution_timeframe: exec,
        higher_timeframes: htfs.length > 0 ? htfs : ['1H'],
        risk_reward_ratio: 3,
        stop_loss_type: 'structure',
        max_contracts: 10,
        session_filters: [],
        fvg_min_size_ticks: 4,
        rule_tree: {},
      } as any)
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['strategies'] })
      navigate('/app/strategies')
    },
  })

  const hasContent = steps.some(s => s.body.trim().length > 10)
  const allQuestions = steps.flatMap(s => s.questions.map(q => ({ stepId: s.id, q })))

  if (previewMode) {
    return (
      <div className="space-y-6 max-w-4xl mx-auto px-4 sm:px-6 py-6">
        <div className="flex items-center justify-between gap-4">
          <div>
            <h1 className="text-2xl font-extrabold text-slate-900 dark:text-slate-100 flex items-center gap-2">
              <BookOpen size={22}/> Review your strategy
            </h1>
            <p className="text-sm text-slate-500 dark:text-slate-400 mt-1">Final read-through before saving. Click any step to edit.</p>
          </div>
          <button onClick={() => setPreviewMode(false)}
            className="text-sm font-semibold text-slate-600 hover:text-slate-800 dark:text-slate-300">← Back to editor</button>
        </div>

        <div className="rounded-2xl border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900 p-6 space-y-1">
          <div className="text-[10px] font-bold uppercase tracking-widest text-blue-600 dark:text-blue-400">Strategy Name</div>
          <div className="text-xl font-extrabold text-slate-900 dark:text-slate-100">{name.trim() || 'My Custom Strategy'}</div>
          <div className="text-xs text-slate-500 dark:text-slate-400">Instruments: {instruments.join(', ')}</div>
        </div>

        {steps.filter(s => s.body.trim()).map((s, idx) => (
          <div key={s.id} className="rounded-2xl border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900 p-6 group">
            <div className="flex items-start justify-between mb-2">
              <div>
                <div className="text-[10px] font-bold uppercase tracking-widest text-blue-600 dark:text-blue-400">{STEP_LABELS[s.kind]}</div>
                <div className="text-lg font-extrabold text-slate-900 dark:text-slate-100">{s.title.replace(/^Step \d+ — /, `${idx + 1}. `)}</div>
              </div>
              <button onClick={() => { setEditingStep(s.id); setPreviewMode(false) }}
                className="text-xs font-semibold text-blue-600 hover:text-blue-700 inline-flex items-center gap-1">
                <Edit2 size={12}/> Edit
              </button>
            </div>
            <p className="text-sm text-slate-700 dark:text-slate-200 leading-relaxed whitespace-pre-line">{s.body}</p>
            <div className="flex flex-wrap gap-1.5 mt-3">
              {s.timeframes.map(tf => <span key={tf} className="text-[10px] font-bold uppercase tracking-wider bg-blue-100 text-blue-700 dark:bg-blue-900/40 dark:text-blue-300 px-1.5 py-0.5 rounded">{tf}</span>)}
              {s.concepts.map(c => <span key={c} className="text-[10px] font-semibold bg-slate-100 text-slate-700 dark:bg-slate-800 dark:text-slate-300 px-1.5 py-0.5 rounded">{c}</span>)}
              {s.levels.map(l => <span key={l} className="text-[10px] font-semibold bg-amber-100 text-amber-700 dark:bg-amber-900/40 dark:text-amber-300 px-1.5 py-0.5 rounded">{l}</span>)}
            </div>
          </div>
        ))}

        {allQuestions.length > 0 && (
          <div className="rounded-2xl border border-amber-200 dark:border-amber-800 bg-amber-50 dark:bg-amber-900/20 p-5">
            <div className="flex items-center gap-2 mb-3">
              <AlertCircle size={16} className="text-amber-600 dark:text-amber-400"/>
              <h2 className="text-sm font-bold uppercase tracking-widest text-amber-800 dark:text-amber-300">Clarifications I need from you</h2>
            </div>
            <ul className="text-sm text-amber-900 dark:text-amber-200 space-y-2 leading-relaxed list-disc list-inside">
              {allQuestions.map((q, i) => <li key={i}>{q.q}</li>)}
            </ul>
            <p className="text-[11px] text-amber-700 dark:text-amber-300 mt-3 italic">Resolve these before saving for the best results.</p>
          </div>
        )}

        <div className="flex justify-end gap-3">
          <button onClick={() => setPreviewMode(false)}
            className="px-5 py-2.5 rounded-xl border border-slate-200 dark:border-slate-700 text-slate-600 dark:text-slate-300 text-sm font-semibold">Keep editing</button>
          <button onClick={() => saveMutation.mutate()}
            disabled={!hasContent || saveMutation.isPending || !name.trim()}
            className="px-6 py-2.5 rounded-xl bg-blue-600 hover:bg-blue-700 disabled:opacity-50 text-white text-sm font-bold inline-flex items-center gap-2">
            <Check size={14}/> {saveMutation.isPending ? 'Saving…' : 'Save Strategy'}
          </button>
        </div>
      </div>
    )
  }

  return (
    <div className="space-y-6 max-w-4xl mx-auto px-4 sm:px-6 py-6">
      <div className="flex items-center justify-between gap-4">
        <div>
          <h1 className="text-2xl font-extrabold text-slate-900 dark:text-slate-100 flex items-center gap-2">
            <Sparkles size={22}/> Plain-English Strategy Builder
          </h1>
          <p className="text-sm text-slate-500 dark:text-slate-400 mt-1">Describe your strategy step by step in your own words. The bot reads it, pulls out timeframes / FVG concepts / levels, and flags anything ambiguous before you save.</p>
        </div>
      </div>

      {/* Header — name + instruments */}
      <div className="rounded-2xl border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900 p-5 space-y-3">
        <div>
          <label className="text-xs font-bold uppercase tracking-widest text-slate-500 dark:text-slate-400 block mb-1.5">Strategy Name</label>
          <input value={name} onChange={e => setName(e.target.value)}
            placeholder="e.g. My Inversion Hunter"
            className="w-full border border-slate-300 dark:border-slate-700 dark:bg-slate-800 rounded-lg px-3.5 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"/>
        </div>
        <div>
          <label className="text-xs font-bold uppercase tracking-widest text-slate-500 dark:text-slate-400 block mb-1.5">
            {instruments.length === 0 ? 'What instrument do you want to trade?' : 'Instruments'}
          </label>

          {/* Already-added instruments as removable chips */}
          {instruments.length > 0 && (
            <div className="flex flex-wrap gap-2 mb-3">
              {instruments.map(inst => (
                <span key={inst} className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-bold bg-blue-600 text-white">
                  {inst}
                  <button type="button" onClick={() => removeInstrument(inst)}
                    className="opacity-80 hover:opacity-100 -mr-1">
                    <X size={11}/>
                  </button>
                </span>
              ))}
            </div>
          )}

          {/* Active typed-input — only visible when adding */}
          {showInstrumentField ? (
            <div className="flex gap-2">
              <input
                autoFocus
                value={instrumentDraft}
                onChange={e => setInstrumentDraft(e.target.value)}
                onKeyDown={e => { if (e.key === 'Enter') { e.preventDefault(); saveInstrument() } }}
                placeholder="e.g. NQ, ES, MNQ, SPY, NVDA…"
                className="flex-1 border border-slate-300 dark:border-slate-700 dark:bg-slate-800 rounded-lg px-3.5 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 uppercase"/>
              <button type="button" onClick={saveInstrument} disabled={!instrumentDraft.trim()}
                className="px-5 py-2.5 rounded-lg bg-blue-600 hover:bg-blue-700 disabled:opacity-50 text-white text-sm font-bold">
                Save
              </button>
              {instruments.length > 0 && (
                <button type="button" onClick={() => { setShowInstrumentField(false); setInstrumentDraft('') }}
                  className="px-4 py-2.5 rounded-lg border border-slate-300 dark:border-slate-700 text-slate-600 dark:text-slate-300 text-sm font-semibold">
                  Cancel
                </button>
              )}
            </div>
          ) : (
            <button type="button" onClick={() => setShowInstrumentField(true)}
              className="inline-flex items-center gap-2 text-sm font-semibold text-blue-600 hover:text-blue-700 px-3 py-2 rounded-lg border border-dashed border-blue-300 hover:border-blue-500 hover:bg-blue-50 dark:hover:bg-blue-900/20 transition">
              <Plus size={14}/> Add another instrument
            </button>
          )}
          <p className="text-[10.5px] text-slate-400 dark:text-slate-500 mt-1.5">Type the symbol (case insensitive). Press <kbd className="px-1 py-0.5 bg-slate-100 dark:bg-slate-800 rounded text-[9px]">Enter</kbd> or click Save. Add as many as the strategy applies to.</p>
        </div>
      </div>

      {/* Steps */}
      {steps.map((step, idx) => (
        <div key={step.id} className="rounded-2xl border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900 overflow-hidden">
          <button onClick={() => setEditingStep(editingStep === step.id ? null : step.id)}
            className="w-full flex items-center justify-between p-5 text-left">
            <div className="min-w-0">
              <div className="text-[10px] font-bold uppercase tracking-widest text-blue-600 dark:text-blue-400">{STEP_LABELS[step.kind]}</div>
              <div className="text-base font-extrabold text-slate-900 dark:text-slate-100 mt-0.5">{step.title}</div>
              {step.body && !editingStep ? (
                <p className="text-xs text-slate-500 dark:text-slate-400 mt-1.5 line-clamp-2">{step.body}</p>
              ) : null}
            </div>
            <div className="flex items-center gap-3 flex-shrink-0">
              {step.kind === 'custom' && (
                <button onClick={(e) => { e.stopPropagation(); removeStep(step.id) }}
                  className="p-1.5 rounded-lg text-slate-400 hover:text-red-500 hover:bg-red-50 dark:hover:bg-red-900/20">
                  <Trash2 size={14}/>
                </button>
              )}
              <ChevronRight size={18} className={`text-slate-400 transition-transform ${editingStep === step.id ? 'rotate-90' : ''}`}/>
            </div>
          </button>

          {editingStep === step.id && (
            <div className="border-t border-slate-100 dark:border-slate-800 p-5 space-y-3">
              <textarea
                rows={5}
                value={step.body}
                onChange={e => updateStep(step.id, e.target.value)}
                placeholder={STEP_TEMPLATES.find(t => t.kind === step.kind)?.placeholder || 'Describe this part of the strategy in your own words…'}
                className="w-full border border-slate-300 dark:border-slate-700 dark:bg-slate-800 rounded-lg px-3.5 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 resize-none leading-relaxed"
              />

              {/* Auto-detected chips */}
              {(step.timeframes.length + step.concepts.length + step.levels.length > 0) && (
                <div>
                  <div className="text-[10px] font-bold uppercase tracking-widest text-slate-500 dark:text-slate-400 mb-1.5">Detected automatically</div>
                  <div className="flex flex-wrap gap-1.5">
                    {step.timeframes.map(tf => <span key={tf} className="text-[10px] font-bold uppercase tracking-wider bg-blue-100 text-blue-700 dark:bg-blue-900/40 dark:text-blue-300 px-1.5 py-0.5 rounded">{tf}</span>)}
                    {step.concepts.map(c => <span key={c} className="text-[10px] font-semibold bg-slate-100 text-slate-700 dark:bg-slate-800 dark:text-slate-300 px-1.5 py-0.5 rounded">{c}</span>)}
                    {step.levels.map(l => <span key={l} className="text-[10px] font-semibold bg-amber-100 text-amber-700 dark:bg-amber-900/40 dark:text-amber-300 px-1.5 py-0.5 rounded">{l}</span>)}
                  </div>
                </div>
              )}

              {/* Inline clarifications */}
              {step.questions.length > 0 && (
                <div className="rounded-lg border border-amber-200 dark:border-amber-800 bg-amber-50 dark:bg-amber-900/20 p-3">
                  <div className="flex items-start gap-2">
                    <AlertCircle size={14} className="text-amber-600 dark:text-amber-400 flex-shrink-0 mt-0.5"/>
                    <div>
                      <div className="text-[11px] font-bold uppercase tracking-widest text-amber-800 dark:text-amber-300 mb-1">Need clarification</div>
                      <ul className="text-xs text-amber-900 dark:text-amber-200 space-y-1 leading-relaxed list-disc list-inside">
                        {step.questions.map((q, i) => <li key={i}>{q}</li>)}
                      </ul>
                    </div>
                  </div>
                </div>
              )}
            </div>
          )}
        </div>
      ))}

      {/* Add custom step */}
      <button onClick={addCustom}
        className="w-full rounded-2xl border-2 border-dashed border-slate-300 dark:border-slate-700 text-slate-500 dark:text-slate-400 hover:border-blue-400 hover:text-blue-600 py-5 text-sm font-bold transition-colors inline-flex items-center justify-center gap-2">
        <Plus size={14}/> Add custom step
      </button>

      {/* Bottom action bar */}
      <div className="sticky bottom-0 bg-white dark:bg-slate-900 border-t border-slate-200 dark:border-slate-800 -mx-4 sm:-mx-6 px-4 sm:px-6 py-4 flex items-center justify-between">
        <div className="text-xs text-slate-500 dark:text-slate-400">
          {hasContent ? `${steps.filter(s => s.body.trim()).length} step(s) filled in` : 'Start by filling in at least one step'}
        </div>
        <button onClick={() => setPreviewMode(true)}
          disabled={!hasContent || !name.trim()}
          className="px-5 py-2.5 rounded-xl bg-blue-600 hover:bg-blue-700 disabled:opacity-50 text-white text-sm font-bold inline-flex items-center gap-2">
          Review & Save <ChevronRight size={14}/>
        </button>
      </div>

      {/* Note about chart annotations */}
      <div className="rounded-xl border border-slate-200 dark:border-slate-700 bg-slate-50 dark:bg-slate-900/50 p-4">
        <div className="text-[11px] font-bold uppercase tracking-widest text-slate-500 dark:text-slate-400 mb-1.5">Coming soon</div>
        <p className="text-xs text-slate-600 dark:text-slate-300 leading-relaxed">
          Interactive chart annotations — if your strategy needs you to mark out a specific range, draw fib levels, or point at a precise zone, we'll ship a chart-annotation tool that lets you draw the setup and pin it to the step. Until then, describe the rule as precisely as you can in text and the parser will surface any ambiguity for clarification.
        </p>
      </div>
    </div>
  )
}
