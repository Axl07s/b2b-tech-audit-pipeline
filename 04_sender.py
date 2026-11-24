"""
04_sender.py — SMTP dispatcher with follow-up thread management.

States handled:
  DRAFTED   → sends day-1 email  → SENT_1
  SENT_1    → if 3 days passed   → sends day-3 follow-up  → SENT_2
  SENT_2    → if 7 days passed   → sends day-7 farewell   → SENT_3

Flags:
  --dry-run   (default: reads DRY_RUN from .env, default true)
  --live      forces live mode regardless of .env

Usage:
    python 04_sender.py              # dry-run (safe)
    python 04_sender.py --live       # real send
"""
from __future__ import annotations

import argparse
import os
import random
import smtplib
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from email.utils import formatdate, make_msgid

from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from db import count_sent_today, fetch_by_state, init_db, json_loads_safe, log_send, set_state
from logger_setup import get_logger

load_dotenv()
log = get_logger("04_sender")
console = Console()

SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
MAX_DAILY = int(os.getenv("MAX_DAILY_EMAILS", "25"))
DELAY_MIN = int(os.getenv("SEND_DELAY_MIN", "300"))
DELAY_MAX = int(os.getenv("SEND_DELAY_MAX", "720"))
FOLLOWUP_DAY2 = int(os.getenv("FOLLOWUP_DAY2", "3"))
FOLLOWUP_DAY3 = int(os.getenv("FOLLOWUP_DAY3", "7"))

_ENV_DRY_RUN = os.getenv("DRY_RUN", "true").strip().lower() in ("true", "1", "yes")


# ── SMTP helpers ──────────────────────────────────────────────────────────────

def build_message(
    from_addr: str,
    to_addr: str,
    subject: str,
    body: str,
    in_reply_to: str | None = None,
    references: str | None = None,
) -> tuple[MIMEText, str]:
    """Returns (MIMEText message, generated Message-ID)."""
    msg = MIMEText(body, "plain", "utf-8")
    msg_id = make_msgid(domain=from_addr.split("@")[-1])
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = msg_id
    # Thread headers for follow-ups
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = references
    return msg, msg_id


def send_smtp(msg: MIMEText, from_addr: str, to_addr: str) -> None:
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.ehlo()
        server.starttls()
        server.login(SMTP_USER, SMTP_PASSWORD)
        server.sendmail(from_addr, [to_addr], msg.as_string())


def print_dry_run(place_id: str, name: str, to_addr: str, subject: str, body: str, attempt: int) -> None:
    console.print(
        Panel(
            f"[bold yellow]DRY RUN — Attempt #{attempt}[/bold yellow]\n"
            f"[cyan]To:[/cyan]      {to_addr}\n"
            f"[cyan]Subject:[/cyan] {subject}\n\n"
            f"{body}",
            title=f"[green]{name}[/green] ({place_id})",
            border_style="yellow",
        )
    )


# ── Follow-up timing ──────────────────────────────────────────────────────────

def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _days_since(dt: datetime | None) -> float:
    if dt is None:
        return 0.0
    now = datetime.now(tz=timezone.utc)
    return (now - dt).total_seconds() / 86400


# ── Main dispatch ─────────────────────────────────────────────────────────────

def dispatch_business(row: sqlite3.Row, dry_run: bool) -> bool:
    """
    Try to send the right email for this business.
    Returns True if an email was dispatched (or would have been in dry-run).
    """
    place_id = row["place_id"]
    name = row["display_name"] or place_id
    state = row["state"]
    emails = json_loads_safe(row["found_emails"])
    subject = row["subject"] or "Quick question"

    if not emails:
        log.warning("  %s has no emails — skipping", name)
        set_state(place_id, "DISCARDED", {"audit_notes": "No emails at send time"})
        return False

    to_addr = emails[0]  # always use the first validated email

    # ── DRAFTED → SENT_1 ──────────────────────────────────────────────────────
    if state == "DRAFTED":
        body = row["body_day1"] or ""
        if not body:
            log.warning("  %s has no body_day1 — skipping", name)
            return False
        msg, msg_id = build_message(SMTP_USER, to_addr, subject, body)
        if dry_run:
            print_dry_run(place_id, name, to_addr, subject, body, 1)
        else:
            send_smtp(msg, SMTP_USER, to_addr)
        set_state(place_id, "SENT_1", {
            "sent_at_1": datetime.now(tz=timezone.utc).isoformat(),
            "message_id_1": msg_id,
        })
        log.info("  → SENT_1 to %s%s", to_addr, " [DRY RUN]" if dry_run else "")
        return True

    # ── SENT_1 → SENT_2 (after FOLLOWUP_DAY2 days) ───────────────────────────
    if state == "SENT_1":
        sent_at = _parse_dt(row["sent_at_1"])
        if _days_since(sent_at) < FOLLOWUP_DAY2:
            log.debug("  %s not yet %d days since SENT_1 — skip", name, FOLLOWUP_DAY2)
            return False
        body = row["body_day2"] or ""
        if not body:
            log.warning("  %s has no body_day2 — skipping follow-up", name)
            return False
        followup_subject = f"Re: {subject}"
        msg_id_1 = row["message_id_1"] or ""
        msg, msg_id = build_message(
            SMTP_USER, to_addr, followup_subject, body,
            in_reply_to=msg_id_1, references=msg_id_1
        )
        if dry_run:
            print_dry_run(place_id, name, to_addr, followup_subject, body, 2)
        else:
            send_smtp(msg, SMTP_USER, to_addr)
        set_state(place_id, "SENT_2", {
            "sent_at_2": datetime.now(tz=timezone.utc).isoformat(),
            "message_id_2": msg_id,
        })
        log.info("  → SENT_2 to %s%s", to_addr, " [DRY RUN]" if dry_run else "")
        return True

    # ── SENT_2 → SENT_3 (after FOLLOWUP_DAY3 days) ───────────────────────────
    if state == "SENT_2":
        sent_at = _parse_dt(row["sent_at_2"])
        if _days_since(sent_at) < FOLLOWUP_DAY3:
            log.debug("  %s not yet %d days since SENT_2 — skip", name, FOLLOWUP_DAY3)
            return False
        body = row["body_day7"] or ""
        if not body:
            log.warning("  %s has no body_day7 — skipping farewell", name)
            return False
        farewell_subject = f"Re: {subject}"
        refs = " ".join(filter(None, [row["message_id_1"], row["message_id_2"]]))
        msg, msg_id = build_message(
            SMTP_USER, to_addr, farewell_subject, body,
            in_reply_to=row["message_id_2"] or "",
            references=refs or None,
        )
        if dry_run:
            print_dry_run(place_id, name, to_addr, farewell_subject, body, 3)
        else:
            send_smtp(msg, SMTP_USER, to_addr)
        set_state(place_id, "SENT_3", {
            "sent_at_3": datetime.now(tz=timezone.utc).isoformat(),
            "message_id_3": msg_id,
        })
        log.info("  → SENT_3 to %s%s", to_addr, " [DRY RUN]" if dry_run else "")
        return True

    return False


def main(dry_run: bool) -> None:
    init_db()

    mode_label = "[DRY RUN]" if dry_run else "[LIVE]"
    log.info("Sender starting %s — daily cap: %d", mode_label, MAX_DAILY)

    sent_today = count_sent_today()
    log.info("Already sent today: %d / %d", sent_today, MAX_DAILY)

    if sent_today >= MAX_DAILY:
        log.info("Daily cap reached. Exiting.")
        return

    # Collect all actionable rows
    actionable: list[sqlite3.Row] = []
    for state in ("DRAFTED", "SENT_1", "SENT_2"):
        actionable.extend(fetch_by_state(state))

    if not actionable:
        log.info("No businesses ready to send.")
        return

    log.info("Actionable businesses: %d", len(actionable))

    dispatched = 0
    for row in actionable:
        if sent_today + dispatched >= MAX_DAILY:
            log.info("Daily cap hit mid-run. Stopping.")
            break

        log.info("[%s] %s (state=%s)", row["place_id"], row["display_name"], row["state"])
        try:
            did_send = dispatch_business(row, dry_run)
        except Exception as exc:
            log.error("Error dispatching %s: %s", row["place_id"], exc, exc_info=True)
            continue

        if did_send:
            dispatched += 1
            attempt_map = {"DRAFTED": 1, "SENT_1": 2, "SENT_2": 3}
            attempt_num = attempt_map.get(row["state"], 1)
            if not dry_run:
                log_send(row["place_id"], attempt_num)

            if dispatched < MAX_DAILY - sent_today:
                delay = random.uniform(DELAY_MIN, DELAY_MAX)
                log.info("  Waiting %.0f s before next send ...", delay)
                if not dry_run:
                    time.sleep(delay)
                else:
                    log.info("  [DRY RUN] Skipping wait")

    log.info("Sender complete. Dispatched this run: %d", dispatched)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Send cold emails or follow-ups")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--dry-run", dest="dry_run", action="store_true", default=None,
                       help="Print emails to console, never send (default: reads DRY_RUN from .env)")
    group.add_argument("--live", dest="dry_run", action="store_false",
                       help="Send real emails via SMTP")
    args = parser.parse_args()

    effective_dry_run = _ENV_DRY_RUN if args.dry_run is None else args.dry_run
    main(dry_run=effective_dry_run)
