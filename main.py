"""
main.py — Daily orchestrator.

Runs the five pipeline modules in order via subprocess.
Handles individual module failures gracefully (logs and continues).

Usage:
    python main.py                    # full pipeline, respects DRY_RUN from .env
    python main.py --skip-fetch       # skip step 1 (useful after first run)
    python main.py --only-listen      # only run the reply listener
    python main.py --dry-run          # force dry-run on the sender step
    python main.py --live             # force live send (overrides .env DRY_RUN)
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from logger_setup import get_logger

log = get_logger("main")

PROJECT_DIR = Path(__file__).parent
PYTHON = sys.executable  # always use the same interpreter that runs main.py

MODULES = [
    ("01_fetch_maps.py",       "Fetch Google Maps leads"),
    ("02_audit_and_extract.py","Audit & extract emails"),
    ("03_draft_emails.py",     "Draft emails with LLM"),
    ("04_sender.py",           "Send / schedule emails"),
    ("05_reply_listener.py",   "Listen for replies"),
]


def run_module(script: str, label: str, extra_args: list[str] | None = None) -> bool:
    """
    Runs a module as a subprocess.
    Returns True on success (exit code 0), False on failure.
    """
    cmd = [PYTHON, str(PROJECT_DIR / script)] + (extra_args or [])
    log.info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    log.info("▶ Starting: %s  [%s]", label, script)
    log.info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    t0 = time.monotonic()

    result = subprocess.run(
        cmd,
        cwd=str(PROJECT_DIR),
        capture_output=False,   # let stdout/stderr flow to terminal
    )

    elapsed = time.monotonic() - t0
    if result.returncode == 0:
        log.info("✓ %s completed in %.1f s", label, elapsed)
        return True
    else:
        log.error("✗ %s FAILED (exit code %d) after %.1f s", label, result.returncode, elapsed)
        return False


def main() -> None:
    parser = argparse.ArgumentParser(description="B2B Prospecting Pipeline Orchestrator")
    parser.add_argument("--skip-fetch", action="store_true",
                        help="Skip module 01 (don't call Google Places today)")
    parser.add_argument("--only-listen", action="store_true",
                        help="Only run module 05 (reply listener)")
    parser.add_argument("--dry-run", action="store_true", default=None,
                        help="Force dry-run mode in sender")
    parser.add_argument("--live", action="store_true",
                        help="Force live mode in sender (overrides .env DRY_RUN=true)")
    args = parser.parse_args()

    start_time = datetime.now(tz=timezone.utc)
    log.info("╔══════════════════════════════════════════════════╗")
    log.info("║   B2B Prospecting Pipeline — %s   ║", start_time.strftime("%Y-%m-%d %H:%M UTC"))
    log.info("╚══════════════════════════════════════════════════╝")

    # Build sender extra args
    sender_args: list[str] = []
    if args.dry_run:
        sender_args.append("--dry-run")
    elif args.live:
        sender_args.append("--live")

    # Only-listen mode
    if args.only_listen:
        run_module("05_reply_listener.py", "Listen for replies")
        return

    failures = []

    # Step 1: Fetch
    if not args.skip_fetch:
        ok = run_module("01_fetch_maps.py", "Fetch Google Maps leads")
        if not ok:
            failures.append("01_fetch_maps.py")
    else:
        log.info("⏭ Skipping 01_fetch_maps.py (--skip-fetch)")

    # Step 2: Audit
    ok = run_module("02_audit_and_extract.py", "Audit & extract emails")
    if not ok:
        failures.append("02_audit_and_extract.py")

    # Step 3: Draft
    ok = run_module("03_draft_emails.py", "Draft emails with LLM")
    if not ok:
        failures.append("03_draft_emails.py")

    # Step 4: Send
    ok = run_module("04_sender.py", "Send / schedule emails", extra_args=sender_args)
    if not ok:
        failures.append("04_sender.py")

    # Step 5: Reply listener
    ok = run_module("05_reply_listener.py", "Listen for replies")
    if not ok:
        failures.append("05_reply_listener.py")

    # Final summary
    elapsed_total = (datetime.now(tz=timezone.utc) - start_time).total_seconds()
    log.info("╔══════════════════════════════════════════════════╗")
    if failures:
        log.warning("║  Pipeline finished with %d module failure(s):    ║", len(failures))
        for f in failures:
            log.warning("║    ✗ %s", f)
    else:
        log.info("║  Pipeline finished — all modules succeeded ✓     ║")
    log.info("║  Total elapsed: %.1f s                            ║", elapsed_total)
    log.info("╚══════════════════════════════════════════════════╝")


if __name__ == "__main__":
    main()
