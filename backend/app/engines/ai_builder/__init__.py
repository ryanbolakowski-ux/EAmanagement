"""AI Strategy Builder V2 — the REAL backend compiler.

Turns a user's plain-English strategy description into the exact set of
knobs the trading engine actually supports (see schema.KNOWN_* for the
honest vocabulary), via a structured-output Anthropic call. Anything the
engine cannot express is reported in ``unsupported_concepts`` — never
silently dropped. This replaces the frontend-only regex "compiler" in
pages/AIStrategyBuilder.tsx, whose "same logic on the server" comment was
aspirational: no server-side generation path existed before this package.
"""
