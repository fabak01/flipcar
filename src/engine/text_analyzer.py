"""LLM-assisted listing text analysis with resilient fallback.

AI enriches output but NEVER blocks classification.
Every listing carries ai_status so downstream knows what happened.
"""

import json
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)


def _empty_ai_result(ai_status: str) -> dict[str, Any]:
    """Return a safe empty AI result with the given status."""
    return {
        "ai_status": ai_status,
        "ai_used_for_classification": False,
        "ai_used_for_enrichment": False,
        "positive_signals": [],
        "negative_signals": [],
        "missing_info": [],
        "seller_questions": [],
        "seller_motivation_score": None,
        "seller_motivation_reasoning": None,
        "condition_summary": None,
        "hard_red_flag": False,
        "ai_summary_short": "AI unavailable — rule-based only" if ai_status != "ok" else "",
    }


def analyze_listing_text(listing_text: str, make: str, model: str, year: int) -> dict[str, Any]:
    """Analyze listing text with OpenAI gpt-4o-mini when available, else graceful fallback.

    Returns dict with ai_status field indicating what happened.
    AI failure never blocks classification.
    """
    if not listing_text or len(listing_text) < 50:
        return _empty_ai_result("short_text")

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        logger.info("OPENAI_API_KEY missing — AI analysis skipped")
        return _empty_ai_result("fallback_no_api_key")

    try:
        from openai import OpenAI
    except Exception as e:
        logger.warning("OpenAI import failed: %s", e)
        return _empty_ai_result("fallback_import_error")

    prompt = f"""Analyser denne norske FINN.no-annonsen for en {year} {make} {model}.

Svar KUN med gyldig JSON, ingen annen tekst.

Annonsetekst:
\"\"\"{listing_text}\"\"\"

Returner denne JSON-strukturen:
{{
  "positive_signals": ["spesifikke positive ting fra annonsen"],
  "negative_signals": ["spesifikke bekymringer eller sannsynlige kostnader"],
  "missing_info": ["viktig manglende informasjon aa spoerre selger om"],
  "seller_questions": ["praktiske spoersmaal til selger"],
  "seller_motivation_score": 3,
  "seller_motivation_reasoning": "en kort setning",
  "condition_summary": "en kort setning om tilstand",
  "hard_red_flag": false
}}

Regler:
- Vaer spesifikk — kun ting som faktisk staar eller er tydelig implisert
- Ikke hallusinér
- Ignorer generisk salgsfluff
- Fokuser paa handlingsrelevant informasjon for en kjoeper
- seller_motivation_score: 1=ikke motivert, 5=veldig motivert
- hard_red_flag: true kun ved alvorlige problemer (taxi, totalskade, alvorlig mekanisk)"""

    try:
        client = OpenAI(api_key=api_key)
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "Du er en erfaren norsk bruktbilkjoeper. Svar kun med gyldig JSON."},
                {"role": "user", "content": prompt},
            ],
            response_format={"type": "json_object"},
            max_tokens=2000,
            temperature=0.3,
        )
        response_text = response.choices[0].message.content
        parsed = json.loads(response_text)
        if isinstance(parsed, dict):
            parsed["ai_status"] = "ok"
            parsed["ai_used_for_classification"] = False
            parsed["ai_used_for_enrichment"] = True

            # Build ai_summary_short
            motivation = parsed.get("seller_motivation_score", "?")
            condition = parsed.get("condition_summary", "")
            summary_parts = [f"Motivasjon {motivation}/5"]
            if condition:
                summary_parts.append(condition[:60])
            parsed["ai_summary_short"] = "; ".join(summary_parts)

            return parsed
    except json.JSONDecodeError as e:
        logger.warning("AI response JSON parse failed: %s", e)
    except Exception as e:
        logger.warning("AI API call failed: %s", e)

    return _empty_ai_result("fallback_runtime_error")
