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


def analyze_listing_text(listing_text: str, make: str, model: str, year: int, seller_type: str = "") -> dict[str, Any]:
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

    is_dealer = seller_type == "forhandler"
    dealer_context = (
        "VIKTIG: Dette er en FORHANDLERANNONSE. Forhandlere er profesjonelle selgere som polerer teksten sin — "
        "tekstkvalitet er IKKE det samme som bilkvalitet. Vær ekstra skeptisk. "
        "Mangel på nøkkelinfo (dokumentert service, kjent SOH, konkrete feil) fra forhandler er mistenkelig. "
        "Høy positiv justeringssum fra forhandler er et VARSELTEGN, ikke et kjøpssignal. "
        if is_dealer else
        "Dette er en privatannonse."
    )

    prompt = f"""Du er en svært skeptisk og erfaren norsk bruktbilkjøper som analyserer en FINN.no-annonse for en {year} {make} {model}.

{dealer_context}

Annonsetekst:
\"\"\"{listing_text}\"\"\"

Svar KUN med gyldig JSON, ingen annen tekst.

{{
  "positive_signals": ["kun KONKRETE fakta med faktisk verdi — se regler nedenfor"],
  "negative_signals": ["bekymringer, kostnader, manglende info som er mistenkelig"],
  "missing_info": ["viktig manglende informasjon som kjøper bør etterspørre"],
  "seller_questions": ["konkrete spørsmål til selger"],
  "seller_motivation_score": 3,
  "seller_motivation_reasoning": "en kort setning",
  "condition_summary": "en nøktern setning om tilstand — ikke selgerens ord, dine observasjoner",
  "hard_red_flag": false
}}

STRENGE REGLER FOR positive_signals:
- IGNORER FULLSTENDIG generiske fraser: "velholdt", "pen", "god stand", "må sees", "mye bil for pengene", "godt vedlikeholdt", "fin bil", "lite brukt" og lignende salgsfluff
- IGNORER standard-utstyr som er inkludert i trim-prisen (f.eks. autopilot, premium-interiør, AWD på standardvarianter)
- GODKJENTE positive signaler (kun hvis eksplisitt nevnt med detaljer):
  * Nye bremser/dekk (faktisk nevnt, ikke bare "bra stand")
  * Dokumentert servicehistorikk med årstall/km
  * Konkret SOH%-verdi (f.eks. "87% SOH")
  * Fersk EU-godkjenning med dato
  * Eksplisitte garantivilkår (ikke bare "garanti" uten detaljer)
  * Hengerfeste eller sjeldent ettermarkedsutstyr
  * Kvittering/faktura for nylig arbeid

NEGATIVE signaler vektes TYNGRE enn positive:
- Vag tilstandsbeskrivelse fra forhandler er i seg selv et negativt signal
- Manglende servicehefte = negativt
- Ukjent SOH på elbil = negativt
- "Selges som den er" er negativt
- seller_motivation_score: 1=ikke motivert, 5=veldig motivert
- hard_red_flag: true kun ved alvorlige problemer (taxi, totalskade, alvorlig mekanisk feil, rust gjennomslag)"""

    try:
        client = OpenAI(api_key=api_key, timeout=30.0)
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
