import os
import stripe
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel

from app.database import get_db
from app.models.user import User, SubscriptionTier
from app.core.auth import get_current_user
from loguru import logger

router = APIRouter()

stripe.api_key = os.getenv("STRIPE_SECRET_KEY", "")
FRONTEND_URL = os.getenv("FRONTEND_URL", "https://thetaalgos.com")
WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")

# Stripe Price IDs — created via the API on startup if missing.
# Free Trial (tier_1) is $0 so no Stripe product needed.
THETA_LOGO_URL = f"{FRONTEND_URL}/theta-logo.png"

TIER_PRICES = {
    "tier_2": {
        "name":   "Tier 2 (Futures Signals)",
        "amount": 4900,
        "desc":   ("ICT signals on ES/NQ/RTY/YM for prop-firm accounts "
                    "(Apex, TPT, Topstep). Manual execution inside your prop "
                    "rules. Paper trading + backtesting included."),
    },
    "tier_3": {
        "name":   "Tier 3 (Options Scanner)",
        "amount": 9900,
        "desc":   ("Full 3,000+ ticker pre-market scanner. Daily 1+4 email "
                    "at 8:30 ET with Low-Float Squeeze, 52-Week Breakout, "
                    "Pre-Market Gap, Oracle, and Momentum picks. Manual "
                    "execution."),
    },
    "tier_4": {
        "name":   "Tier 4 (Options Live)",
        "amount": 19900,
        "desc":   ("Same scanner as Tier 3 plus Tradier broker integration. "
                    "One-click confirm places real orders with live greeks "
                    "and real bid/ask. Most popular plan."),
    },
    "tier_5": {
        "name":   "Tier 5 (Fully Automated)",
        "amount": 39900,
        "desc":   ("Zero clicks. The bot scans, picks, sizes, places, "
                    "manages, and exits — automatically. Multi-strategy "
                    "concurrent including the Wheel. Priority + chat support."),
    },
}

_price_ids: dict[str, str] = {}

# Tier a subscription is downgraded to when Stripe reports it
# deleted/cancelled. NOTE: the SubscriptionTier enum has no `FREE`
# member -- the previous `SubscriptionTier.FREE` raised AttributeError
# -> 500 on every cancellation/refund webhook (cancelled users kept
# paid access; sustained 500s can make Stripe auto-disable the
# endpoint). FREE_TRIAL is the real lowest/free tier.
CANCELLED_TIER = SubscriptionTier.FREE_TRIAL


async def ensure_stripe_products():
    """Create or fetch Stripe products/prices on startup."""
    global _price_ids
    if _price_ids:
        return
    try:
        products = stripe.Product.list(limit=100)
        existing = {p.name: p.id for p in products.data}

        for tier_key, info in TIER_PRICES.items():
            prod_name = f"Theta Algos - {info['name']}"
            if prod_name in existing:
                prod_id = existing[prod_name]
                # Ensure image + description stay fresh on every restart
                try:
                    stripe.Product.modify(prod_id,
                        description=info.get("desc"),
                        images=[THETA_LOGO_URL],
                    )
                except Exception:
                    pass
            else:
                prod = stripe.Product.create(
                    name=prod_name,
                    description=info.get("desc"),
                    images=[THETA_LOGO_URL],
                    metadata={"tier_key": tier_key, "platform": "theta_algos"},
                )
                prod_id = prod.id

            # Check for existing price
            prices = stripe.Price.list(product=prod_id, active=True, limit=10)
            matching = [p for p in prices.data if p.unit_amount == info["amount"] and p.recurring and p.recurring.interval == "month"]
            if matching:
                _price_ids[tier_key] = matching[0].id
            else:
                price = stripe.Price.create(
                    product=prod_id,
                    unit_amount=info["amount"],
                    currency="usd",
                    recurring={"interval": "month"},
                )
                _price_ids[tier_key] = price.id

        print(f"Stripe products ready: {_price_ids}")
    except Exception as e:
        print(f"Stripe setup error: {e}")


class CheckoutRequest(BaseModel):
    tier: str


@router.post("/create-checkout")
async def create_checkout_session(
    req: CheckoutRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await ensure_stripe_products()

    if req.tier not in _price_ids:
        raise HTTPException(status_code=400, detail=f"Invalid tier: {req.tier}")

    try:
        session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            mode="subscription",
            line_items=[{"price": _price_ids[req.tier], "quantity": 1}],
            success_url=f"{FRONTEND_URL}/app/profile?payment=success",
            cancel_url=f"{FRONTEND_URL}/app/profile?payment=cancelled",
            client_reference_id=str(current_user.id),
            customer_email=current_user.email,
            metadata={"user_id": str(current_user.id), "tier": req.tier},
            # Also stamp the tier onto the SUBSCRIPTION (not just the checkout
            # session). session.metadata never reaches the subscription object,
            # so customer.subscription.updated could not read it. This lands
            # metadata.tier on the sub as a fallback; the primary re-sync path
            # in that webhook resolves the tier from the price ID (below),
            # since a Portal plan change swaps the price but leaves metadata
            # untouched.
            subscription_data={
                "metadata": {"user_id": str(current_user.id), "tier": req.tier}
            },
        )
        return {"checkout_url": session.url}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/webhook")
async def stripe_webhook(request: Request, db: AsyncSession = Depends(get_db)):
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature", "")

    # Bug #7 fix: fail closed when webhook secret is missing. Previously
    # the code parsed the body unverified, letting anyone who could reach
    # the public webhook URL forge "checkout.session.completed" events
    # and upgrade arbitrary accounts.
    if not WEBHOOK_SECRET:
        raise HTTPException(
            status_code=503,
            detail="Webhook secret not configured",
        )
    try:
        event = stripe.Webhook.construct_event(payload, sig_header, WEBHOOK_SECRET)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid signature")

    if event.get("type") == "checkout.session.completed":
        session_data = event["data"]["object"]
        user_id = session_data.get("metadata", {}).get("user_id")
        tier = session_data.get("metadata", {}).get("tier")
        subscription_id = session_data.get("subscription")

        if user_id and tier:
            result = await db.execute(select(User).where(User.id == user_id))
            user = result.scalar_one_or_none()
            if user:
                tier_map = {"tier_2": SubscriptionTier.TIER_2, "tier_3": SubscriptionTier.TIER_3, "tier_4": SubscriptionTier.TIER_4, "tier_5": SubscriptionTier.TIER_5}
                if tier in tier_map:
                    user.subscription_tier = tier_map[tier]
                    user.stripe_subscription_id = subscription_id
                    await db.commit()
                    # 2FA gate: a paid subscription just started. If the user
                    # has not enrolled TOTP yet, gated routes will return
                    # 403 detail.code='requires_2fa_setup' starting now.
                    if not user.totp_enabled:
                        logger.info(
                            f"[stripe] 2FA-required for user={user.email} "
                            f"tier={tier} totp_enabled=False at subscription start"
                        )

    elif event.get("type") == "customer.subscription.updated":
        sub = event["data"]["object"]
        sub_id = sub.get("id")
        tier_map = {"tier_2": SubscriptionTier.TIER_2, "tier_3": SubscriptionTier.TIER_3, "tier_4": SubscriptionTier.TIER_4, "tier_5": SubscriptionTier.TIER_5}
        # Resolve the new tier PRIMARILY from the subscription's PRICE ID. A
        # Portal plan change swaps the price on the subscription but leaves
        # custom metadata untouched, so metadata.tier alone can never reflect a
        # Portal upgrade/downgrade. Map price_id -> tier_key via _price_ids
        # (populated on startup / first checkout; refresh here if empty).
        new_tier_key = None
        try:
            items = (sub.get("items") or {}).get("data") or []
            price_id = (items[0].get("price") or {}).get("id") if items else None
            if price_id:
                if not _price_ids:
                    await ensure_stripe_products()
                price_to_tier = {pid: tk for tk, pid in _price_ids.items()}
                new_tier_key = price_to_tier.get(price_id)
        except Exception as e:  # never let resolution crash the webhook
            logger.warning(f"[stripe] price->tier resolution failed for sub={sub_id}: {e}")
            new_tier_key = None
        # Fallback to subscription metadata.tier (now stamped at checkout via
        # subscription_data) if the price lookup didn't resolve.
        if not new_tier_key:
            new_tier_key = (sub.get("metadata") or {}).get("tier")
        if sub_id and new_tier_key in tier_map:
            result = await db.execute(select(User).where(User.stripe_subscription_id == sub_id))
            user = result.scalar_one_or_none()
            if user:
                user.subscription_tier = tier_map[new_tier_key]
                await db.commit()

    elif event.get("type") == "customer.subscription.deleted":
        sub = event["data"]["object"]
        sub_id = sub.get("id")
        if sub_id:
            result = await db.execute(select(User).where(User.stripe_subscription_id == sub_id))
            user = result.scalar_one_or_none()
            if user:
                user.subscription_tier = CANCELLED_TIER
                user.stripe_subscription_id = None
                await db.commit()

    return {"status": "ok"}


@router.post("/cancel")
async def cancel_subscription(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    # 2026-08-26: free-trial / comped users have NO Stripe subscription, so the
    # old hard-400 made cancellation IMPOSSIBLE for them. Cancel locally — end
    # the trial now (the trial-expiry gate then removes access) and drop a comped
    # paid tier to the free floor. Cancellation must ALWAYS succeed.
    if not current_user.stripe_subscription_id:
        from datetime import datetime as _dt, timezone as _tz
        current_user.subscription_tier = CANCELLED_TIER
        current_user.trial_ends_at = _dt.now(_tz.utc)
        await db.commit()
        return {"message": "Your subscription has been cancelled."}

    try:
        stripe.Subscription.modify(
            current_user.stripe_subscription_id,
            cancel_at_period_end=True,
        )
        return {"message": "Subscription will cancel at end of billing period"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/portal")
async def customer_portal(
    current_user: User = Depends(get_current_user),
):
    """Redirect to Stripe Customer Portal for managing subscription."""
    try:
        customers = stripe.Customer.list(email=current_user.email, limit=1)
        if not customers.data:
            raise HTTPException(status_code=400, detail="You're on a free trial with no paid billing account to manage. Use Cancel to end your trial.")

        session = stripe.billing_portal.Session.create(
            customer=customers.data[0].id,
            return_url=f"{FRONTEND_URL}/app/profile",
        )
        return {"portal_url": session.url}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
