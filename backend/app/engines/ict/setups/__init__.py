"""Per-strategy ICT setup evaluators.

Importing this package imports each setup module, whose ``@register(...)``
decorator wires the evaluator into the registry. Strategies that are NOT
imported here remain un-ported, so ``registry.get_setup`` returns ``None`` for
them and the engine keeps using the generic ``ICTStrategy`` fallback. That is
the safety property: porting one strategy can never affect the others.

Ported so far:
  * FVG Inversion Tap  (proposal SS3.8, build step 3) -> ``fvg_inversion_tap``
"""
from __future__ import annotations

# Side-effecting imports: each module self-registers via @register on import.
from app.engines.ict.setups import fvg_inversion_tap  # noqa: F401

__all__ = ["fvg_inversion_tap"]
