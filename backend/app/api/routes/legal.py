"""Legal acknowledgments — every time a user clicks 'I agree' to a disclaimer
this records who/what/when/where so we have a paper trail."""
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.user import User
from app.core.auth import get_current_user

router = APIRouter()

# Versions get bumped whenever the underlying T&C text changes. The recorded
# acknowledgment freezes the version the user accepted, so we can prove what
# they saw at the time even if we later edit the document.
CURRENT_VERSIONS = {
    "terms_of_service":         "v1",
    "risk_disclosure":          "v1",
    "live_trading_consent":     "v1",
    "options_trading_consent":  "v1",
    "signals_disclosure":       "v2",
    "fully_automated_trading":  "v1",
    "risk_change":              "v1",
}

# Disclosure documents shown to the user when they click "I agree". Each
# document carries its own version that travels with the recorded acknowledgment
# so we can prove the user accepted *this exact text* even after the policy is
# updated. Keep these terse — long EULA walls of text don't hold up better than
# short clear ones, and they bury the parts a court actually cares about.
DOCUMENTS = {
    "terms_of_service": {
        "title": "Terms of Service",
        "version": "v1",
        "html": """
<p>By using the Theta Algos platform (the <strong>"Service"</strong>) you agree to these Terms. The Service is operated by <strong>Theta Algos LLC</strong> ("we", "us"). You must be at least 18 years old and legally permitted to trade derivatives in your jurisdiction.</p>
<p>You agree not to (a) reverse engineer or resell access to the Service, (b) use the Service in connection with any unlawful activity, or (c) share your account credentials with any third party. We may suspend or terminate access at any time for any reason. The Service is provided <strong>"as is"</strong> without warranty of any kind, and your sole remedy for dissatisfaction is to stop using it.</p>
<p>You acknowledge that algorithmic trading carries a substantial risk of loss, that we do not provide investment advice, and that any decision to deploy capital is yours alone. To the maximum extent permitted by law, our aggregate liability to you for any claim arising from your use of the Service is limited to the fees you have paid us in the twelve months preceding the claim.</p>
""",
    },
    "risk_disclosure": {
        "title": "Risk Disclosure",
        "version": "v1",
        "html": """
<p><strong>Trading futures, options, and other leveraged derivative products involves substantial risk of loss and is not suitable for every investor.</strong> The high degree of leverage that is often obtainable in these markets can work against you as well as for you. You may sustain a total loss of the funds that you deposit with your broker to establish or maintain a position, and you may be required to deposit additional funds. <em>You may lose more than your initial deposit.</em></p>
<p>You acknowledge that (i) past performance, simulated performance, and backtest results are not indicative of future results, (ii) market conditions, liquidity, slippage, technology failures, and force-majeure events can cause real results to differ materially from any backtest or simulation, (iii) no trading system, including this one, eliminates the risk of loss, and (iv) you are solely responsible for evaluating whether any strategy is appropriate for your financial situation, risk tolerance, and objectives.</p>
<p>Theta Algos LLC is not a registered investment adviser, broker-dealer, commodity trading advisor, or commodity pool operator. Nothing on the Service constitutes investment advice or a recommendation to buy, sell, or hold any financial instrument.</p>
""",
    },
    "live_trading_consent": {
        "title": "Live Trading Consent",
        "version": "v1",
        "html": """
<p>You are about to enable <strong>live trading</strong>, which authorizes the Service to place real orders against real funds in a brokerage account you control. You confirm and agree that:</p>
<ol>
  <li>You have read and understood the <strong>Risk Disclosure</strong> and accept that trading futures and other derivatives may result in losses that exceed your initial deposit.</li>
  <li>You are the lawful owner of the brokerage account being connected, you have authority to authorize trades in it, and you have configured your daily loss limit, maximum contracts, and kill switch to levels you can afford to lose.</li>
  <li>Theta Algos LLC will execute trades according to the strategy's logic without further confirmation from you. Latency, slippage, partial fills, broker outages, and adverse market moves can cause realized outcomes to differ — sometimes substantially — from simulated or paper-trading outcomes.</li>
  <li>You will monitor your account and you accept that the kill switch, daily-loss cap, and other risk controls are best-effort safeguards, not guarantees. They may fail to fire during a fast move, an outage, or a code defect.</li>
  <li>You release Theta Algos LLC, its officers, employees, contractors, and affiliates from any claim arising from losses sustained while live trading is enabled, except where such losses are caused by our gross negligence or willful misconduct.</li>
</ol>
<p>If you do not accept every clause above, do <strong>not</strong> click "I agree" and do not enable live trading.</p>
""",
    },
    "options_trading_consent": {
        "title": "Options Trading Consent",
        "version": "v1",
        "html": """
<p>You are about to deploy a strategy that trades <strong>listed options</strong>. Options have characteristics distinct from futures and equities, and you confirm that you understand:</p>
<ol>
  <li><strong>Time decay (theta).</strong> Options lose value as expiration approaches even when the underlying is unchanged. A "correct" directional view can still produce a loss if the move is too slow.</li>
  <li><strong>Volatility risk (vega).</strong> A drop in implied volatility can reduce the premium of a long option even when the underlying moves in your favor.</li>
  <li><strong>Total-loss risk.</strong> A long option can expire worthless. A short option (uncovered) has theoretically unlimited loss potential. Spread strategies cap maximum loss only if all legs fill and remain intact through expiration.</li>
  <li><strong>Assignment & exercise risk.</strong> American-style options may be exercised early, particularly around ex-dividend dates or near expiration, which can result in unexpected stock or cash positions.</li>
  <li><strong>Liquidity & slippage.</strong> Options often trade with wider bid–ask spreads than the underlying and may be illiquid intraday, causing real entry and exit prices to differ from displayed mid prices.</li>
  <li><strong>Approval level.</strong> You confirm your brokerage account is approved for the option strategy levels (long calls/puts, spreads, naked, etc.) the strategy will deploy. Trades that exceed your approved level will be rejected by your broker.</li>
</ol>
<p>You accept all option-specific risks in addition to the general Risk Disclosure, and you agree that Theta Algos LLC is not responsible for losses arising from options-specific factors (theta, vega, assignment, exercise, liquidity, approval-level mismatch, or otherwise).</p>
""",
    },
    "signals_disclosure": {
        "title": "Signal-Only Mode Disclosure",
        "version": "v2",
        "html": """
<p>The <strong>Account Signals</strong> feature emits notifications (email, push, in-app) describing positions the Theta Algos algorithm is taking in its own proprietary book. These notifications are intended for prop-firm accounts and other contexts in which automated trading is prohibited or impractical.</p>
<p>Signal-only notifications are <strong>not</strong> investment advice, a recommendation, an endorsement, a solicitation, or an offer to buy, sell, or hold any financial instrument. Whether you replicate any portion of a signal in your own account is entirely your decision and your responsibility. Past or hypothetical performance shown anywhere in the Service is not indicative of future results. You may lose money — possibly more than your initial deposit — and Theta Algos LLC is not liable for losses arising from your use of, or reliance on, any signal.</p>
<p>Theta Algos LLC is <strong>not managing your account</strong>. Account management &mdash; where the system places, manages, and closes trades on your behalf &mdash; applies <strong>only</strong> if you are on the fully automated package and have separately accepted the <em>Fully Automated Trading Agreement</em>. Unless you are on that package and have accepted that agreement, every trade decision is yours to make and to execute.</p>
<p>Trade ideas and signals are provided for <strong>educational and informational purposes only</strong> unless you are separately authorized for automated or assisted execution. Trading involves risk and you may lose money; <strong>no profits are guaranteed</strong>.</p>
""",
    },
    "fully_automated_trading": {
        "title": "Fully Automated Trading Agreement",
        "version": "v1",
        "html": """
<p>You are enabling <strong>fully automated trading</strong>. This authorizes Theta Algos LLC ("we", "us") to <strong>automatically place, manage, and close trades</strong> in a brokerage account you control, according to the strategies and risk settings you have selected, <strong>without asking you to approve each individual trade</strong>. By accepting, you confirm and agree that:</p>
<ol>
  <li><strong>Automated execution.</strong> Once automation is enabled, the system may enter, manage, and exit positions on your behalf with no further manual approval. It may place <strong>entry orders, stop-loss orders, take-profit orders, trailing stops, break-even stop adjustments, and closing orders</strong> as the selected strategy logic and your configured risk settings dictate.</li>
  <li><strong>Risk of loss.</strong> Trading involves substantial risk and losses are possible, including losses that may exceed your initial deposit. Past, simulated, and backtested performance is not indicative of future results.</li>
  <li><strong>Your responsibilities.</strong> You are solely responsible for your brokerage account, your connected brokerage credentials, your risk settings (including maximum allocation, maximum risk, daily-loss limits, and contract/position limits), and for enabling or disabling automation. You confirm you are the lawful owner of the connected brokerage account and are authorized to place trades in it.</li>
  <li><strong>No guarantee of profit.</strong> Theta Algos LLC does not guarantee any profit or any particular trading result.</li>
  <li><strong>Not financial advice.</strong> Theta Algos LLC is not a registered investment adviser or broker-dealer, and nothing in the Service constitutes investment advice or a recommendation to buy, sell, or hold any instrument. The decision to enable automation is yours alone.</li>
  <li><strong>You can turn it off.</strong> You may disable automation at any time from your account. Disabling automation stops new automated entries; you remain responsible for monitoring, managing, or closing any positions that are already open.</li>
</ol>
<p>If you do not accept every clause above, do <strong>not</strong> accept this agreement and do not enable fully automated trading.</p>
""",
    },
}


def get_document(kind: str) -> Optional[dict]:
    """Return {title, version, html} for a kind, or None if unknown."""
    return DOCUMENTS.get(kind)


async def has_current_ack(db: AsyncSession, user_id: str, kind: str) -> bool:
    """True if the user has accepted the current version of `kind`."""
    ver = CURRENT_VERSIONS.get(kind)
    if not ver:
        return False
    r = await db.execute(text("""
        SELECT 1 FROM user_acknowledgments
         WHERE user_id = :uid AND kind = :kind AND content_version = :ver
         LIMIT 1
    """), {"uid": str(user_id), "kind": kind, "ver": ver})
    return r.fetchone() is not None


async def require_current_ack(db: AsyncSession, user_id: str, kind: str):
    """Raise HTTP 403 if the user hasn't accepted the current version of `kind`.
    Server-side enforcement so a missing ack can't be bypassed by hitting the
    API directly."""
    if not await has_current_ack(db, user_id, kind):
        ver = CURRENT_VERSIONS.get(kind, "v1")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"acknowledgment_required:{kind}:{ver}",
        )


class AcknowledgmentCreate(BaseModel):
    kind: str
    detail: Optional[str] = None


class AcknowledgmentResponse(BaseModel):
    id: str
    kind: str
    content_version: str
    detail: Optional[str]
    agreed_at: str
    ip_address: Optional[str]


@router.post("/acknowledge", response_model=AcknowledgmentResponse, status_code=status.HTTP_201_CREATED)
async def record_acknowledgment(
    data: AcknowledgmentCreate,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    version = CURRENT_VERSIONS.get(data.kind, "v1")
    aid = uuid.uuid4()
    ip = request.client.host if request.client else None
    ua = request.headers.get("user-agent")
    now = datetime.now(timezone.utc)
    await db.execute(text("""
        INSERT INTO user_acknowledgments
            (id, user_id, kind, content_version, detail, ip_address, user_agent, agreed_at)
        VALUES
            (:id, :uid, :kind, :ver, :detail, :ip, :ua, :now)
    """), {
        "id": str(aid), "uid": str(current_user.id),
        "kind": data.kind, "ver": version, "detail": data.detail,
        "ip": ip, "ua": ua, "now": now,
    })
    await db.commit()
    return AcknowledgmentResponse(
        id=str(aid), kind=data.kind, content_version=version,
        detail=data.detail, agreed_at=now.isoformat(), ip_address=ip,
    )


@router.get("/acknowledgments", response_model=list[AcknowledgmentResponse])
async def list_my_acknowledgments(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    rows = await db.execute(text("""
        SELECT id, kind, content_version, detail, ip_address, agreed_at
          FROM user_acknowledgments
         WHERE user_id = :uid
         ORDER BY agreed_at DESC
         LIMIT 200
    """), {"uid": str(current_user.id)})
    return [
        AcknowledgmentResponse(
            id=str(r.id), kind=r.kind, content_version=r.content_version,
            detail=r.detail, ip_address=r.ip_address,
            agreed_at=r.agreed_at.isoformat() if r.agreed_at else "",
        )
        for r in rows.fetchall()
    ]


# Admin: see acknowledgments across all users
@router.get("/documents/{kind}")
async def get_disclosure_document(kind: str):
    """Return the rendered HTML for a disclosure document. Used by the
    frontend modal so the user sees the same text we have on file."""
    doc = get_document(kind)
    if not doc:
        raise HTTPException(status_code=404, detail="Unknown document")
    return {
        "kind": kind,
        "title": doc["title"],
        "version": doc["version"],
        "html": doc["html"].strip(),
    }


@router.get("/status")
async def get_my_ack_status(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return which current-version acks the user has, plus which they're
    missing. Frontend uses this to know whether to show the modal before
    enabling live trading / options."""
    r = await db.execute(text("""
        SELECT kind, content_version
          FROM user_acknowledgments
         WHERE user_id = :uid
        ORDER BY agreed_at DESC
    """), {"uid": str(current_user.id)})
    have: dict[str, set] = {}
    for row in r.fetchall():
        have.setdefault(row.kind, set()).add(row.content_version)

    status_map = {}
    for kind, current_ver in CURRENT_VERSIONS.items():
        status_map[kind] = {
            "current_version": current_ver,
            "accepted": current_ver in have.get(kind, set()),
        }
    return {"acknowledgments": status_map}


@router.get("/admin/acknowledgments")
async def admin_list_acknowledgments(
    user_id: Optional[str] = None,
    kind: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    # Admin gate — only tier_5 can hit this
    tier = current_user.subscription_tier.value if hasattr(current_user.subscription_tier, "value") else str(current_user.subscription_tier)
    if tier != "tier_5":
        raise HTTPException(status_code=403, detail="Admin only.")
    where = []
    params = {}
    if user_id:
        where.append("a.user_id = :uid")
        params["uid"] = user_id
    if kind:
        where.append("a.kind = :kind")
        params["kind"] = kind
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    rows = await db.execute(text(f"""
        SELECT a.id, a.user_id, u.email, u.username, a.kind, a.content_version,
               a.detail, a.ip_address, a.agreed_at
          FROM user_acknowledgments a
          JOIN users u ON u.id = a.user_id
          {where_sql}
         ORDER BY a.agreed_at DESC
         LIMIT 500
    """), params)
    return [
        {
            "id": str(r.id), "user_id": str(r.user_id),
            "email": r.email, "username": r.username,
            "kind": r.kind, "content_version": r.content_version,
            "detail": r.detail, "ip_address": r.ip_address,
            "agreed_at": r.agreed_at.isoformat() if r.agreed_at else None,
        }
        for r in rows.fetchall()
    ]
