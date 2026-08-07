"""Saro 1.3 predictive-consolidation scanner — SHADOW ONLY (2026-08-07).

Find the coil BEFORE the move: the opposite of Saro 1.2's momentum chase.
Parallel forward-test track next to Saro 1.0 and Saro 1.2. This package
NEVER sends emails, NEVER queues paper/live entries, and NEVER imports
pick_router / emit_theta_pick / broker order paths. Its only side effects
are shadow rows in email_signals_history (shadow=true,
source='saro13_shadow', matched_strategy prefixed 'saro13:',
instrument_type='watch_only'), nightly ATM-IV rows in saro_iv_history, and
per-day redis caches. Execution/exits are OUT OF SCOPE this round — signal
quality first (house doctrine); see shadow.py for the next-phase note.

This __init__ intentionally imports nothing so the package import graph
stays inert until a submodule is explicitly imported.
"""
