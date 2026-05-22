import os
import stripe
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel

from app.database import get_db
from app.models.user import User, SubscriptionTier
from app.core.auth import get_current_user

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

    elif event.get("type") == "customer.subscription.updated":
        sub = event["data"]["object"]
        sub_id = sub.get("id")
        # Stripe sends metadata on the subscription itself when changed via
        # Portal; the tier we synced at checkout still lives on user.subscription_tier
        # but the price may have changed. Re-sync if metadata.tier is present.
        new_tier_key = (sub.get("metadata") or {}).get("tier")
        if sub_id and new_tier_key:
            result = await db.execute(select(User).where(User.stripe_subscription_id == sub_id))
            user = result.scalar_one_or_none()
            tier_map = {"tier_2": SubscriptionTier.TIER_2, "tier_3": SubscriptionTier.TIER_3, "tier_4": SubscriptionTier.TIER_4, "tier_5": SubscriptionTier.TIER_5}
            if user and new_tier_key in tier_map:
                user.subscription_tier = tier_map[new_tier_key]
                await db.commit()

    elif event.get("type") == "customer.subscription.deleted":
        sub = event["data"]["object"]
        sub_id = sub.get("id")
        if sub_id:
            result = await db.execute(select(User).where(User.stripe_subscription_id == sub_id))
            user = result.scalar_one_or_none()
            if user:
                user.subscription_tier = SubscriptionTier.FREE
                user.stripe_subscription_id = None
                await db.commit()

    return {"status": "ok"}


@router.post("/cancel")
async def cancel_subscription(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if not current_user.stripe_subscription_id:
        raise HTTPException(status_code=400, detail="No active subscription")

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
            raise HTTPException(status_code=400, detail="No billing account found")

        session = stripe.billing_portal.Session.create(
            customer=customers.data[0].id,
            return_url=f"{FRONTEND_URL}/app/profile",
        )
        return {"portal_url": session.url}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
