"""Judas Swing - the false move at the London open that reverses.

A thin :class:`AMDCore` wrapper with the proposal SS3.3 anchors (defaults
LOCKED where SS3.3 said "USER TO CONFIRM"):

  * **Accumulation** = the **Asian range 20:00-00:00 ET** (SS3.3.4.i / SS3.3.8
    default: "use 02:00 London open + Asian range as the reference").
  * **Manipulation** = the false move at the **London open**, captured by the
    ``LONDON_OPEN`` 02:00-03:00 ET killzone: the first push sweeps one side of
    the Asian range (the Judas leg).
  * **Distribution / entry** = the reversal: an MSS back through the swing that
    made the sweep, with displacement leaving an FVG, **within 60 min** of the
    open (SS3.3.4 default trap window). Entry fires in the London window.
  * **Mode** = ``reversal`` - trade OPPOSITE the false move (low swept ->
    LONG, high swept -> SHORT), i.e. the bias direction.
  * **Stop** = 2 ticks beyond the **Judas (manipulation) extreme** - the high
    of the false up-move for shorts, the low for longs (SS3.3.6).
  * **Target** = the opposite-side liquidity of the swept range; **min RR 3.0**
    (SS3.3.7 seed, clamp default; configurable via ``risk_reward_ratio``).
  * **Max trades/day** = **<= 2** (SS3.3.9 default cap 2).

Registered under the seed name "Judas Swing" and the ``rule_tree``
``ict_setup == "judas_swing"`` id. Every other strategy keeps falling back to
the generic model.
"""
from __future__ import annotations

from app.engines.ict.registry import register
from app.engines.ict.setups.amd_core import AMDCore


# Resolvable via BOTH the seed name and the short rule_tree id:
#   get_setup("Judas Swing")                        -> this setup
#   get_setup("x", {"ict_setup": "judas_swing"})    -> this setup
@register("judas_swing")
@register("Judas Swing")
class JudasSwing(AMDCore):
    """The London-open false move that sweeps the Asian range then reverses."""

    variant = "judas_swing"
    # Reference range = the Asian session (SS3.3.8 default).
    accumulation_session = ("20:00", "00:00")
    # The false move is anchored to the London OPEN (02:00-03:00 ET).
    manipulation_killzone = ("LONDON_OPEN",)
    # The reversal entry fires in the London window (02:00-05:00 ET); the trap
    # window below (60 min from the sweep) keeps it tight to the open.
    entry_killzone = ("LONDON",)
    mode = "reversal"
    # SS3.3.4 default: the sweep + MSS reversal must occur within 60 min.
    trap_window_min = 60
    max_trades_day = 2          # SS3.3.9 default cap 2.
    min_rr = 3.0                # SS3.3.7 seed.
