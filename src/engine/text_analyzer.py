"""LLM-assisted listing text analysis with cache-friendly deterministic fallback."""

import json
import os
from typing import Any


def _fallback_analysis() -> dict[str, Any]:
    return {
        "issues": [],
        "positives": [],
        "diligence_items": [
            {
                "question": "Kan du dokumentere servicehistorikk og eventuelle større reparasjoner?",
                "why": "Manglende dokumentasjon øker risiko for skjulte kostnader",
                "risk_if_bad": 15000,
                "category": "service",
            }
        ],
        "condition_summary": {
            "dekk": "ukjent",
            "eu_status": "ukjent",
            "service": "ukjent",
            "batteri_soh": None,
            "garanti": None,
            "selges_som_den_er": False,
            "hengerfeste": None,
            "varmepumpe": None,
            "antall_eiere": None,
            "overall_score": 5,
        },
        "negotiation_signals": {
            "motivated_seller": False,
            "reason": "ikke avklart",
            "suggested_approach": "Be om dokumentasjon først, deretter prisdiskusjon.",
        },
    }


def analyze_listing_text(listing_text: str, make: str, model: str, year: int) -> dict[str, Any]:
    """Analyze listing text with Claude Sonnet when available, else fallback."""
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        return _fallback_analysis()

    try:
        import anthropic  # type: ignore
    except Exception:
        return _fallback_analysis()

    prompt = f"""Du er en erfaren norsk bruktbilmekaniker og dealer. Les denne FINN-annonsen for en {year} {make} {model}.

Svar KUN med gyldig JSON, ingen annen tekst.

Annonsetekst:
\"\"\"{listing_text}\"\"\"

Returner denne JSON-strukturen:
{{
  "issues": [{{"name":"kort beskrivelse","severity":"low|medium|high","cost_p50":0,"cost_p90":0,"evidence":"sitat"}}],
  "positives": [{{"name":"kort beskrivelse","value_nok":0,"evidence":"sitat"}}],
  "diligence_items": [{{"question":"...","why":"...","risk_if_bad":0,"category":"battery|service|damage|legal|mechanical"}}],
  "condition_summary": {{"dekk":"ukjent","eu_status":"ukjent","service":"ukjent","batteri_soh":null,"garanti":null,"selges_som_den_er":false,"hengerfeste":null,"varmepumpe":null,"antall_eiere":null,"overall_score":5}},
  "negotiation_signals": {{"motivated_seller":false,"reason":"...","suggested_approach":"..."}}
}}"""

    try:
        client = anthropic.Anthropic(api_key=api_key)
        message = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}],
        )
        response_text = message.content[0].text
        if response_text.startswith("```"):
            response_text = response_text.split("```")[1]
            if response_text.startswith("json"):
                response_text = response_text[4:]
        parsed = json.loads(response_text)
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        pass

    return _fallback_analysis()
