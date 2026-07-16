"""
core/notifications.py

Email notifications for the Eko Recon App.
- send_eod_summary(): daily EOD digest per partner
- send_match_rate_alert(): fires when match rate drops below threshold

All email settings come from environment variables.
If SMTP_HOST is not set, notifications are silently skipped (no error raised).
"""

import os
import smtplib
import logging
import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

logger = logging.getLogger("eko_recon.notifications")

# ─── Config from env ──────────────────────────────────────────────────────────
SMTP_HOST      = os.getenv("SMTP_HOST", "")
SMTP_PORT      = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER      = os.getenv("SMTP_USER", "")
SMTP_PASS      = os.getenv("SMTP_PASS", "")
SMTP_FROM      = os.getenv("SMTP_FROM", "recon@eko.co.in")
EOD_EMAIL_TO   = [e.strip() for e in os.getenv("EOD_EMAIL_TO", "").split(",") if e.strip()]
ALERT_THRESHOLD = float(os.getenv("MATCH_RATE_ALERT_THRESHOLD", "85"))


def _send(subject: str, html_body: str, to: list):
    """Send an email. Silently skips if SMTP is not configured."""
    if not SMTP_HOST or not to:
        logger.info(f"[notifications] Email skipped (SMTP not configured): {subject}")
        return
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = SMTP_FROM
        msg["To"]      = ", ".join(to)
        msg.attach(MIMEText(html_body, "html"))

        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as server:
            server.ehlo()
            server.starttls()
            if SMTP_USER:
                server.login(SMTP_USER, SMTP_PASS)
            server.sendmail(SMTP_FROM, to, msg.as_string())

        logger.info(f"[notifications] Sent '{subject}' to {to}")
    except Exception as e:
        logger.error(f"[notifications] Failed to send '{subject}': {e}")


def send_eod_summary(db):
    """
    Build and send the daily EOD reconciliation summary.
    Called by the scheduler at the configured hour (default 20:00 IST).
    Summarises today's recon activity across all partners.
    """
    if not EOD_EMAIL_TO:
        return

    today = str(datetime.date.today())

    # Per-PRODUCT stats for today, deduped across ALL products (core ledger + E-Value +
    # BBPS + SBI Kiosk + AePS/QR) — the same aggregator the analytics dashboard uses, so
    # the digest now covers every product, not just the core ledger (audit #21).
    from core.analytics import build_analytics
    a = build_analytics(db, today, today)
    tot = a.get("totals", {})
    overall_total     = tot.get("transactions", 0)
    overall_matched   = tot.get("matched", 0)
    overall_unmatched = tot.get("unmatched", 0) + tot.get("mismatch", 0)
    overall_rate      = tot.get("match_rate", 0)

    if not overall_total:
        logger.info("[notifications] EOD email skipped — no transactions for today")
        return

    rate_color = "#16a34a" if overall_rate >= 95 else "#d97706" if overall_rate >= 85 else "#dc2626"

    # Build per-product rows (one per product group)
    partner_rows_html = ""
    for g in a.get("by_group", []):
        total   = g.get("transactions", 0)
        matched = g.get("matched", 0)
        openn   = g.get("unmatched", 0) + g.get("mismatch", 0)
        rate    = g.get("match_rate", 0)
        flag = " ⚠️" if rate < ALERT_THRESHOLD else ""
        rc   = "#dc2626" if rate < ALERT_THRESHOLD else "#16a34a" if rate >= 95 else "#d97706"
        partner_rows_html += f"""
        <tr>
          <td style="padding:8px 12px;border-bottom:1px solid #f3f4f6;font-weight:600">{g.get('label','')}</td>
          <td style="padding:8px 12px;border-bottom:1px solid #f3f4f6;text-align:center">{total:,}</td>
          <td style="padding:8px 12px;border-bottom:1px solid #f3f4f6;text-align:center;color:#16a34a;font-weight:600">{matched:,}</td>
          <td style="padding:8px 12px;border-bottom:1px solid #f3f4f6;text-align:center;color:#dc2626">{openn:,}</td>
          <td style="padding:8px 12px;border-bottom:1px solid #f3f4f6;text-align:center;color:{rc};font-weight:700">{rate}%{flag}</td>
        </tr>"""

    html = f"""
    <html><body style="font-family:sans-serif;color:#1f2937;max-width:600px;margin:auto;padding:24px">
      <div style="background:#1e3a5f;color:white;padding:20px 24px;border-radius:12px 12px 0 0">
        <div style="font-size:11px;opacity:0.7;text-transform:uppercase;letter-spacing:1px">Eko Bharat Ventures</div>
        <div style="font-size:20px;font-weight:700;margin-top:4px">Daily Recon Summary — {today}</div>
      </div>

      <div style="background:#f9fafb;padding:20px 24px;display:flex;gap:24px">
        <div style="flex:1;background:white;border-radius:8px;padding:16px;text-align:center;border:1px solid #e5e7eb">
          <div style="font-size:28px;font-weight:700;color:#1e3a5f">{overall_total:,}</div>
          <div style="font-size:11px;color:#6b7280;text-transform:uppercase;margin-top:4px">Total Transactions</div>
        </div>
        <div style="flex:1;background:white;border-radius:8px;padding:16px;text-align:center;border:1px solid #e5e7eb">
          <div style="font-size:28px;font-weight:700;color:#16a34a">{overall_matched:,}</div>
          <div style="font-size:11px;color:#6b7280;text-transform:uppercase;margin-top:4px">Matched</div>
        </div>
        <div style="flex:1;background:white;border-radius:8px;padding:16px;text-align:center;border:1px solid #e5e7eb">
          <div style="font-size:28px;font-weight:700;color:#dc2626">{overall_unmatched:,}</div>
          <div style="font-size:11px;color:#6b7280;text-transform:uppercase;margin-top:4px">Open Items</div>
        </div>
        <div style="flex:1;background:white;border-radius:8px;padding:16px;text-align:center;border:1px solid #e5e7eb">
          <div style="font-size:28px;font-weight:700;color:{rate_color}">{overall_rate}%</div>
          <div style="font-size:11px;color:#6b7280;text-transform:uppercase;margin-top:4px">Match Rate</div>
        </div>
      </div>

      <table style="width:100%;border-collapse:collapse;background:white;border:1px solid #e5e7eb;border-top:none">
        <thead>
          <tr style="background:#f3f4f6;font-size:11px;text-transform:uppercase;color:#6b7280">
            <th style="padding:8px 12px;text-align:left">Product</th>
            <th style="padding:8px 12px;text-align:center">Total</th>
            <th style="padding:8px 12px;text-align:center">Matched</th>
            <th style="padding:8px 12px;text-align:center">Open</th>
            <th style="padding:8px 12px;text-align:center">Rate</th>
          </tr>
        </thead>
        <tbody>{partner_rows_html}</tbody>
      </table>

      {'<div style="background:#fef2f2;border:1px solid #fecaca;border-radius:8px;padding:12px 16px;margin-top:16px;color:#991b1b;font-size:13px">⚠️ <strong>Alert:</strong> One or more partners have match rate below ' + str(int(ALERT_THRESHOLD)) + '%. Please review open items immediately.</div>' if overall_rate < ALERT_THRESHOLD else ''}

      <div style="margin-top:16px;font-size:11px;color:#9ca3af;text-align:center">
        Eko Bharat Ventures — Recon App · Auto-generated at {datetime.datetime.now().strftime('%H:%M IST')}
      </div>
    </body></html>"""

    alert_flag = " ⚠️ ACTION REQUIRED" if overall_rate < ALERT_THRESHOLD else ""
    _send(
        subject=f"Recon EOD {today} — {overall_rate}% match rate{alert_flag}",
        html_body=html,
        to=EOD_EMAIL_TO,
    )


def send_match_rate_alert(partner: str, recon_date: str, match_rate: float, matched: int, unmatched: int):
    """
    Send an immediate alert when a recon run produces a match rate below threshold.
    Called after any auto or manual recon run.
    """
    if not EOD_EMAIL_TO or match_rate >= ALERT_THRESHOLD:
        return

    html = f"""
    <html><body style="font-family:sans-serif;color:#1f2937;max-width:500px;margin:auto;padding:24px">
      <div style="background:#dc2626;color:white;padding:16px 20px;border-radius:8px 8px 0 0">
        <strong>⚠️ Low Match Rate Alert — Eko Recon</strong>
      </div>
      <div style="background:#fef2f2;border:1px solid #fecaca;padding:20px;border-radius:0 0 8px 8px">
        <table style="width:100%;font-size:14px">
          <tr><td style="color:#6b7280;padding:4px 0">Partner:</td><td style="font-weight:700;text-transform:capitalize">{partner}</td></tr>
          <tr><td style="color:#6b7280;padding:4px 0">Date:</td><td style="font-weight:700">{recon_date}</td></tr>
          <tr><td style="color:#6b7280;padding:4px 0">Match Rate:</td><td style="font-weight:700;color:#dc2626;font-size:20px">{match_rate}%</td></tr>
          <tr><td style="color:#6b7280;padding:4px 0">Matched:</td><td style="color:#16a34a;font-weight:600">{matched}</td></tr>
          <tr><td style="color:#6b7280;padding:4px 0">Open Items:</td><td style="color:#dc2626;font-weight:600">{unmatched}</td></tr>
        </table>
        <div style="margin-top:16px;font-size:13px;color:#7f1d1d">
          This is below the {int(ALERT_THRESHOLD)}% alert threshold. Common causes:<br>
          • Internal dump not yet uploaded for this date<br>
          • Bank statement covers different transactions than the dump<br>
          • Format detection issue at upload<br><br>
          <strong>Action:</strong> Check Open Items in the Recon App → filter by {partner} / {recon_date}.
        </div>
      </div>
    </body></html>"""

    _send(
        subject=f"⚠️ LOW MATCH RATE: {partner} {recon_date} — only {match_rate}%",
        html_body=html,
        to=EOD_EMAIL_TO,
    )
