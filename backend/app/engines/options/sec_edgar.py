"""SEC EDGAR 8-K filing feed — free, real-time, no API key.

8-K filings are the 'material event' disclosure form every public US
company must file within ~4 business days of:
  • Earnings release (Item 2.02)
  • Material agreement / contract (Items 1.01, 1.02)
  • M&A activity (Item 2.01)
  • Going-concern / financial distress (Item 2.04, 2.06)
  • Exec changes (Item 5.02)
  • FDA actions, lawsuits, regulatory events (Item 8.01)

The EDGAR Atom feed at /cgi-bin/browse-edgar gives us a real-time list
of every 8-K filed in the last 1-2 hours. We poll it every 5 minutes
and cache the ticker + item codes in a small DB table — when the
scanner sees a momentum hit, it can confirm there's a *real* catalyst
behind the move (not just a random pump).

User-Agent header is required by SEC — they ban traffic without one.
Rate limit: 10 req/sec; we stay well under at 1 every 5 min.
"""
import asyncio
import re
import json as _j
from datetime import datetime, timedelta, timezone
from typing import Optional
from xml.etree import ElementTree as ET
from loguru import logger

import httpx
from sqlalchemy import text

from app.database import async_session_factory


EDGAR_ATOM = (
    "https://www.sec.gov/cgi-bin/browse-edgar"
    "?action=getcurrent&type=8-K&company=&dateb=&owner=include&count=100&output=atom"
)
USER_AGENT = "Theta Algos LLC research@thetaalgos.com"
ATOM_NS = "{http://www.w3.org/2005/Atom}"


# Item-code → human-readable map. We tag which ones are "high-impact"
# catalysts (worth amplifying scanner hits).
ITEM_TAGS = {
    "1.01": ("material_agreement", True),
    "1.02": ("agreement_termination", True),
    "2.01": ("acquisition_or_disposition", True),
    "2.02": ("earnings_results", True),
    "2.03": ("debt_obligation", False),
    "2.04": ("triggering_event_debt", True),
    "2.06": ("material_impairment", True),
    "3.01": ("delisting_notice", True),
    "3.02": ("unregistered_sale_dilution", True),
    "3.03": ("material_modification_security", False),
    "5.02": ("officer_change", True),
    "5.03": ("amended_bylaws", False),
    "7.01": ("regulation_fd", False),
    "8.01": ("other_events", True),   # often FDA, lawsuits, partnerships
    "9.01": ("financial_statements", False),
}


async def init_table():
    """Create the cache table if it doesn't already exist."""
    async with async_session_factory() as db:
        await db.execute(text("""
            CREATE TABLE IF NOT EXISTS edgar_filings (
                id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
                accession_no    VARCHAR(40) NOT NULL UNIQUE,
                ticker          VARCHAR(20),
                cik             VARCHAR(20),
                company_name    VARCHAR(200),
                form_type       VARCHAR(20),
                filed_at        TIMESTAMPTZ NOT NULL,
                item_codes      JSONB DEFAULT '[]'::jsonb,
                item_tags       JSONB DEFAULT '[]'::jsonb,
                is_high_impact  BOOLEAN NOT NULL DEFAULT false,
                url             TEXT,
                ingested_at     TIMESTAMPTZ NOT NULL DEFAULT now()
            );
        """))
        await db.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_edgar_ticker_time "
            "ON edgar_filings(ticker, filed_at DESC);"
        ))
        await db.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_edgar_high_impact "
            "ON edgar_filings(filed_at DESC) WHERE is_high_impact = true;"
        ))
        await db.commit()


# ── Atom feed parsing ──────────────────────────────────────────────────────

# Item codes appear in the entry summary like "Items 2.02, 9.01"
_ITEM_RE = re.compile(r"\bItem(?:s)?\s+([\d\.,\s]+)")

def _parse_items(summary: str) -> list[str]:
    if not summary:
        return []
    m = _ITEM_RE.search(summary)
    if not m:
        return []
    # Normalize "2.02, 9.01" → ["2.02", "9.01"]
    raw = m.group(1).replace(" ", "")
    return [p for p in raw.split(",") if re.match(r"^\d+\.\d+$", p)]


# Atom title looks like: "8-K - APPLE INC (0000320193) (Filer)"
_TICKER_RE = re.compile(r"\(([A-Z0-9\-\.]{1,8})\)", re.IGNORECASE)
_CIK_RE    = re.compile(r"\((\d{6,10})\)")


def _parse_entry(entry) -> Optional[dict]:
    title = (entry.findtext(f"{ATOM_NS}title") or "").strip()
    summary = (entry.findtext(f"{ATOM_NS}summary") or "").strip()
    link_el = entry.find(f"{ATOM_NS}link")
    url = link_el.get("href") if link_el is not None else None
    updated = entry.findtext(f"{ATOM_NS}updated")
    try:
        filed_at = datetime.fromisoformat(updated.replace("Z", "+00:00")) if updated else None
    except Exception:
        filed_at = None
    if not title or not url:
        return None

    cik_m = _CIK_RE.search(title)
    cik = cik_m.group(1) if cik_m else None

    # Company name is between "8-K - " and " (CIK)"
    company = title
    if " - " in title:
        company = title.split(" - ", 1)[1]
    if cik:
        company = company.replace(f"({cik})", "").strip()
    company = company.replace("(Filer)", "").strip(" -()")

    items = _parse_items(summary)
    tags = []
    is_high = False
    for code in items:
        t = ITEM_TAGS.get(code)
        if t:
            tags.append(t[0])
            is_high = is_high or t[1]

    # Accession number is in the URL: /Archives/edgar/data/<cik>/<accession>/0000-index.htm
    acc_m = re.search(r"(\d{10}-\d{2}-\d{6})", url or "")
    accession = acc_m.group(1) if acc_m else f"unk-{filed_at.isoformat() if filed_at else 'na'}-{cik or 'na'}"

    return {
        "accession_no": accession,
        "company_name": company,
        "cik": cik,
        "filed_at": filed_at,
        "form_type": "8-K",
        "item_codes": items,
        "item_tags": tags,
        "is_high_impact": is_high,
        "url": url,
    }


async def refresh_edgar() -> int:
    """Poll the EDGAR 8-K feed and persist any new filings. Returns the
    count of new rows inserted."""
    await init_table()
    try:
        async with httpx.AsyncClient(
            timeout=20, headers={"User-Agent": USER_AGENT}
        ) as c:
            r = await c.get(EDGAR_ATOM)
            r.raise_for_status()
            xml = r.text
    except Exception as e:
        logger.warning(f"[EDGAR] fetch failed: {e}")
        return 0

    try:
        root = ET.fromstring(xml)
    except Exception as e:
        logger.warning(f"[EDGAR] XML parse failed: {e}")
        return 0

    rows = []
    for entry in root.findall(f"{ATOM_NS}entry"):
        parsed = _parse_entry(entry)
        if parsed:
            rows.append(parsed)
    if not rows:
        return 0

    saved = 0
    async with async_session_factory() as db:
        for r in rows:
            try:
                await db.execute(text("""
                    INSERT INTO edgar_filings (
                        accession_no, ticker, cik, company_name, form_type,
                        filed_at, item_codes, item_tags, is_high_impact, url
                    )
                    VALUES (
                        :acc, NULL, :cik, :company, :form,
                        :filed, :items, :tags, :high, :url
                    )
                    ON CONFLICT (accession_no) DO NOTHING
                """), {
                    "acc": r["accession_no"], "cik": r["cik"],
                    "company": r["company_name"][:200], "form": r["form_type"],
                    "filed": r["filed_at"], "items": _j.dumps(r["item_codes"]),
                    "tags": _j.dumps(r["item_tags"]), "high": r["is_high_impact"],
                    "url": r["url"],
                })
                saved += 1
            except Exception as e:
                logger.warning(f"[EDGAR] insert failed for {r['accession_no']}: {e}")
        await db.commit()
    return saved


# ── Catalyst lookup helper for scanners ────────────────────────────────────

async def has_recent_high_impact_filing(ticker: str, lookback_hours: int = 48,
                                          company_name_hint: Optional[str] = None) -> Optional[dict]:
    """Return the most recent high-impact 8-K within the lookback window
    that mentions `ticker` (matched by company name when ticker isn't on
    the row — EDGAR feed doesn't include tickers directly)."""
    # We match by company name substring since EDGAR's Atom feed doesn't
    # carry the ticker. The scanner can pass the fundamentals-derived name.
    if not company_name_hint:
        return None
    cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
    name_pat = f"%{company_name_hint.upper().split()[0]}%"  # first word match
    async with async_session_factory() as db:
        r = (await db.execute(text("""
            SELECT accession_no, company_name, item_codes, item_tags, filed_at, url
              FROM edgar_filings
             WHERE is_high_impact = true
               AND filed_at >= :s
               AND upper(company_name) LIKE :pat
             ORDER BY filed_at DESC
             LIMIT 1
        """), {"s": cutoff, "pat": name_pat})).fetchone()
    if not r:
        return None
    return {
        "accession_no": r.accession_no,
        "company_name": r.company_name,
        "item_codes":   r.item_codes,
        "item_tags":    r.item_tags,
        "filed_at":     r.filed_at.isoformat(),
        "url":          r.url,
    }
