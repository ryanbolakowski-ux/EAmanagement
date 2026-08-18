"""Saro 1.3 predictive-consolidation scanner — SHADOW ONLY (2026-08-07).

Find the coil BEFORE the move: the opposite of Saro 1.2's momentum chase.
Parallel forward-test track next to Saro 1.0 and Saro 1.2. This package
NEVER queues paper/live entries and NEVER touches pick_router /
emit_theta_pick / broker order paths. The core modules also never send
emails — with ONE scoped exception (2026-08-18): sunday_watchlist.py, the
Sunday WATCHLIST cohort, sends a single killswitch-whitelisted watch-only
email (subject carries "Saro") to the SARO-activated recipient set, lazily
at send time, and writes rows with source='sunwatch' /
matched_strategy='saro13:sunday_watchlist'. It still queues nothing, ever
(enforced by tests/test_saro13.py on the core files and
tests/test_sunday_watchlist.py on the exception). Its only side effects
are shadow rows in email_signals_history (shadow=true,
source='saro13_shadow', matched_strategy prefixed 'saro13:',
instrument_type='watch_only'), nightly ATM-IV rows in saro_iv_history, and
per-day redis caches. Execution/exits are OUT OF SCOPE this round — signal
quality first (house doctrine); see shadow.py for the next-phase note.

This __init__ intentionally imports nothing so the package import graph
stays inert until a submodule is explicitly imported.
"""
