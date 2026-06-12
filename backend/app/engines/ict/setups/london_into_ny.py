"""London Sweep into NY - London takes the Asian range, NY reverses it.

A thin :class:`AMDCore` wrapper with the proposal SS3.5 anchors (defaults
LOCKED where SS3.5 said "USER TO CONFIRM"):

  * **Accumulation** = the **Asian range 20:00-00:00 ET** (SS3.5.4.i).
  * **Manipulation** = **London** 02:00-05:00 ET sweeps a side of the Asian
    range (SS3.5.4.ii).
  * **Distribution / entry** = the **NY session 09:30-11:00 ET** reverses the
    London move: an NY MSS + displacement + FVG against London's push; entry at
    the NY-displacement FVG (SS3.5.4.iii / SS3.5.8 default NY window
    09:30-11:00). The trade window is NY, NOT London.
  * **Mode** = ``reversal`` (default, matching the name "London Sweep into NY";
    SS3.5.4: "default mode = reversal", continuation is a separate variant).
  * **Stop** = 2 ticks beyond the NY swing extreme the MSS broke (the
    London-sweep extreme when NY reverses right off it) (SS3.5.6).
  * **Target** = the opposite side of the Asian range / prior-day liquidity in
    the NY-move direction; **min RR 3.0** (SS3.5.7 seed; configurable via
    ``risk_reward_ratio``).
  * **Max trades/day** = **1** (SS3.5.9).

Registered under the seed name "London Sweep into NY" and the ``rule_tree``
``ict_setup == "london_into_ny"`` id. Every other strategy keeps falling back
to the generic model.
"""
from __future__ import annotations

from app.engines.ict.registry import register
from app.engines.ict.setups.amd_core import AMDCore


# Resolvable via BOTH the seed name and the short rule_tree id:
#   get_setup("London Sweep into NY")                  -> this setup
#   get_setup("x", {"ict_setup": "london_into_ny"})    -> this setup
@register("london_into_ny")
@register("London Sweep into NY")
class LondonSweepIntoNY(AMDCore):
    """London sweeps the Asian range; the NY session reverses it."""

    variant = "london_into_ny"
    # Accumulation = the Asian range (SS3.5.4.i).
    accumulation_session = ("20:00", "00:00")
    # Manipulation in London (02:00-05:00 ET) (SS3.5.4.ii).
    manipulation_killzone = ("LONDON",)
    # Entry ONLY in the NY session 09:30-11:00 ET (SS3.5.8 default). Uses
    # NY_AM (09:30-12:00) gated tighter is not needed: a custom NY window is
    # built here via the dedicated key below.
    entry_killzone = ("LONDON_INTO_NY_ENTRY",)
    mode = "reversal"           # SS3.5.4 default (reversal-only).
    trap_window_min = None
    max_trades_day = 1          # SS3.5.9.
    min_rr = 3.0                # SS3.5.7 seed.
