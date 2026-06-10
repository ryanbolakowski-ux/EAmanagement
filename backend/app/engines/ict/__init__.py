"""ICT strategy engine: a name->logic registry over shared ICT primitives.

This package is the foundation for the strategy rebuild (see
``/root/STRATEGY_PROPOSAL.md`` SS1, SS2, SS6). It is intentionally additive:

* ``context``  - the ``ICTContext`` object handed to each evaluator.
* ``base``     - the ``ICTSetup`` ABC + shared SL/TP/RR helpers.
* ``registry`` - ``register(name)`` decorator + ``get_setup(name, rule_tree)``.
* ``primitives`` - re-exported + new pure ICT primitives.

SAFETY PROPERTY: until a strategy is explicitly registered, ``get_setup``
returns ``None`` and the engine falls back to the existing generic
``ICTStrategy`` model, so behavior is unchanged for every strategy.
"""

from app.engines.ict.context import ICTContext
from app.engines.ict.base import ICTSetup
from app.engines.ict.registry import register, get_setup

__all__ = ["ICTContext", "ICTSetup", "register", "get_setup"]
