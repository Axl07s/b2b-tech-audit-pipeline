"""
02_audit_and_extract.py — Technical auditor + email extractor.

For every business in state NEW:
  - If no website → NO_WEBSITE_MANUAL
  - If website exists:
      1. HTTP status check (down / SSL invalid)
      2. Google PageSpeed Insights mobile score
      3. BeautifulSoup CTA detection
      4. Email harvesting from home + /contact + /contacto + /about
      5. MX record validation
  → QUALIFIED (≥1 real problem + ≥1 valid email) or DISCARDED

Usage:
    python 02_audit_and_extract.py
    python 02_audit_and_extract.py --limit 10
"""
from __future__ import annotations

import argparse
import json
import os
import re
import ssl
import time
from typing import Any
from urllib.parse import urljoin, urlparse

import dns.resolver
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from tenacity import retry, stop_after_attempt, wait_exponential

from db import fetch_by_state, init_db, set_state
from logger_setup import get_logger

load_dotenv()
log = get_logger("02_audit")

# ── Constants ────────────────────────────────────────────────────────────────
PAGESPEED_API = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"
SLOW_SCORE_THRESHOLD = 50
REQUEST_TIMEOUT = 12
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    )
}

# Spam / noise email patterns to discard
EMAIL_BLACKLIST_PATTERNS = re.compile(
    r"(sentry\.io|wixpress|example\.com|\.png|\.jpg|\.svg|noreply|no-reply|"
    r"donotreply|support@wix|info@wix|@sentry|@datadog|@honeybadger)",
    re.IGNORECASE,
)

# CTA keywords / services indicating the site already has conversion flows
CTA_INDICATORS = re.compile(
    r"(wa\.me|whatsapp|calendly|cal\.com|book\s*appointment|"
    r"reservar|agenda\s*cita|schedule|book\s*now|book\s*online)",
    re.IGNORECASE,
)

CONTACT_PATHS = ["/", "/contact", "/contacto", "/about", "/about-us", "/quienes-somos"]
EMAIL_REGEX = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")


# ── Helpers ──────────────────────────────────────────────────────────────────

def _get(url: str, timeout: int = REQUEST_TIMEOUT) -> requests.Response | None:
    try:
        resp = requests.get(url, headers=HEADERS, timeout=timeout, allow_redirects=True)
        return resp
    except requests.exceptions.SSLError:
        return None
    except requests.exceptions.ConnectionError:
        return None
    except requests.exceptions.Timeout:
        return None
    except Exception as exc:
        log.debug("GET %s — unexpected error: %s", url, exc)
        return None


def normalize_url(raw: str) -> str:
    """Ensure URL has a scheme."""
    raw = raw.strip()
    if not raw.startswith(("http://", "https://")):
        raw = "https://" + raw
    return raw


def check_website_health(url: str) -> dict[str, Any]:
    """
    Returns:
        is_up: bool
        has_https: bool
        ssl_valid: bool
        status_code: int | None
        problems: list[str]
    """
    result: dict[str, Any] = {
        "is_up": False,
        "has_https": url.startswith("https://"),
        "ssl_valid": True,
        "status_code": None,
        "problems": [],
    }

    # Try HTTPS first; then HTTP fallback
    for scheme in (("https://", True), ("http://", False)):
        prefix, is_https = scheme
        test_url = url if url.startswith(prefix) else re.sub(r"^https?://", prefix, url)
        try:
            resp = requests.get(
                test_url, headers=HEADERS, timeout=REQUEST_TIMEOUT,
                allow_redirects=True, verify=is_https
            )
            result["status_code"] = resp.status_code
            result["is_up"] = resp.status_code < 400
            result["has_https"] = is_https
            if resp.status_code >= 400:
                result["problems"].append(
                    f"El sitio devuelve error HTTP {resp.status_code}" if resp.status_code >= 500
                    else f"Página no encontrada (HTTP {resp.status_code})"
                )
            break
        except requests.exceptions.SSLError:
            result["ssl_valid"] = False
            result["problems"].append("Certificado SSL inválido o expirado")
            # fall through to try http
        except requests.exceptions.ConnectionError:
            result["problems"].append("Sitio web caído o inaccesible")
            break
        except requests.exceptions.Timeout:
            result["problems"].append("El sitio tarda más de 12 s en responder (timeout)")
            break

    # If only HTTP works, record missing HTTPS
    if result["is_up"] and not result["has_https"]:
        result["problems"].append("El sitio no tiene HTTPS (solo HTTP)")

    return result


@retry(stop=stop_after_attempt(2), wait=wait_exponential(min=3, max=8))
def check_pagespeed(url: str) -> int | None:
    """Returns the mobile performance score (0-100) or None on API error."""
    api_key = os.getenv("GOOGLE_PAGESPEED_API_KEY", "")
    params: dict[str, str] = {
        "url": url,
        "strategy": "mobile",
        "category": "performance",
    }
    if api_key:
        params["key"] = api_key
    try:
        resp = requests.get(PAGESPEED_API, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        score_raw = (
            data.get("lighthouseResult", {})
                .get("categories", {})
                .get("performance", {})
                .get("score")
        )
        if score_raw is not None:
            return int(float(score_raw) * 100)
    except Exception as exc:
        log.debug("PageSpeed API error for %s: %s", url, exc)
    return None


def has_cta(html: str) -> bool:
    """Returns True if the page has visible booking / WhatsApp / Calendly CTAs."""
    return bool(CTA_INDICATORS.search(html))


def extract_emails_from_html(html: str, base_domain: str) -> list[str]:
    """Extract and deduplicate emails; skip obvious noise."""
    soup = BeautifulSoup(html, "lxml")
    # Remove script and style tags to avoid false positives
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    text = soup.get_text(separator=" ")

    # Also scan mailto: links
    mailto_emails = [
        a["href"].replace("mailto:", "").split("?")[0].strip()
        for a in soup.find_all("a", href=True)
        if a["href"].startswith("mailto:")
    ]

    raw_emails = set(EMAIL_REGEX.findall(text)) | set(mailto_emails)

    cleaned: list[str] = []
    for em in raw_emails:
        em = em.lower().strip(".,;")
        if EMAIL_BLACKLIST_PATTERNS.search(em):
            continue
        cleaned.append(em)

    return sorted(set(cleaned))


def has_mx_records(email: str) -> bool:
    """Check that the email's domain has valid MX records."""
    domain = email.split("@")[-1]
    try:
        answers = dns.resolver.resolve(domain, "MX", lifetime=5)
        return bool(answers)
    except Exception:
        return False


def harvest_emails(base_url: str) -> list[str]:
    """Crawl home + contact paths, return validated email list."""
    parsed = urlparse(base_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    all_emails: set[str] = set()

    for path in CONTACT_PATHS:
        url = urljoin(origin, path)
        resp = _get(url)
        if resp is None or resp.status_code >= 400:
            continue
        emails = extract_emails_from_html(resp.text, parsed.netloc)
        all_emails.update(emails)
        time.sleep(0.5)  # polite crawl

    # Validate MX for each email domain
    valid: list[str] = []
    seen_domains: dict[str, bool] = {}
    for em in all_emails:
        domain = em.split("@")[-1]
        if domain not in seen_domains:
            seen_domains[domain] = has_mx_records(em)
        if seen_domains[domain]:
            valid.append(em)

    return valid


def build_wa_link(phone: str) -> str | None:
    if not phone:
        return None
    digits = re.sub(r"\D", "", phone)
    if len(digits) < 7:
        return None
    return f"https://wa.me/{digits}"


# ── Main audit loop ───────────────────────────────────────────────────────────

def audit_business(row: Any) -> None:
    place_id: str = row["place_id"]
    name: str = row["display_name"] or place_id
    website: str = row["website_uri"] or ""
    phone: str = row["phone"] or ""
    reviews: int = row["user_rating_count"] or 0

    log.info("Auditing: %s", name)

    # ── Case A: No website ───────────────────────────────────────────────────
    if not website:
        problem = (
            f"Sin sitio web en Google Maps a pesar de tener {reviews} reseñas"
        )
        wa = build_wa_link(phone)
        set_state(place_id, "NO_WEBSITE_MANUAL", {
            "detected_problems": json.dumps([problem]),
            "wa_link": wa,
            "audit_notes": "No website — WhatsApp contact generated",
        })
        log.info("  → NO_WEBSITE_MANUAL (%s)", wa)
        return

    # ── Case B: Has website ──────────────────────────────────────────────────
    url = normalize_url(website)
    problems: list[str] = []

    # 1. HTTP / SSL health
    health = check_website_health(url)
    problems.extend(health["problems"])

    if not health["is_up"]:
        log.info("  Website is down — still attempting email harvest from DNS/cache.")

    # 2. PageSpeed (only if site is reachable)
    ps_score: int | None = None
    if health["is_up"]:
        ps_score = check_pagespeed(url)
        if ps_score is not None and ps_score < SLOW_SCORE_THRESHOLD:
            lang = row["lang"] or "en"
            if lang == "es":
                problems.append(f"Web móvil lenta: score {ps_score}/100 en Google PageSpeed")
            else:
                problems.append(f"Slow mobile site: {ps_score}/100 on Google PageSpeed")

        # 3. CTA detection
        resp = _get(url)
        if resp and resp.status_code < 400:
            page_html = resp.text
            if not has_cta(page_html):
                lang = row["lang"] or "en"
                if lang == "es":
                    problems.append(
                        "El sitio no tiene botón visible de reserva o contacto rápido "
                        "(sin WhatsApp, Calendly, ni formulario de cita)"
                    )
                else:
                    problems.append(
                        "Site has no visible booking or quick-contact button "
                        "(no WhatsApp, Calendly, or appointment form found)"
                    )

    # 4. Email harvest
    emails = harvest_emails(url)
    log.info("  Found %d valid email(s): %s", len(emails), emails)

    # ── Qualify / Discard ────────────────────────────────────────────────────
    if problems and emails:
        set_state(place_id, "QUALIFIED", {
            "detected_problems": json.dumps(problems),
            "found_emails": json.dumps(emails),
            "audit_notes": f"PageSpeed mobile: {ps_score}",
        })
        log.info("  → QUALIFIED | problems: %d | emails: %d", len(problems), len(emails))
    else:
        reason = []
        if not problems:
            reason.append("no technical problems detected")
        if not emails:
            reason.append("no valid email found")
        set_state(place_id, "DISCARDED", {
            "audit_notes": " & ".join(reason),
            "found_emails": json.dumps(emails),
            "detected_problems": json.dumps(problems),
        })
        log.info("  → DISCARDED (%s)", " & ".join(reason))


def main(limit: int | None = None) -> None:
    init_db()
    rows = fetch_by_state("NEW")
    if not rows:
        log.info("No businesses in state NEW to audit.")
        return

    total = len(rows)
    if limit:
        rows = rows[:limit]

    log.info("Starting audit for %d / %d NEW businesses ...", len(rows), total)

    for i, row in enumerate(rows, 1):
        log.info("[%d/%d] ─────────────────────────────", i, len(rows))
        try:
            audit_business(row)
        except Exception as exc:
            log.error("Unexpected error auditing %s: %s", row["place_id"], exc, exc_info=True)
            # Mark as discarded with error note rather than leaving stuck in NEW
            set_state(row["place_id"], "DISCARDED", {"audit_notes": f"Error during audit: {exc}"})
        # Polite delay between businesses
        if i < len(rows):
            time.sleep(1)

    log.info("Audit complete.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Audit businesses from Google Maps leads")
    parser.add_argument("--limit", type=int, default=None, help="Max businesses to audit in this run")
    args = parser.parse_args()
    main(limit=args.limit)
