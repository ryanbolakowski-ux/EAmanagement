"""Power of 3 (PO3) - Accumulation / Manipulation / Distribution at London.

A thin :class:`AMDCore` wrapper with the proposal SS3.4 anchors (defaults
LOCKED where the proposal said "USER TO CONFIRM"):

  * **Accumulation** = the **Asian range 20:00-00:00 ET** (SS3.4.4.i default:
    "Asian 20:00-00:00").
  * **Manipulation + entry killzone** = **London** 02:00-05:00 ET (SS3.4.8
    default: "London primary"). The sweep of the Asian range must occur in
    London, and the distribution entry fires in London.
  * **Mode** = ``reversal`` - PO3's distribution runs OPPOSITE the manipulation
    sweep (low sweep -> bullish distribution / LONG; high sweep -> bearish /
    SHORT).
  * **Stop** = 2 ticks beyond the manipulation extreme (the swept Asian-range
    high/low) (SS3.4.6).
  * **Target** = the opposite extreme of the accumulation range / next pool;
    **min RR 3.0** (SS3.4.7 seed, configurable via ``risk_reward_ratio``).
  * **Max trades/day** = **1** (SS3.4.9 - one distribution per day).

Registered under the seed name "Power of 3 (PO3)" and the ``rule_tree``
``ict_setup == "po3"`` id (both normalize through the registry). Every other
strategy keeps falling back to the generic model.
"""
from __future__ import annotations

from app.engines.ict.registry import register
from app.engines.ict.setups.amd_core import AMDCore


# Resolvable via BOTH the seed name and the short rule_tree id. Stacking the
# decorator (bottom-up; topmost wins for ``cls.name``) registers both keys:
#   get_setup("Power of 3 (PO3)")              -> this setup
#   get_setup("x", {"ict_setup": "po3"})       -> this setup
@register("po3")
@register("Power of 3 (PO3)")
class PowerOfThree(AMDCore):
    """PO3: Asian-range accumulation, London manipulation + distribution."""

    variant = "po3"
    # Accumulation = Asian 20:00-00:00 ET (SS3.4.4.i default).
    accumulation_session = ("20:00", "00:00")
    # Manipulation + entry both in the London killzone (SS3.4.8 default).
    manipulation_killzone = ("LONDON",)
    entry_killzone = ("LONDON",)
    mode = "reversal"
    trap_window_min = None
    max_trades_day = 1          # SS3.4.9: one distribution/day.
    min_rr = 3.0                # SS3.4.7 seed.
