"""Pipeline failure alerts — loud, admin-only emails fired from any code
path that catches an exception in the email/scanner/signal pipeline.

The whole point: when a watcher / scheduler / send-path crashes, we want a
RED-FLAG email in the admin inbox within seconds — not a buried log line at
2am that nobody sees until the customer complains.

Design constraints:
  * Subject MUST start with '\U0001F6A8 URGENT' so:
      - the EMAIL_KILL_SWITCH whitelist lets it through ([Admin] keyword is
        also matched, but URGENT is the canonical brand here)
      - admins can mail-filter on it and surface as push / SMS
  * The alert dispatch itself MUST NEVER raise — every call site wraps it
    again in try/except so the alert can't crash the original error path.
  * Recipients default to every admin user (is_admin = TRUE). Override with
    the `recipients` kwarg for ad-hoc routing.
"""
from __future__ import annotations
from typing import Iterable, Mapping, Any
from datetime import datetime, timezone

from loguru import logger


async def _fetch_admin_emails() -> list[str]:
    """Return the email of every active admin user. Empty list on DB error."""
    try:
        from sqlalchemy import text as _t
        from app.database import async_session_factory
        async with async_session_factory() as db:
            rows = (await db.execute(_t(
                "SELECT email FROM users "
                "WHERE is_active = true AND is_admin = true"
            ))).fetchall()
        return [r[0] for r in rows if r and r[0]]
    except Exception as e:
        logger.error(f"[pipeline-alert] failed to fetch admin recipients: {e}")
        return []


def _html_body(reason: str, context: Mapping[str, Any] | None,
               traceback_str: str | None) -> str:
    """Render the alert body. Big red banner, then key/value of context,
    then traceback in a monospace block when provided."""
    ctx = context or {}
    rows_html = ""
    for k, v in ctx.items():
        # Truncate huge values so the email doesn't break Gmail's render.
        s = str(v)
        if len(s) > 4000:
            s = s[:4000] + "... [truncated]"
        rows_html += (
            f'<tr><td style="padding:6px 12px;color:#94a3b8;font-weight:700;'
            f'vertical-align:top;white-space:nowrap;">{k}</td>'
            f'<td style="padding:6px 12px;color:#0f172a;'
            f'word-break:break-word;"><code style="font-family:ui-monospace,'
            f'SFMono-Regular,Menlo,Monaco,Consolas,monospace;font-size:12px;">'
            f'{s}</code></td></tr>'
        )

    tb_block = ""
    if traceback_str:
        tb_block = (
            f'<div style="margin-top:20px;padding:14px;background:#0f172a;'
            f'color:#fecaca;border-radius:8px;overflow-x:auto;">'
            f'<div style="font-size:10px;letter-spacing:0.12em;'
            f'text-transform:uppercase;color:#f87171;font-weight:800;'
            f'margin-bottom:8px;">Traceback</div>'
            f'<pre style="margin:0;font-family:ui-monospace,SFMono-Regular,'
            f'Menlo,Monaco,Consolas,monospace;font-size:11px;line-height:1.45;'
            f'white-space:pre-wrap;word-break:break-word;">'
            f'{traceback_str}</pre></div>'
        )

    return f"""
    <div style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;
                max-width:680px;margin:0 auto;padding:20px;color:#0f172a;">
      <div style="background:#dc2626;color:#fff;padding:18px 22px;border-radius:12px;
                  margin-bottom:18px;">
        <div style="font-size:11px;letter-spacing:0.2em;text-transform:uppercase;
                    font-weight:900;opacity:0.85;">🚨 Pipeline failure</div>
        <div style="font-size:20px;font-weight:900;margin-top:4px;line-height:1.3;">
          {reason}
        </div>
        <div style="font-size:11px;opacity:0.75;margin-top:8px;">
          {datetime.now(timezone.utc).isoformat()}Z
        </div>
      </div>
      <table style="border-collapse:collapse;width:100%;font-size:13px;
                    border:1px solid #e2e8f0;border-radius:8px;overflow:hidden;">
        <tbody>{rows_html or '<tr><td style="padding:10px;color:#94a3b8;">no context provided</td></tr>'}</tbody>
      </table>
      {tb_block}
      <p style="margin:18px 0 0;color:#94a3b8;font-size:11px;line-height:1.55;">
        Auto-sent by pipeline_alerts to all admins. Never reply to this email
        directly — fix the underlying job, then dismiss the thread.
      </p>
    </div>
    """


async def send_pipeline_failure_alert(
    reason: str,
    context: Mapping[str, Any] | None = None,
    *,
    traceback_str: str | None = None,
    recipients: Iterable[str] | None = None,
) -> int:
    """Send the URGENT failure alert. Returns the count of admins emailed
    (0 if nothing could be sent — caller should NOT treat 0 as an error
    because we never raise).

    Subject deliberately starts with '\U0001F6A8 URGENT' so it:
      * passes the EMAIL_KILL_SWITCH whitelist (URGENT is in the transactional
        keyword set we added in the killswitch doc block)
      * sorts to the top of admin inboxes when they wake up
    """
    try:
        to_list = list(recipients) if recipients else await _fetch_admin_emails()
        if not to_list:
            logger.error(f"[pipeline-alert] NO ADMIN RECIPIENTS — reason={reason!r}")
            return 0
        from app.services.email import _send_tracked
        subject = f"🚨 URGENT · Pipeline failure · {reason[:80]}"
        html = _html_body(reason, context, traceback_str)
        sent = 0
        for addr in to_list:
            try:
                if _send_tracked(addr, subject, html).get("sent"):
                    sent += 1
            except Exception as e:
                logger.error(f"[pipeline-alert] send to {addr} crashed: {e}")
        logger.error(
            f"[pipeline-alert] sent to {sent}/{len(to_list)} admins: reason={reason!r} "
            f"context_keys={list((context or {}).keys())}"
        )
        return sent
    except Exception as e:
        # Last-resort guard: this MUST NEVER bubble up to the caller, which
        # is itself already inside an except: clause for some real error.
        logger.error(f"[pipeline-alert] dispatch itself crashed: {e}")
        return 0
