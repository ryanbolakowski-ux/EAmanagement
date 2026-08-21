"""Regression test for the Stripe `customer.subscription.deleted` webhook.

Bug: the handler assigned `SubscriptionTier.FREE`, but the enum has no `FREE`
member -> AttributeError -> 500 on every cancellation/refund webhook (cancelled
users kept paid access; sustained 500s can make Stripe auto-disable the
endpoint). This drives the real handler and asserts the downgrade path resolves
to a VALID enum member.

Run standalone (pytest may be absent):
    python tests/test_billing_deleted_webhook.py
"""
import asyncio

from app.models.user import SubscriptionTier
import app.api.routes.stripe_billing as sb


class _Result:
    def __init__(self, user):
        self._user = user

    def scalar_one_or_none(self):
        return self._user


class _FakeDB:
    def __init__(self, user):
        self._user = user
        self.committed = False

    async def execute(self, *a, **k):
        return _Result(self._user)

    async def commit(self):
        self.committed = True


class _FakeUser:
    def __init__(self):
        self.subscription_tier = SubscriptionTier.TIER_4
        self.stripe_subscription_id = "sub_123"
        self.totp_enabled = True


class _FakeRequest:
    def __init__(self):
        self.headers = {"stripe-signature": "sig"}

    async def body(self):
        return b"{}"


class _StubWebhook:
    @staticmethod
    def construct_event(payload, sig, secret):
        return {
            "type": "customer.subscription.deleted",
            "data": {"object": {"id": "sub_123"}},
        }


def test_deleted_webhook_resolves_valid_tier():
    user = _FakeUser()
    db = _FakeDB(user)

    prev_secret = sb.WEBHOOK_SECRET
    prev_webhook = sb.stripe.Webhook
    sb.WEBHOOK_SECRET = "whsec_test"
    sb.stripe.Webhook = _StubWebhook
    try:
        resp = asyncio.run(sb.stripe_webhook(_FakeRequest(), db))
    finally:
        sb.WEBHOOK_SECRET = prev_secret
        sb.stripe.Webhook = prev_webhook

    assert resp == {"status": "ok"}
    assert db.committed is True
    # Core assertion: the downgrade target is a REAL enum member (would have
    # raised AttributeError before the fix), and it is the free tier.
    assert user.subscription_tier in list(SubscriptionTier)
    assert isinstance(user.subscription_tier, SubscriptionTier)
    assert user.subscription_tier == SubscriptionTier.FREE_TRIAL
    assert user.stripe_subscription_id is None


def test_subscriptiontier_has_no_free_member_and_constant_is_valid():
    # Documents the original defect and guards the module-level target.
    assert not hasattr(SubscriptionTier, "FREE")
    assert sb.CANCELLED_TIER in list(SubscriptionTier)
    assert sb.CANCELLED_TIER == SubscriptionTier.FREE_TRIAL


if __name__ == "__main__":
    test_deleted_webhook_resolves_valid_tier()
    test_subscriptiontier_has_no_free_member_and_constant_is_valid()
    print("OK")
