"""Geo status endpoint — used by the frontend to detect if user is blocked."""
from fastapi import APIRouter, Request
from app.middleware.geo_block import _client_ip, _lookup_geo_full, ALLOWED_COUNTRIES, _FRAUD_BLOCK_THRESHOLD

router = APIRouter()


@router.get("/status")
async def geo_status(request: Request):
    """Full geo + VPN/proxy status. Frontend uses this to render a friendly
    explanation on the /not-available page."""
    ip = _client_ip(request)
    geo = await _lookup_geo_full(ip)
    country = geo.get("country")
    allowed = (country in ALLOWED_COUNTRIES if country else True)               and not (geo.get("is_vpn") or geo.get("is_proxy") or geo.get("is_tor"))               and not geo.get("is_datacenter")               and geo.get("fraud_score", 0) < _FRAUD_BLOCK_THRESHOLD
    return {
        "ip": ip, "country": country, "allowed": allowed,
        "is_vpn": geo.get("is_vpn", False),
        "is_proxy": geo.get("is_proxy", False),
        "is_tor": geo.get("is_tor", False),
        "is_datacenter": geo.get("is_datacenter", False),
        "fraud_score": geo.get("fraud_score", 0),
    }
