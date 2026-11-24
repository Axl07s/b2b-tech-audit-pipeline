"""
05_reply_listener.py — IMAP listener + LLM reply classifier + alert dispatcher.

Checks the inbox for replies from leads in leads.db.
Classifies each as: POSITIVE | NEGATIVE | OOO | BOUNCE
On POSITIVE:
  - Updates state to REPLIED_POSITIVE
  - Exports ready_demos/{place_id}.json
  - Sends Telegram alert and/or email forwarding

Usage:
    python 05_reply_listener.py
"""
from __future__ import annotations

import email
import email.header
import imaplib
import json
import os
import smtplib
import time
from datetime import datetime, timezone
from email.mime.text import MIMEText
from pathlib import Path

import httpx
from dotenv import load_dotenv

from db import fetch_one, get_conn, init_db, json_loads_safe, set_state
from logger_setup import get_logger

load_dotenv()
log = get_logger("05_reply_listener")

IMAP_HOST = os.getenv("IMAP_HOST", "imap.gmail.com")
IMAP_PORT = int(os.getenv("IMAP_PORT", "993"))
IMAP_USER = os.getenv("IMAP_USER", "")
IMAP_PASSWORD = os.getenv("IMAP_PASSWORD", "")

SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")

ALERT_EMAIL = os.getenv("ALERT_EMAIL", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

READY_DEMOS_DIR = Path(__file__).parent / "ready_demos"
READY_DEMOS_DIR.mkdir(exist_ok=True)

LLM_PROVIDER = os.getenv("LLM_PROVIDER", "ollama").lower()


# ── IMAP Helpers ──────────────────────────────────────────────────────────────

def decode_header_value(raw: str | bytes | None) -> str:
    if raw is None:
        return ""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    decoded_parts = email.header.decode_header(raw)
    result = []
    for part, charset in decoded_parts:
        if isinstance(part, bytes):
            result.append(part.decode(charset or "utf-8", errors="replace"))
        else:
            result.append(part)
    return "".join(result)


def extract_text_body(msg: email.message.Message) -> str:
    """Extract plain-text body from email (handles multipart)."""
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            cd = str(part.get("Content-Disposition", ""))
            if ct == "text/plain" and "attachment" not in cd:
                try:
                    return part.get_payload(decode=True).decode(
                        part.get_content_charset() or "utf-8", errors="replace"
                    )
                except Exception:
                    continue
        return ""
    else:
        try:
            return msg.get_payload(decode=True).decode(
                msg.get_content_charset() or "utf-8", errors="replace"
            )
        except Exception:
            return ""


def get_sender_email(msg: email.message.Message) -> str:
    from_raw = msg.get("From", "")
    # Extract just the email address
    import re
    matches = re.findall(r"[\w._%+\-]+@[\w.\-]+\.[a-zA-Z]{2,}", from_raw)
    return matches[0].lower() if matches else ""


def fetch_unread_replies() -> list[tuple[str, str, str]]:
    """
    Returns list of (from_email, subject, body_snippet) for unread messages
    whose sender appears in leads.db.
    """
    results = []
    conn_imap = None
    try:
        conn_imap = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
        conn_imap.login(IMAP_USER, IMAP_PASSWORD)
        conn_imap.select("INBOX")

        _, data = conn_imap.search(None, "UNSEEN")
        uids = data[0].split()
        log.info("Unread messages in INBOX: %d", len(uids))

        for uid in uids:
            _, msg_data = conn_imap.fetch(uid, "(RFC822)")
            raw = msg_data[0][1]
            msg = email.message_from_bytes(raw)

            from_email = get_sender_email(msg)
            subject = decode_header_value(msg.get("Subject", ""))
            body = extract_text_body(msg)

            # Only process if sender is a known lead
            lead_row = find_lead_by_email(from_email)
            if lead_row is None:
                log.debug("Unknown sender %s — ignoring", from_email)
                continue

            # Mark as seen
            conn_imap.store(uid, "+FLAGS", "\\Seen")
            results.append((from_email, subject, body[:2000], lead_row))

    except imaplib.IMAP4.error as exc:
        log.error("IMAP error: %s", exc)
    finally:
        if conn_imap:
            try:
                conn_imap.logout()
            except Exception:
                pass

    return results


def find_lead_by_email(email_addr: str):
    """Find a business row whose found_emails JSON contains this address."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM businesses WHERE found_emails LIKE ? AND state NOT IN (?, ?, ?)",
            (f"%{email_addr}%", "REPLIED_POSITIVE", "REPLIED_NEGATIVE", "BOUNCED"),
        ).fetchall()
    # Exact match inside the JSON list
    for row in rows:
        emails = json_loads_safe(row["found_emails"])
        if email_addr.lower() in [e.lower() for e in emails]:
            return row
    return None


# ── LLM Classification ────────────────────────────────────────────────────────

CLASSIFY_PROMPT = """\
You are classifying a reply to a cold outreach email.
Return ONLY one word: POSITIVE, NEGATIVE, OOO, or BOUNCE.

POSITIVE: The person shows interest, asks questions, wants to know more, or agrees to a demo.
NEGATIVE: They explicitly decline, ask to be removed, or are not interested.
OOO: This is an out-of-office auto-reply.
BOUNCE: This is a delivery failure / mailbox not found notice.

Email subject: {subject}
Email body:
{body}

Classification:"""


def classify_reply(subject: str, body: str) -> str:
    prompt = CLASSIFY_PROMPT.format(subject=subject, body=body[:1500])
    try:
        if LLM_PROVIDER == "ollama":
            resp = httpx.post(
                f"{os.getenv('OLLAMA_BASE_URL', 'http://localhost:11434')}/api/generate",
                json={"model": os.getenv("OLLAMA_MODEL", "llama3.1:8b"), "prompt": prompt,
                      "stream": False, "options": {"temperature": 0.0}},
                timeout=60,
            )
            resp.raise_for_status()
            classification = resp.json().get("response", "").strip().upper()
        elif LLM_PROVIDER == "openai":
            from openai import OpenAI
            base_url = os.getenv("OPENAI_BASE_URL")
            kwargs = {"api_key": os.getenv("OPENAI_API_KEY")}
            if base_url:
                kwargs["base_url"] = base_url
            client = OpenAI(**kwargs)
            result = client.chat.completions.create(
                model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
                messages=[{"role": "user", "content": prompt}],
                temperature=0.0, max_tokens=10,
            )
            classification = result.choices[0].message.content.strip().upper()
        elif LLM_PROVIDER == "anthropic":
            import anthropic
            client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
            msg = client.messages.create(
                model=os.getenv("ANTHROPIC_MODEL", "claude-haiku-3-5"),
                max_tokens=10, messages=[{"role": "user", "content": prompt}],
            )
            classification = msg.content[0].text.strip().upper()
        else:
            classification = "NEGATIVE"
    except Exception as exc:
        log.error("LLM classification failed: %s", exc)
        classification = "NEGATIVE"

    # Normalize — accept only valid labels
    valid = {"POSITIVE", "NEGATIVE", "OOO", "BOUNCE"}
    for label in valid:
        if label in classification:
            return label
    return "NEGATIVE"


# ── Alert Dispatch ────────────────────────────────────────────────────────────

def send_telegram_alert(text: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        resp = httpx.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}, timeout=10)
        resp.raise_for_status()
        log.info("  Telegram alert sent.")
    except Exception as exc:
        log.error("  Telegram alert failed: %s", exc)


def send_email_alert(subject: str, body: str) -> None:
    if not ALERT_EMAIL or not SMTP_USER:
        return
    try:
        msg = MIMEText(body, "plain", "utf-8")
        msg["From"] = SMTP_USER
        msg["To"] = ALERT_EMAIL
        msg["Subject"] = subject
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.ehlo(); server.starttls()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.sendmail(SMTP_USER, [ALERT_EMAIL], msg.as_string())
        log.info("  Email alert sent to %s", ALERT_EMAIL)
    except Exception as exc:
        log.error("  Email alert failed: %s", exc)


def export_ready_demo(row) -> Path:
    place_id = row["place_id"]
    demo_data = {
        "place_id": place_id,
        "display_name": row["display_name"],
        "niche": row["niche"],
        "city": row["city"],
        "country": row["country"],
        "lang": row["lang"],
        "rating": row["rating"],
        "user_rating_count": row["user_rating_count"],
        "website_uri": row["website_uri"],
        "phone": row["phone"],
        "address": row["address"],
        "google_maps_uri": row["google_maps_uri"],
        "detected_problems": json_loads_safe(row["detected_problems"]),
        "found_emails": json_loads_safe(row["found_emails"]),
        "exported_at": datetime.now(tz=timezone.utc).isoformat(),
    }
    out_path = READY_DEMOS_DIR / f"{place_id}.json"
    out_path.write_text(json.dumps(demo_data, ensure_ascii=False, indent=2), encoding="utf-8")
    return out_path


def handle_positive(row, reply_body: str, demo_path: Path) -> None:
    name = row["display_name"] or row["place_id"]
    problems = json_loads_safe(row["detected_problems"])
    problems_text = "\n".join(f"• {p}" for p in problems)

    alert_msg = (
        f"<b>LEAD POSITIVO 🎯</b>\n"
        f"<b>Negocio:</b> {name}\n"
        f"<b>Respuesta:</b>\n{reply_body[:500]}\n\n"
        f"<b>Problemas detectados:</b>\n{problems_text}\n\n"
        f"<b>Demo JSON:</b> {demo_path}"
    )
    send_telegram_alert(alert_msg)

    email_subject = f"LEAD POSITIVO: {name}"
    email_body = (
        f"Lead positivo recibido!\n\n"
        f"Negocio: {name}\n"
        f"Problemas detectados:\n{problems_text}\n\n"
        f"Respuesta del lead:\n{reply_body[:1000]}\n\n"
        f"Demo JSON listo en: {demo_path}\n"
    )
    send_email_alert(email_subject, email_body)
    log.info("  Alerts dispatched for %s", name)


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    init_db()

    if not IMAP_USER or not IMAP_PASSWORD:
        log.error("IMAP_USER / IMAP_PASSWORD not configured in .env")
        return

    log.info("Checking IMAP inbox for replies from known leads ...")
    replies = fetch_unread_replies()

    if not replies:
        log.info("No new replies from known leads.")
        return

    log.info("Found %d replies to process.", len(replies))

    for from_email, subject, body, row in replies:
        place_id = row["place_id"]
        name = row["display_name"] or place_id
        log.info("Processing reply from %s (%s) — subject: %s", from_email, name, subject)

        classification = classify_reply(subject, body)
        log.info("  Classification: %s", classification)

        snippet = body[:300].replace("\n", " ")

        if classification == "POSITIVE":
            demo_path = export_ready_demo(row)
            set_state(place_id, "REPLIED_POSITIVE", {
                "reply_classification": "POSITIVE",
                "reply_snippet": snippet,
            })
            handle_positive(row, body, demo_path)
            log.info("  → REPLIED_POSITIVE | demo: %s", demo_path)

        elif classification == "NEGATIVE":
            set_state(place_id, "REPLIED_NEGATIVE", {
                "reply_classification": "NEGATIVE",
                "reply_snippet": snippet,
            })
            log.info("  → REPLIED_NEGATIVE")

        elif classification == "BOUNCE":
            set_state(place_id, "BOUNCED", {
                "reply_classification": "BOUNCE",
                "reply_snippet": snippet,
            })
            log.info("  → BOUNCED")

        elif classification == "OOO":
            # Out-of-office: don't change state, just log
            log.info("  OOO auto-reply — state unchanged, will retry naturally.")

        time.sleep(1)

    log.info("Reply listener done.")


if __name__ == "__main__":
    main()
