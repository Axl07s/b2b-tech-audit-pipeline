"""
01_fetch_maps.py — Fetches up to 60 businesses from Google Places API (New).

Usage:
    python 01_fetch_maps.py
    python 01_fetch_maps.py --target-index 2   # force a specific target
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv
from tenacity import retry, stop_after_attempt, wait_exponential

from db import init_db, upsert_business
from logger_setup import get_logger

load_dotenv()
log = get_logger("01_fetch_maps")

TARGETS_FILE = Path(__file__).parent / "targets.json"
PLACES_API_URL = "https://places.googleapis.com/v1/places:searchText"
FIELD_MASK = (
    "places.id,"
    "places.displayName,"
    "places.rating,"
    "places.userRatingCount,"
    "places.websiteUri,"
    "places.nationalPhoneNumber,"
    "places.formattedAddress,"
    "places.googleMapsUri,"
    "nextPageToken"
)
MAX_RESULTS_PER_PAGE = 20   # Google Places (New) maximum per page
MAX_PAGES = 3               # 3 × 20 = 60 max results
MIN_RATING = 4.0
MIN_REVIEWS = 20


def load_targets() -> list[dict]:
    if not TARGETS_FILE.exists():
        log.error("targets.json not found at %s", TARGETS_FILE)
        sys.exit(1)
    return json.loads(TARGETS_FILE.read_text(encoding="utf-8"))


def save_targets(targets: list[dict]) -> None:
    TARGETS_FILE.write_text(json.dumps(targets, ensure_ascii=False, indent=2), encoding="utf-8")


def pick_target(targets: list[dict], forced_index: int | None) -> tuple[int, dict] | tuple[None, None]:
    if forced_index is not None:
        if 0 <= forced_index < len(targets):
            return forced_index, targets[forced_index]
        log.error("--target-index %d out of range (0-%d)", forced_index, len(targets) - 1)
        sys.exit(1)
    for i, t in enumerate(targets):
        if not t.get("processed", False):
            return i, t
    return None, None


@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=10))
def _post_places(payload: dict, api_key: str, page_token: str | None = None) -> dict:
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": FIELD_MASK,
    }
    body: dict[str, Any] = {**payload, "pageSize": MAX_RESULTS_PER_PAGE}
    if page_token:
        body["pageToken"] = page_token
    resp = requests.post(PLACES_API_URL, json=body, headers=headers, timeout=15)
    resp.raise_for_status()
    return resp.json()


def fetch_places(target: dict, api_key: str) -> list[dict]:
    query = f"{target['niche']} en {target['city']}" if target.get("lang") == "es" \
        else f"{target['niche']} in {target['city']}"
    location_bias: dict[str, Any] = {}

    payload: dict[str, Any] = {
        "textQuery": query,
        "languageCode": target.get("lang", "en"),
        "regionCode": target.get("country", "US"),
    }

    all_places: list[dict] = []
    page_token: str | None = None

    for page_num in range(1, MAX_PAGES + 1):
        log.info("Fetching page %d for '%s' in %s ...", page_num, target["niche"], target["city"])
        try:
            data = _post_places(payload, api_key, page_token)
        except requests.HTTPError as exc:
            log.error("Places API error on page %d: %s", page_num, exc)
            break

        places = data.get("places", [])
        log.info("  → %d results on page %d", len(places), page_num)
        all_places.extend(places)

        page_token = data.get("nextPageToken")
        if not page_token:
            break
        # Google requires a short pause before using the next page token
        time.sleep(2)

    log.info("Total raw results fetched: %d", len(all_places))
    return all_places


def filter_and_save(places: list[dict], target: dict) -> int:
    inserted = 0
    skipped_quality = 0
    skipped_duplicate = 0

    for place in places:
        rating = place.get("rating", 0.0) or 0.0
        reviews = place.get("userRatingCount", 0) or 0

        if rating < MIN_RATING or reviews < MIN_REVIEWS:
            skipped_quality += 1
            continue

        display_name_obj = place.get("displayName", {})
        display_name = display_name_obj.get("text", "") if isinstance(display_name_obj, dict) else str(display_name_obj)

        row = {
            "place_id": place.get("id", ""),
            "niche": target["niche"],
            "city": target["city"],
            "country": target.get("country", ""),
            "lang": target.get("lang", "en"),
            "display_name": display_name,
            "rating": rating,
            "user_rating_count": reviews,
            "website_uri": place.get("websiteUri", ""),
            "phone": place.get("nationalPhoneNumber", ""),
            "address": place.get("formattedAddress", ""),
            "google_maps_uri": place.get("googleMapsUri", ""),
            "state": "NEW",
        }

        if not row["place_id"]:
            log.warning("Place with no ID skipped: %s", display_name)
            continue

        was_inserted = upsert_business(row)
        if was_inserted:
            inserted += 1
        else:
            skipped_duplicate += 1

    log.info(
        "Saved %d new businesses (skipped %d quality, %d duplicates)",
        inserted, skipped_quality, skipped_duplicate,
    )
    return inserted


def main(forced_index: int | None = None) -> None:
    init_db()

    api_key = os.getenv("GOOGLE_PLACES_API_KEY", "")
    if not api_key:
        log.error("GOOGLE_PLACES_API_KEY is not set in .env")
        sys.exit(1)

    targets = load_targets()
    idx, target = pick_target(targets, forced_index)

    if target is None:
        log.info("All targets in targets.json have been processed. Add new ones to continue.")
        return

    log.info("Processing target [%d]: %s in %s (%s)", idx, target["niche"], target["city"], target["country"])

    places = fetch_places(target, api_key)
    inserted = filter_and_save(places, target)

    # Mark target as processed
    targets[idx]["processed"] = True
    save_targets(targets)
    log.info("Target marked as processed. Total new leads queued: %d", inserted)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Fetch leads from Google Places API")
    parser.add_argument("--target-index", type=int, default=None, help="Force a specific target index (0-based)")
    args = parser.parse_args()
    main(forced_index=args.target_index)
