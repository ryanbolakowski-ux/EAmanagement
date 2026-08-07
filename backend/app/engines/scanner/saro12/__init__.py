"""Saro 1.2 low-float squeeze scanner — SHADOW ONLY (2026-08-06).

Parallel forward-test track next to Saro 1.0. This package NEVER sends
emails, NEVER queues paper/live entries, and NEVER imports pick_router /
emit_theta_pick / broker order paths. Its only side effect is shadow rows
in email_signals_history (shadow=true, source='saro12_shadow',
matched_strategy prefixed 'saro12:', instrument_type='watch_only') plus
per-day redis caches. Sections 4-5 of the owner spec (execution + exits)
are OUT OF SCOPE this round — signal quality first (house doctrine after
the June/July incidents); see shadow.py for the next-phase note.

This __init__ intentionally imports nothing so the package import graph
stays inert until a submodule is explicitly imported.
"""
