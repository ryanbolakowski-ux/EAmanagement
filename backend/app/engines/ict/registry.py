"""Strategy registry: NAME -> ICTSetup, with a SAFE FALLBACK.

The single most important property here is the fallback. ``get_setup`` returns
``None`` for any name that has not been explicitly registered (ported). The
engine treats ``None`` as "use the existing generic ``ICTStrategy`` model", so
every strategy keeps behaving exactly as it does today until it is ported. This
makes a partial rollout safe: porting one strategy can never regress the
others or the working baseline.

Matching order (per proposal SS2):
  1. ``rule_tree["ict_setup"]`` - an explicit setup id, if present.
  2. the strategy ``name`` (case-insensitive).
A normalized form (lower-cased, spaces/hyphens -> underscores) is also tried so
seed names like ``"ICT Silver Bullet"`` can map to ``"silver_bullet"`` once a
setup registers under that key.
"""
from __future__ import annotations

from typing import Callable, Optional

from loguru import logger

from app.engines.ict.base import ICTSetup

#: name (already normalized) -> ICTSetup subclass.
_REGISTRY: dict[str, type[ICTSetup]] = {}


def _normalize(name: Optional[str]) -> str:
    """Lower-case, strip, and collapse spaces/hyphens to underscores."""
    if not name:
        return ""
    out = str(name).strip().lower()
    for ch in (" ", "-", "/"):
        out = out.replace(ch, "_")
    while "__" in out:
        out = out.replace("__", "_")
    return out.strip("_")


def register(name: str) -> Callable[[type[ICTSetup]], type[ICTSetup]]:
    """Class decorator registering an :class:`ICTSetup` under ``name``.

    ``name`` is normalized, so ``@register("Silver Bullet")`` and
    ``@register("silver_bullet")`` register the same key. Re-registering an
    existing key overwrites it (last definition wins) and logs a warning.
    """
    key = _normalize(name)

    def _decorator(cls: type[ICTSetup]) -> type[ICTSetup]:
        if not isinstance(cls, type) or not issubclass(cls, ICTSetup):
            raise TypeError(f"@register expects an ICTSetup subclass, got {cls!r}")
        if key in _REGISTRY and _REGISTRY[key] is not cls:
            logger.warning(f"[ict.registry] overwriting setup for '{key}' "
                           f"({_REGISTRY[key].__name__} -> {cls.__name__})")
        cls.name = key
        _REGISTRY[key] = cls
        return cls

    return _decorator


def get_setup(name: str, rule_tree: Optional[dict] = None) -> Optional[ICTSetup]:
    """Resolve a strategy to a dedicated :class:`ICTSetup` instance, or ``None``.

    Returns ``None`` when the name (and any ``rule_tree['ict_setup']`` override)
    is unknown or not yet ported - which is the signal for the engine to fall
    back to the existing generic model. Construction errors are swallowed and
    reported as ``None`` so a broken evaluator can never crash the watcher loop
    (fail-open, matching the engine's posture).
    """
    rt = rule_tree or {}

    # 1) explicit rule_tree override, then 2) the strategy name.
    candidates = []
    explicit = rt.get("ict_setup")
    if explicit:
        candidates.append(explicit)
    if name:
        candidates.append(name)

    cls: Optional[type[ICTSetup]] = None
    for cand in candidates:
        key = _normalize(cand)
        if key in _REGISTRY:
            cls = _REGISTRY[key]
            break

    if cls is None:
        return None

    try:
        return cls()
    except Exception as exc:  # never let a bad evaluator break dispatch
        logger.error(f"[ict.registry] failed to construct setup '{cls.__name__}' "
                     f"for name='{name}': {exc!r}; falling back to generic model")
        return None


def registered_names() -> list[str]:
    """Sorted list of registered setup keys (for diagnostics/tests)."""
    return sorted(_REGISTRY.keys())
