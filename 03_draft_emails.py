"""
03_draft_emails.py — AI email drafter (zero-hallucination mode).

Connects to a configured LLM (Ollama local / OpenAI / Anthropic).
For each QUALIFIED business, generates 3 email variants and stores them in DB.

Usage:
    python 03_draft_emails.py
    python 03_draft_emails.py --limit 5
"""
from __future__ import annotations

import argparse
import json
import os
import time

import httpx
from dotenv import load_dotenv

from db import fetch_by_state, init_db, json_loads_safe, set_state
from logger_setup import get_logger

load_dotenv()
log = get_logger("03_draft")

LLM_PROVIDER = os.getenv("LLM_PROVIDER", "ollama").lower()

# ── LLM Dispatch ─────────────────────────────────────────────────────────────

def call_ollama(prompt: str) -> str:
    base_url = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
    model = os.getenv("OLLAMA_MODEL", "llama3.1:8b")
    payload = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": 0.3, "top_p": 0.9},
    }
    resp = httpx.post(f"{base_url}/api/generate", json=payload, timeout=120)
    resp.raise_for_status()
    return resp.json().get("response", "").strip()


def call_openai(prompt: str) -> str:
    from openai import OpenAI  # lazy import

    base_url = os.getenv("OPENAI_BASE_URL")
    kwargs = {"api_key": os.getenv("OPENAI_API_KEY")}
    if base_url:
        kwargs["base_url"] = base_url
    client = OpenAI(**kwargs)
    model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        temperature=0.3,
        max_tokens=800,
    )
    return resp.choices[0].message.content.strip()


def call_anthropic(prompt: str) -> str:
    import anthropic  # lazy import

    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    model = os.getenv("ANTHROPIC_MODEL", "claude-haiku-3-5")
    msg = client.messages.create(
        model=model,
        max_tokens=800,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    )
    return msg.content[0].text.strip()


def call_llm(prompt: str) -> str:
    dispatch = {
        "ollama": call_ollama,
        "openai": call_openai,
        "anthropic": call_anthropic,
    }
    fn = dispatch.get(LLM_PROVIDER)
    if fn is None:
        raise ValueError(f"Unknown LLM_PROVIDER: {LLM_PROVIDER!r}. Valid options: ollama, openai, anthropic")
    return fn(prompt)


# ── Prompts ───────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are a cold-email copywriter for a small web development studio.
Your only job is to write short, human, plain-text emails that mention ONLY the specific
technical problems provided. You NEVER invent data, scores, or facts not given to you.
You NEVER use marketing language, spam words, or hype.
You write in the language specified. You output ONLY the email text — no explanation, no meta-commentary."""

PROMPT_TEMPLATE_ES = """\
Negocio: {name}
Sitio web: {website}
Problemas técnicos detectados:
{problems_list}

Redacta TRES correos electrónicos en español manteniendo un tono profesional impecable en tercera persona formal (Usted, sin tutear ni mezclar registros). Texto plano puro, sin HTML, sin firmas inventadas:

--- EMAIL 1 (day1) ---
Máximo 4 oraciones.
1. Saludo formal y mención precisa de los problemas técnicos detectados sin exagerar.
2. Propuesta técnica: ofrecer montarles una maqueta de prueba funcional activa por 5 días con sus datos públicos para comparar velocidad y reserva directa. Preguntar si desean que se les envíe el enlace por esa vía.
3. Salida limpia: "Si no es una prioridad técnica para su equipo en este momento, indíquemelo y no volveré a escribirle."
Prohibido usar palabras spam o de sumisión: "sin compromiso", "gratis", "oferta", "aumenta tus ventas", "disculpa la molestia".

--- EMAIL 2 (day3) ---
Seguimiento breve en el mismo hilo. Máximo 2 oraciones.
Preguntar cortésmente si desean recibir el enlace de la maqueta de prueba para evaluarlo desde el móvil.

--- EMAIL 3 (day7) ---
Cierre profesional. Máximo 2 oraciones.
Mencionar que asume que están ocupados en la clínica y no insistirá más, dejando abierta la puerta técnica si a futuro deciden optimizarlo. Prohibido decir "dejo de molestar".

Formato de salida estricto:
<DAY1>texto del email 1</DAY1>
<DAY3>texto del email 2</DAY3>
<DAY7>texto del email 3</DAY7>
<SUBJECT>Tiempo de carga y reservas en {name}</SUBJECT>
"""

PROMPT_TEMPLATE_EN = """\
Business: {name}
Website: {website}
Detected technical problems:
{problems_list}

Write THREE cold emails in English (plain text, no HTML, no subject line, no signature):

--- EMAIL 1 (day1) ---
Direct introduction. Maximum 4 sentences.
Mention exactly the problems detected, without exaggerating.
Offer to build them a working 5-day demo using their public data so they can see how it would look fixed.
Close with: "If this isn't a priority right now, just say the word and I won't write again."
No spam words: free, offer, boost your sales.

--- EMAIL 2 (day3) ---
Short follow-up. Maximum 2 sentences.
Just remind them of the demo offer.

--- EMAIL 3 (day7) ---
Farewell. Maximum 2 sentences.
Mention this is your last message. Keep the tone human.

Strict output format (do NOT include the delimiters in the response):
<DAY1>email 1 text</DAY1>
<DAY3>email 2 text</DAY3>
<DAY7>email 3 text</DAY7>
<SUBJECT>short subject, max 8 words</SUBJECT>
"""


def build_prompt(row: dict, problems: list[str]) -> str:
    lang = (row.get("lang") or "en").lower()
    name = row.get("display_name") or "this business"
    website = row.get("website_uri") or "(no website)"
    problems_list = "\n".join(f"- {p}" for p in problems)

    tpl = PROMPT_TEMPLATE_ES if lang == "es" else PROMPT_TEMPLATE_EN
    return tpl.format(name=name, website=website, problems_list=problems_list)


import re as _re

TAG_RE = _re.compile(r"<(DAY1|DAY3|DAY7|SUBJECT)>(.*?)</\1>", _re.DOTALL)


def parse_llm_output(text: str) -> dict[str, str]:
    matches = {m.group(1): m.group(2).strip() for m in TAG_RE.finditer(text)}
    return {
        "subject": matches.get("SUBJECT", ""),
        "body_day1": matches.get("DAY1", ""),
        "body_day2": matches.get("DAY3", ""),
        "body_day7": matches.get("DAY7", ""),
    }


def draft_for_business(row: dict) -> None:
    place_id = row["place_id"]
    name = row["display_name"] or place_id
    problems = json_loads_safe(row["detected_problems"])

    if not problems:
        log.warning("  %s has no detected_problems — skipping draft.", name)
        set_state(place_id, "DISCARDED", {"audit_notes": "No problems in QUALIFIED state — discarded during draft"})
        return

    log.info("  Drafting emails for: %s (%d problems)", name, len(problems))

    prompt = build_prompt(dict(row), problems)
    try:
        raw_output = call_llm(prompt)
    except Exception as exc:
        log.error("  LLM call failed for %s: %s", name, exc)
        return  # leave in QUALIFIED, will retry next run

    parsed = parse_llm_output(raw_output)

    # Safety check: all 4 fields populated
    missing = [k for k, v in parsed.items() if not v.strip()]
    if missing:
        log.warning("  LLM output missing fields %s for %s — raw:\n%s", missing, name, raw_output[:300])
        # Still save whatever we got; operator can review
        if not parsed["body_day1"]:
            log.error("  body_day1 is empty — skipping state transition for %s", name)
            return

    set_state(place_id, "DRAFTED", parsed)
    log.info("  → DRAFTED ✓ | subject: %s", parsed.get("subject", "(none)"))


def main(limit: int | None = None) -> None:
    init_db()
    rows = fetch_by_state("QUALIFIED")
    if not rows:
        log.info("No businesses in state QUALIFIED to draft.")
        return

    total = len(rows)
    if limit:
        rows = rows[:limit]

    log.info("Drafting for %d / %d QUALIFIED businesses (provider: %s) ...", len(rows), total, LLM_PROVIDER)

    for i, row in enumerate(rows, 1):
        log.info("[%d/%d] ──────────────────────────────────", i, len(rows))
        try:
            draft_for_business(dict(row))
        except Exception as exc:
            log.error("Unexpected error drafting %s: %s", row["place_id"], exc, exc_info=True)
        if i < len(rows):
            time.sleep(2)  # Rate-limit the LLM calls

    log.info("Draft run complete.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Draft cold emails using LLM")
    parser.add_argument("--limit", type=int, default=None, help="Max businesses to draft in this run")
    args = parser.parse_args()
    main(limit=args.limit)
