"""Saro STOCK-pick per-user activation (self-serve opt-in).

Backward-compat context
-----------------------
Historically the daily Saro STOCK pick email went to every user with an ACTIVE
`theta_scanner` strategy row (a manually-seeded set — jaceford12 + ryan.icloud
in prod). There was NO way for a new paying (tier_3+) user to turn Saro on for
themselves. This module introduces a per-user opt-in flag,
`users.saro_signals_enabled`, so eligible users can self-activate.

The migration GRANDFATHERS exactly the pre-existing recipient set so the LIVE
recipient set is byte-identical after deploy — nobody currently getting picks
is dropped. We deliberately do NOT blanket-default every tier_3+ user to true:
that would silently expand the active recipient set (2 -> ~8) and, because the
emit path also queues paper/live auto-entries, could trigger surprise emails
AND trades for users who never opted in. New tier_3+ users start NULL/false and
must self-activate via POST /api/v1/account-signals/saro/activate.

The column + backfill are applied lazily (idempotent ADD COLUMN IF NOT EXISTS +
guarded UPDATE), mirroring the existing `theta_scanner_allocation_usd` pattern,
so we never depend on an out-of-band migration step.
"""
from loguru import logger

# Once-per-process guard so we don't re-issue the ALTER/backfill on every call.
_saro_column_ensured = False


async def ensure_saro_column(db) -> None:
    """Idempotently ensure `users.saro_signals_enabled` exists and grandfather
    the CURRENT Saro stock-pick recipients to activated.

    Idempotent + re-runnable: the backfill only touches rows still NULL, so a
    container restart / redeploy NEVER re-activates a user who has since
    deactivated. Fail-open: on any error we roll back and log — the caller
    (recipient query / endpoint) then sees the column as absent and treats
    everyone as un-activated for that request rather than crashing.
    """
    global _saro_column_ensured
    if _saro_column_ensured:
        return
    from sqlalchemy import text as _t
    try:
        await db.execute(_t(
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS saro_signals_enabled boolean"
        ))
        # GRANDFATHER exactly the pre-existing recipient set (users with an
        # ACTIVE theta_scanner strategy). WHERE saro_signals_enabled IS NULL
        # keeps this idempotent — a redeploy can't re-activate a deactivated
        # user. is_active is intentionally NOT filtered here: the emit query
        # applies is_active, and grandfathering an inactive row is harmless.
        await db.execute(_t(
            "UPDATE users SET saro_signals_enabled = true "
            "WHERE saro_signals_enabled IS NULL "
            "AND id IN (SELECT DISTINCT user_id FROM strategies "
            "WHERE signal_mode = 'theta_scanner' AND status = 'ACTIVE')"
        ))
        await db.commit()
        _saro_column_ensured = True
    except Exception as e:  # pragma: no cover - defensive
        try:
            await db.rollback()
        except Exception:
            pass
        logger.warning(f"[saro] could not ensure saro_signals_enabled column: {e}")


# Shared SQL fragment: the recipient gate for the daily Saro STOCK pick.
# A user receives the pick iff they are ACTIVE, on a Saro-eligible tier
# (tier_3/4/5), and have opted in (saro_signals_enabled = true). COALESCE
# treats the new/absent flag as false so activation is strictly additive.
SARO_RECIPIENT_WHERE = (
    "u.is_active = true "
    "AND lower(u.subscription_tier) IN ('tier_3', 'tier_4', 'tier_5') "
    "AND COALESCE(u.saro_signals_enabled, false) = true"
)
