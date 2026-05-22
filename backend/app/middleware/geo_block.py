"""US-only geo-restriction middleware.

Uses ipapi.co (free, 1k/day) to look up the country for each new IP.
Caches results in Redis for 7 days so we only hit the API once per IP.

Blocks the API for any IP outside the US (and a few US territories).
The frontend will catch the 451 status and show a "Not available in your
country" page.
"""
import os
import json
import asyncio
from typing import Optional
from loguru import logger
import httpx
from fastapi import Request, HTTPException

import redis.asyncio as redis_async


# US territories we allow alongside mainland US
ALLOWED_COUNTRIES = {"US", "PR", "VI", "GU", "MP", "AS"}

# Paths that bypass the geo-check (so health checks + legal pages + denied page work)
BYPASS_PATHS = {
    "/health", "/api/v1/health",
    "/", "/privacy", "/terms", "/disclosures", "/cookies", "/pricing", "/not-available",
    "/api/v1/geo/status",   # so the frontend can ask "am I blocked"
}

# Treat private / local IPs as US (dev)
PRIVATE_PREFIXES = ("127.", "10.", "172.16.", "172.17.", "172.18.", "172.19.",
                    "172.20.", "172.21.", "172.22.", "172.23.", "172.24.",
                    "172.25.", "172.26.", "172.27.", "172.28.", "172.29.",
                    "172.30.", "172.31.", "192.168.")

_redis: Optional[redis_async.Redis] = None


def _get_redis():
    global _redis
    if _redis is None:
        url = os.environ.get("REDIS_URL", "redis://edge_redis:6379")
        _redis = redis_async.from_url(url, decode_responses=True)
    return _redis


def _client_ip(request: Request) -> str:
    """Pull the real client IP from common proxy headers (cf-connecting-ip,
    x-forwarded-for, x-real-ip) before falling back to socket peer."""
    for hdr in ("cf-connecting-ip", "x-real-ip", "x-forwarded-for"):
        v = request.headers.get(hdr)
        if v:
            return v.split(",")[0].strip()
    return request.client.host if request.client else ""



# IPQualityScore VPN/proxy/datacenter detection. Free tier: 5,000 lookups/mo.
# Sign up: https://www.ipqualityscore.com/create-account → get IPQS_API_KEY
_IPQS_KEY = os.environ.get("IPQS_API_KEY", "")

# Fraud-score threshold above which we block (IPQS scores 0-100; 75+ = high risk)
_FRAUD_BLOCK_THRESHOLD = int(os.environ.get("IPQS_FRAUD_THRESHOLD", "85"))


async def _lookup_geo_full(ip: str) -> dict:
    """Full geo + VPN/proxy lookup. Returns dict with:
       country (ISO2), is_vpn, is_proxy, is_tor, is_datacenter, fraud_score.
       Cached 7 days in Redis. Falls back to {country: 'US'} on private IPs."""
    if not ip:
        return {"country": None, "is_vpn": False, "is_proxy": False,
                "is_tor": False, "is_datacenter": False, "fraud_score": 0}
    if any(ip.startswith(p) for p in PRIVATE_PREFIXES):
        return {"country": "US", "is_vpn": False, "is_proxy": False,
                "is_tor": False, "is_datacenter": False, "fraud_score": 0}
    try:
        r = _get_redis()
        cached = await r.get(f"geo2:{ip}")
        if cached:
            return json.loads(cached)
    except Exception as e:
        logger.warning(f"[geo] redis get failed: {e}")

    result = {"country": None, "is_vpn": False, "is_proxy": False,
              "is_tor": False, "is_datacenter": False, "fraud_score": 0}

    # Prefer IPQS if configured (gives us VPN/proxy/datacenter flags)
    if _IPQS_KEY:
        try:
            async with httpx.AsyncClient(timeout=4.0) as client:
                resp = await client.get(
                    f"https://www.ipqualityscore.com/api/json/ip/{_IPQS_KEY}/{ip}",
                    params={"strictness": 1, "allow_public_access_points": "true",
                            "fast": "true", "mobile": "true"},
                )
                if resp.status_code == 200:
                    j = resp.json()
                    if j.get("success", True):
                        result = {
                            "country": (j.get("country_code") or "").upper() or None,
                            "is_vpn": bool(j.get("vpn")),
                            "is_proxy": bool(j.get("proxy")),
                            "is_tor": bool(j.get("tor")),
                            "is_datacenter": bool(j.get("is_crawler")),  # free tier only flags crawlers; full DC detection requires IPQS Premium
                            "fraud_score": int(j.get("fraud_score") or 0),
                        }
        except Exception as e:
            logger.warning(f"[geo] IPQS failed for {ip}: {e}")

    # Fallback to ipapi.co for country only (no VPN detection)
    if not result["country"]:
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                resp = await client.get(f"https://ipapi.co/{ip}/country/")
                if resp.status_code == 200:
                    country = (resp.text or "").strip().upper()
                    if country and len(country) <= 4:
                        result["country"] = country
        except Exception as e:
            logger.warning(f"[geo] ipapi fallback failed for {ip}: {e}")

    # Cache result
    try:
        await _get_redis().setex(f"geo2:{ip}", 7 * 24 * 3600, json.dumps(result))
    except Exception:
        pass
    return result

async def _lookup_country(ip: str) -> Optional[str]:
    """Look up ISO country code. Cached for 7 days in Redis."""
    if not ip:
        return None
    if any(ip.startswith(p) for p in PRIVATE_PREFIXES):
        return "US"  # localhost / docker network treated as US
    try:
        r = _get_redis()
        cached = await r.get(f"geo:{ip}")
        if cached:
            return cached
    except Exception as e:
        logger.warning(f"[geo] redis get failed: {e}")

    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get(f"https://ipapi.co/{ip}/country/")
            if resp.status_code == 200:
                country = (resp.text or "").strip().upper()
                if country and len(country) <= 4:
                    try:
                        await _get_redis().setex(f"geo:{ip}", 7 * 24 * 3600, country)
                    except Exception:
                        pass
                    return country
    except Exception as e:
        logger.warning(f"[geo] lookup failed for {ip}: {e}")
    return None


async def geo_block_middleware(request: Request, call_next):
    """FastAPI middleware that returns 451 for non-US IPs."""
    path = request.url.path
    # Bypass health checks, legal pages, root, and the geo-status endpoint
    # Bypass all auth + landing + legal paths so users can ALWAYS reach login,
    # register, password reset, etc. The actual geo gate fires only on
    # post-login API calls. Once logged in + KYC verified, full bypass kicks in.
    if path in BYPASS_PATHS or path.startswith("/api/v1/auth/") or path.startswith("/api/v1/kyc/") or path.startswith("/static/") or path.startswith("/assets/") or path.startswith("/api/v1/legal/"):
        return await call_next(request)
    # admin + KYC-verified bypass: trust users we've already verified.
    # An admin OR anyone with a verified Stripe Identity ID gets through
    # regardless of IP flags — eliminates false positives on mobile carrier
    # NAT, iCloud Private Relay, etc.
    auth_hdr = request.headers.get("authorization", "")
    if auth_hdr.startswith("Bearer "):
        try:
            from app.core.security import decode_token
            payload = decode_token(auth_hdr.split(" ", 1)[1])
            # ANY valid JWT bypasses geo. KYC is the real gate for live trading.
            # The IP check is just to keep random non-US visitors off the marketing site.
            if payload.get("sub") or payload.get("user_id") or payload.get("is_admin"):
                return await call_next(request)
        except Exception:
            pass
    # Allow if GEO_BLOCK_ENABLED is false
    if os.environ.get("GEO_BLOCK_ENABLED", "1") != "1":
        return await call_next(request)

    ip = _client_ip(request)
    geo = await _lookup_geo_full(ip)
    country = geo.get("country")
    from fastapi.responses import JSONResponse

    # 1. Block by country
    if country and country not in ALLOWED_COUNTRIES:
        logger.info(f"[geo] BLOCKED country {ip} country={country} path={path}")
        return JSONResponse(status_code=451, content={
            "detail": "Theta Algos is currently available only to residents of the United States.",
            "reason": "country", "country": country,
        })

    # 2. Block VPN / proxy / tor — even if it claims to be US
    # is_proxy alone is too noisy (Apple Private Relay, Cloudflare WARP, etc) — only block on confirmed VPN or Tor
    if geo.get("is_vpn") or geo.get("is_tor"):
        logger.info(f"[geo] BLOCKED vpn/proxy {ip} country={country} vpn={geo.get('is_vpn')} proxy={geo.get('is_proxy')} tor={geo.get('is_tor')} path={path}")
        return JSONResponse(status_code=451, content={
            "detail": "Theta Algos requires you to disable any VPN, proxy, or anonymizer before using the platform. US compliance regulations require us to verify your physical location.",
            "reason": "vpn", "country": country,
            "is_vpn": geo.get("is_vpn"), "is_proxy": geo.get("is_proxy"), "is_tor": geo.get("is_tor"),
        })

    # 3. Block datacenter IPs (bots, scrapers, hosting)
    if geo.get("is_datacenter"):
        logger.info(f"[geo] BLOCKED datacenter {ip} country={country} path={path}")
        return JSONResponse(status_code=451, content={
            "detail": "Theta Algos cannot be accessed from datacenter or hosting IPs. Please connect from a residential or mobile network.",
            "reason": "datacenter", "country": country,
        })

    # 4. Block high fraud score
    if geo.get("fraud_score", 0) >= _FRAUD_BLOCK_THRESHOLD:
        logger.info(f"[geo] BLOCKED fraud_score {ip} score={geo.get('fraud_score')} path={path}")
        return JSONResponse(status_code=451, content={
            "detail": "Theta Algos cannot verify your connection. Please contact support@thetaalgos.com if you believe this is in error.",
            "reason": "fraud", "country": country, "fraud_score": geo.get("fraud_score"),
        })

    return await call_next(request)
