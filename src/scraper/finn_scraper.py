"""FINN.no scraper for used car listings."""

import json
import logging
import random
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import requests
import yaml
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

CONFIG_DIR = Path(__file__).parent.parent.parent / "config"


def load_config() -> dict[str, Any]:
    """Load scraper configuration from YAML files."""
    with open(CONFIG_DIR / "models.yaml") as f:
        models = yaml.safe_load(f)
    with open(CONFIG_DIR / "params.yaml") as f:
        params = yaml.safe_load(f)
    with open(CONFIG_DIR / "variant_aliases.yaml") as f:
        aliases = yaml.safe_load(f)
    return {"models": models["models"], "params": params["scraper"], "aliases": aliases}


def _model_key(make: str, model: str) -> str:
    """Generate a config key from make+model, e.g. 'tesla_model_y'."""
    return f"{make}_{model}".lower().replace(" ", "_").replace(".", "").replace("-", "")


def normalize_variant(
    make: str, model: str, variant_raw: str, title: str, aliases: dict[str, Any]
) -> tuple[str, float]:
    """Match variant text against known aliases. Returns (variant, uncertainty_penalty).

    Aliases in YAML are checked in definition order (most specific first).
    Within each variant, longer aliases are checked before shorter ones to avoid
    substring false positives.
    """
    key = _model_key(make, model)
    model_aliases = aliases.get(key, {})
    if not model_aliases:
        return variant_raw or "unknown", 0.10

    search_text = f"{variant_raw or ''} {title}".lower().strip()

    # Check variants in YAML order; within each, sort aliases longest-first
    for canonical, alias_list in model_aliases.items():
        sorted_aliases = sorted(alias_list, key=len, reverse=True)
        for alias in sorted_aliases:
            if alias.lower() in search_text:
                return canonical, 0.0

    logger.debug("Unknown variant for %s %s: raw='%s' title='%s'", make, model, variant_raw, title)
    return "unknown", 0.10


def compute_dq_score(listing: dict[str, Any], params: dict[str, Any]) -> float:
    """Compute data quality score for a listing."""
    dq_params = params
    score = 1.0

    if listing.get("variant") == "unknown":
        score -= dq_params.get("unknown_variant_penalty", 0.15)
    if not listing.get("km") or listing["km"] == 0:
        score -= dq_params.get("missing_km_penalty", 0.30)
    text_len = len(listing.get("listing_text") or "")
    if text_len < dq_params.get("short_text_threshold", 50):
        score -= dq_params.get("short_text_penalty", 0.10)
    if not listing.get("price_nok"):
        score -= dq_params.get("missing_price_penalty", 1.0)

    return max(score, 0.0)


def _parse_base64_data(html: str) -> list[dict[str, Any]]:
    """Extract listing data from base64-encoded script tags (FINN's actual format)."""
    import base64

    soup = BeautifulSoup(html, "html.parser")
    for script in soup.find_all("script"):
        text = script.string or ""
        if len(text) > 50000 and text.startswith("eyJ"):
            try:
                decoded = base64.b64decode(text).decode("utf-8")
                data = json.loads(decoded)
                if "queries" in data:
                    for query in data["queries"]:
                        state = query.get("state", {})
                        qdata = state.get("data", {})
                        if isinstance(qdata, dict):
                            docs = qdata.get("docs", [])
                            if isinstance(docs, list) and docs:
                                logger.info("Found %d docs via base64 decode", len(docs))
                                return docs
            except Exception as e:
                logger.debug("Base64 decode attempt failed: %s", e)
    return []


def _parse_schema_org(html: str) -> list[dict[str, Any]]:
    """Fallback: extract listings from schema.org CollectionPage JSON-LD."""
    soup = BeautifulSoup(html, "html.parser")
    for script in soup.find_all("script"):
        text = script.string or ""
        if '"@type":"CollectionPage"' in text or '"@type": "CollectionPage"' in text:
            try:
                data = json.loads(text)
                items = data.get("mainEntity", {}).get("itemListElement", [])
                if items:
                    logger.info("Found %d items via schema.org", len(items))
                    # Convert schema.org format to our format
                    listings = []
                    for item in items:
                        product = item.get("item", {})
                        offers = product.get("offers", {})
                        url = product.get("url", "")
                        id_match = re.search(r'/(\d+)', url)
                        listings.append({
                            "id": id_match.group(1) if id_match else "",
                            "heading": product.get("name", ""),
                            "price": {"amount": int(offers.get("price", 0))},
                            "canonical_url": url,
                        })
                    return listings
            except (json.JSONDecodeError, ValueError):
                continue
    return []


def _parse_next_data(html: str) -> Optional[dict[str, Any]]:
    """Extract listing data from __NEXT_DATA__ script tag."""
    soup = BeautifulSoup(html, "html.parser")
    script = soup.find("script", {"id": "__NEXT_DATA__"})
    if script and script.string:
        try:
            return json.loads(script.string)
        except json.JSONDecodeError:
            logger.warning("Failed to parse __NEXT_DATA__ JSON")
    return None


def _extract_listings_from_next_data(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Navigate __NEXT_DATA__ structure to find listing items."""
    listings = []
    try:
        props = data.get("props", {}).get("pageProps", {})
        search = props.get("search", props)
        docs = search.get("docs", search.get("ads", []))
        if isinstance(docs, list):
            return docs
        for key in ["result", "data", "searchResult"]:
            if key in search and isinstance(search[key], dict):
                items = search[key].get("docs", search[key].get("ads", search[key].get("items", [])))
                if isinstance(items, list) and items:
                    return items
    except (AttributeError, TypeError) as e:
        logger.warning("Error navigating __NEXT_DATA__: %s", e)
    return listings


def _extract_listings_from_html(soup: BeautifulSoup) -> list[dict[str, Any]]:
    """Fallback: parse listing cards from HTML structure."""
    listings = []
    cards = soup.select("article[class*='ads__unit']") or soup.select("article")
    for card in cards:
        listing: dict[str, Any] = {}
        link = card.find("a", href=True)
        if link:
            href = link["href"]
            listing["listing_url"] = href if href.startswith("http") else f"https://www.finn.no{href}"
            id_match = re.search(r'/(\d+)(?:\?|$)', href)
            if id_match:
                listing["listing_id"] = id_match.group(1)

        title_el = card.find("h2") or card.find("h3")
        if title_el:
            listing["title"] = title_el.get_text(strip=True)

        price_el = card.find(string=re.compile(r'\d+\s*kr'))
        if price_el:
            price_text = re.sub(r'[^\d]', '', price_el.strip())
            if price_text:
                listing["price_nok"] = int(price_text)

        if listing.get("listing_id"):
            listings.append(listing)
    return listings


def _classify_price(price_nok: int | None, listing_text: str, title: str) -> tuple[str, str, str | None]:
    """Classify a parsed price as sale/monthly/leasing/unknown.

    Returns (price_parse_type, price_parse_confidence, skip_reason).
    """
    if price_nok is None:
        return "unknown", "NONE", "no_price"

    text_lower = (listing_text + " " + title).lower()

    # Leasing/monthly indicators in text
    monthly_keywords = ["pr. mnd", "pr.mnd", "/mnd", "per mnd", "mnd.", "maaned",
                        "pr mnd", "monthly", "per month"]
    leasing_keywords = ["leasing", "privatleasing", "firmaleasing", "operasjonell leasing",
                        "lease", "leasingpris"]

    has_monthly_hint = any(kw in text_lower for kw in monthly_keywords)
    has_leasing_hint = any(kw in text_lower for kw in leasing_keywords)

    # Hard thresholds for Norwegian used car market
    MIN_PLAUSIBLE_SALE_PRICE = 25_000  # Below this is almost certainly not a sale price
    LOW_SALE_PRICE = 50_000  # Suspiciously low for any car we track

    if price_nok < MIN_PLAUSIBLE_SALE_PRICE:
        # Under 25k NOK — almost certainly monthly/leasing/teaser
        if has_leasing_hint:
            return "leasing_price", "HIGH", "leasing_price_detected"
        if has_monthly_hint:
            return "monthly_price", "HIGH", "monthly_price_detected"
        return "monthly_price", "MEDIUM", "price_below_25k_likely_monthly"

    if price_nok < LOW_SALE_PRICE:
        # 25k–50k — suspicious, check text
        if has_leasing_hint:
            return "leasing_price", "HIGH", "leasing_price_detected"
        if has_monthly_hint:
            return "monthly_price", "HIGH", "monthly_price_detected"
        return "unknown", "LOW", "price_below_50k_ambiguous"

    # Normal price range — still check for leasing text
    if has_leasing_hint and not any(kw in text_lower for kw in ["ikke leasing", "kjøpt ut av leasing", "ut av leasing"]):
        return "leasing_price", "MEDIUM", "leasing_text_in_normal_price"
    if has_monthly_hint:
        # High price + monthly hint: the price might be the sale price with monthly payment mentioned
        return "sale_price", "MEDIUM", None

    return "sale_price", "HIGH", None


def _normalize_listing(
    raw: dict[str, Any], make: str, model: str, aliases: dict[str, Any]
) -> Optional[dict[str, Any]]:
    """Normalize a raw listing dict into our standard schema."""
    listing_id = str(
        raw.get("id", raw.get("ad_id", raw.get("listing_id", raw.get("finnkode", ""))))
    )
    if not listing_id:
        return None

    price = raw.get("price", raw.get("price_nok", raw.get("main_search_key")))
    if isinstance(price, dict):
        price = price.get("amount", price.get("value"))
    try:
        price_nok = int(price) if price else None
    except (ValueError, TypeError):
        price_nok = None

    variant_raw = raw.get("model_specification", raw.get("variant", raw.get("heading_suffix", "")))
    title = raw.get("heading", raw.get("title", ""))
    variant, uncertainty = normalize_variant(make, model, str(variant_raw), str(title), aliases)

    km = raw.get("mileage", raw.get("km", raw.get("kilometres")))
    if isinstance(km, str):
        km = int(re.sub(r'[^\d]', '', km) or 0)
    elif isinstance(km, (int, float)):
        km = int(km)
    else:
        km = None

    year = raw.get("year", raw.get("model_year"))
    if isinstance(year, str):
        year_match = re.search(r'(\d{4})', year)
        year = int(year_match.group(1)) if year_match else None
    elif isinstance(year, (int, float)):
        year = int(year)
    else:
        year = None

    listing_url = raw.get("canonical_url", raw.get("listing_url", ""))
    if not listing_url and listing_id:
        listing_url = f"https://www.finn.no/car/used/ad.html?finnkode={listing_id}"

    location = raw.get("location", "")
    if isinstance(location, dict):
        location = location.get("city", location.get("name", str(location)))

    seller_type = "privat"
    dealer_segment = raw.get("dealer_segment", "")
    if dealer_segment and "forhandler" in str(dealer_segment).lower():
        seller_type = "forhandler"
    elif raw.get("dealer") or raw.get("organisation_name") or raw.get("company"):
        seller_type = "forhandler"

    reg_nr = raw.get("registration_number", raw.get("regno", None))
    listing_text = raw.get("body", raw.get("description", raw.get("listing_text", "")))
    n_images = raw.get("image_count", len(raw.get("images", raw.get("image_urls", []))))
    listing_date = raw.get("published", raw.get("timestamp", raw.get("listing_date")))
    fuel_type = raw.get("fuel", raw.get("fuel_type", ""))
    gearbox = raw.get("transmission", raw.get("gearbox", ""))

    # Classify parsed price as sale/monthly/leasing/unknown
    price_parse_type, price_parse_confidence, skip_reason = _classify_price(
        price_nok, str(listing_text or ""), str(title)
    )

    return {
        "listing_id": listing_id,
        "listing_url": listing_url,
        "make": make,
        "model": model,
        "variant": variant,
        "variant_uncertainty": uncertainty,
        "variant_match_confidence": "HIGH" if uncertainty == 0.0 else ("LOW" if uncertainty >= 0.10 else "MEDIUM"),
        "year": year,
        "km": km,
        "price_nok": price_nok,
        "price_parse_type": price_parse_type,
        "price_parse_confidence": price_parse_confidence,
        "skip_reason": skip_reason,
        "location_city": str(location),
        "fuel_type": str(fuel_type),
        "gearbox": str(gearbox),
        "seller_type": seller_type,
        "listing_text": str(listing_text or ""),
        "listing_date": str(listing_date or ""),
        "registration_number": reg_nr,
        "n_images": int(n_images) if n_images else 0,
        "title": str(title),
    }


def _get_next_page_url(soup: BeautifulSoup, current_url: str, had_results: bool = False) -> Optional[str]:
    """Find the next page URL from pagination.

    Args:
        soup: Parsed HTML.
        current_url: URL of the current page.
        had_results: Whether the current page had listings. If True and no
            explicit next link, we construct next page by incrementing page=N.
    """
    # Strategy 1: explicit rel="next" link
    next_link = soup.find("a", {"rel": "next"}) or soup.find("a", string=re.compile(r"Neste|Next|>"))
    if next_link and next_link.get("href"):
        href = next_link["href"]
        return href if href.startswith("http") else f"https://www.finn.no{href}"

    # Strategy 2: look for pagination links with page=N+1
    page_match = re.search(r'[?&]page=(\d+)', current_url)
    current_page = int(page_match.group(1)) if page_match else 1

    # Check if there's any link containing page=N+1
    next_page = current_page + 1
    for a in soup.find_all("a", href=True):
        href = a["href"]
        m = re.search(r'[?&]page=(\d+)', href)
        if m and int(m.group(1)) == next_page:
            return href if href.startswith("http") else f"https://www.finn.no{href}"

    # Strategy 3: if current page had results, construct next URL by incrementing page param
    # FINN's base64-encoded format doesn't have <a rel=next>, so we must construct it
    if had_results:
        if page_match:
            return re.sub(r'([?&]page=)\d+', '\\g<1>' + str(next_page), current_url)
        else:
            # First page (no page= param yet), add page=2
            sep = "&" if "?" in current_url else "?"
            return f"{current_url}{sep}page=2"

    return None


def scrape_model(
    make: str,
    model: str,
    finn_query: str,
    params: dict[str, Any],
    aliases: dict[str, Any],
    session: Optional[requests.Session] = None,
    max_listings: Optional[int] = None,
) -> list[dict[str, Any]]:
    """Scrape all listings for a given model from FINN.

    Args:
        make: Car make (e.g. 'Tesla').
        model: Car model (e.g. 'Model Y').
        finn_query: Search query string for FINN (e.g. 'tesla model y').
        params: Scraper parameters.
        aliases: Variant alias config.
        session: Optional requests session.

    Returns:
        List of normalized listing dicts.
    """
    if session is None:
        session = requests.Session()
        session.headers.update({"User-Agent": params["user_agent"]})

    from urllib.parse import quote_plus
    # sales_form=1 filters to "Til salgs" (for sale) — excludes leasing/rental listings at source
    base_url = f"https://www.finn.no/mobility/search/car?q={quote_plus(finn_query)}&sort=PUBLISHED_DESC&sales_form=1"
    all_listings: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    url = base_url
    max_pages = params.get("max_pages_per_model", 20)

    stop_reason = "max_pages_reached"
    expected_per_page = None  # Track first page size to detect short last pages

    for page_num in range(1, max_pages + 1):
        logger.info("Scraping %s %s page %d: %s", make, model, page_num, url)

        try:
            resp = session.get(url, timeout=30)
            resp.raise_for_status()
        except requests.RequestException as e:
            logger.error("Request failed for %s %s page %d: %s", make, model, page_num, e)
            break

        html = resp.text
        soup = BeautifulSoup(html, "html.parser")

        raw_listings: list[dict[str, Any]] = []

        # Strategy 1: Base64-encoded JSON (FINN's actual format)
        raw_listings = _parse_base64_data(html)

        # Strategy 2: Schema.org JSON-LD
        if not raw_listings:
            raw_listings = _parse_schema_org(html)

        # Strategy 3: __NEXT_DATA__
        if not raw_listings:
            next_data = _parse_next_data(html)
            if next_data:
                raw_listings = _extract_listings_from_next_data(next_data)
                if raw_listings:
                    logger.info("Found %d listings via __NEXT_DATA__", len(raw_listings))

        # Strategy 4: HTML parsing
        if not raw_listings:
            raw_listings = _extract_listings_from_html(soup)
            if raw_listings:
                logger.info("Found %d listings via HTML parsing", len(raw_listings))

        if not raw_listings:
            stop_reason = "empty_page"
            logger.info("Stopping pagination for %s %s on page %d: %s", make, model, page_num, stop_reason)
            break

        # Track expected page size from first full page
        if expected_per_page is None and len(raw_listings) >= 20:
            expected_per_page = len(raw_listings)

        new_on_page = 0
        for raw in raw_listings:
            # Post-filter: verify heading contains expected make/model
            heading = str(raw.get("heading", "")).lower()
            if make.lower() not in heading or model.lower() not in heading:
                continue

            normalized = _normalize_listing(raw, make, model, aliases)
            if normalized and normalized["listing_id"] not in seen_ids:
                seen_ids.add(normalized["listing_id"])
                all_listings.append(normalized)
                new_on_page += 1

        if new_on_page == 0:
            stop_reason = "no_new_listings"
            logger.info("Stopping pagination for %s %s on page %d: %s", make, model, page_num, stop_reason)
            break

        if max_listings and len(all_listings) >= max_listings:
            stop_reason = "max_listings_reached"
            logger.info("Stopping pagination for %s %s on page %d: max_listings=%d reached", make, model, page_num, max_listings)
            break

        logger.info("Page %d: %d new listings (total %d, raw %d)", page_num, new_on_page, len(all_listings), len(raw_listings))

        # If this page had significantly fewer results than expected, it's the last page
        is_short_page = (
            expected_per_page is not None
            and len(raw_listings) < expected_per_page * 0.7
        )
        if is_short_page:
            stop_reason = "short_page_last"
            logger.info("Stopping pagination for %s %s on page %d: short page (%d vs expected %d)",
                         make, model, page_num, len(raw_listings), expected_per_page)
            break

        # Rate limiting
        delay = random.uniform(params["delay_min"], params["delay_max"])
        logger.debug("Sleeping %.1f seconds before next page", delay)
        time.sleep(delay)

        # Next page - pass had_results=True since we got listings on this page
        next_url = _get_next_page_url(soup, url, had_results=(len(raw_listings) > 0))
        if not next_url or next_url == url:
            stop_reason = "no_next_page"
            logger.info("Stopping pagination for %s %s on page %d: %s", make, model, page_num, stop_reason)
            break
        url = next_url
    else:
        logger.info("Stopping pagination for %s %s: %s (%d pages)", make, model, stop_reason, max_pages)

    logger.info("Total %d listings scraped for %s %s (stop_reason=%s)", len(all_listings), make, model, stop_reason)
    return all_listings


def scrape_all_models(
    config: Optional[dict[str, Any]] = None,
    max_total_listings: Optional[int] = None,
) -> dict[str, list[dict[str, Any]]]:
    """Scrape listings for all configured models. Returns {make_model: [listings]}.

    max_total_listings: stop scraping (across all models) once this many collected.
    """
    if config is None:
        config = load_config()

    models = config["models"]
    params = config["params"]
    aliases = config.get("aliases", {})

    session = requests.Session()
    session.headers.update({"User-Agent": params["user_agent"]})

    all_results: dict[str, list[dict[str, Any]]] = {}

    for model_cfg in models:
        make = model_cfg["make"]
        model = model_cfg["model"]
        finn_query = model_cfg.get("finn_query", f"{make} {model}")
        key = _model_key(make, model)

        if max_total_listings is not None:
            collected_so_far = sum(len(v) for v in all_results.values())
            if collected_so_far >= max_total_listings:
                logger.info("Reached max_total_listings=%d, skipping remaining models", max_total_listings)
                break
            model_max = max_total_listings - collected_so_far
        else:
            model_max = None

        listings = scrape_model(make, model, finn_query, params, aliases, session, max_listings=model_max)

        # Apply DQ scoring
        dq_params_cfg = yaml.safe_load(open(CONFIG_DIR / "params.yaml"))["data_quality"]
        filtered = []
        for listing in listings:
            dq = compute_dq_score(listing, dq_params_cfg)
            listing["dq_score"] = round(dq, 2)
            if dq < dq_params_cfg.get("min_dq_score", 0.50):
                logger.debug("Dropping listing %s (dq=%.2f)", listing["listing_id"], dq)
                continue
            if dq < dq_params_cfg.get("low_dq_threshold", 0.70):
                listing["flags"] = listing.get("flags", []) + ["LOW_DATA_QUALITY"]
            filtered.append(listing)

        all_results[key] = filtered
        logger.info("%s %s: %d listings after DQ filter (from %d)", make, model, len(filtered), len(listings))

        # Rate limit between models
        delay = random.uniform(params["delay_min"], params["delay_max"])
        time.sleep(delay)

    return all_results


def flatten_results(results: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """Flatten per-model results into a single list."""
    flat: list[dict[str, Any]] = []
    for listings in results.values():
        flat.extend(listings)
    return flat


def fetch_listing_detail(listing_id: str, session: Optional[requests.Session] = None) -> Optional[str]:
    """Fetch the full description text from an individual FINN listing page.

    FINN search results often omit the full body text. This fetches the
    individual ad page and extracts the description from __NEXT_DATA__ or HTML.
    Returns the description string, or None if unavailable.
    """
    if session is None:
        session = requests.Session()
        session.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"})

    # FINN redirects /car/used/ad.html?finnkode=X → /mobility/item/X; use new URL directly
    url = f"https://www.finn.no/mobility/item/{listing_id}"
    try:
        resp = session.get(url, timeout=20)
        if resp.status_code != 200:
            logger.debug("Detail page %s returned %d", listing_id, resp.status_code)
            return None
    except requests.RequestException as e:
        logger.debug("Failed to fetch detail for %s: %s", listing_id, e)
        return None

    html = resp.text

    # Primary: __NEXT_DATA__ with multiple candidate paths
    next_data = _parse_next_data(html)
    if next_data:
        props = next_data.get("props", {}).get("pageProps", {})
        for path_keys in [
            ["ad", "description"],
            ["ad", "body"],
            ["adData", "description"],
            ["adData", "body"],
            ["initialState", "adDetails", "description"],
        ]:
            obj = props
            for key in path_keys:
                obj = obj.get(key) if isinstance(obj, dict) else None
            if obj and isinstance(obj, str) and len(obj) > 30:
                return obj

        # Also try a queries array (React Query cache)
        for query in next_data.get("props", {}).get("pageProps", {}).get("dehydratedState", {}).get("queries", []):
            state_data = query.get("state", {}).get("data", {})
            if isinstance(state_data, dict):
                desc = state_data.get("description") or state_data.get("body")
                if desc and isinstance(desc, str) and len(desc) > 30:
                    return desc

    # Fallback: HTML element search (FINN uses data-testid="expandable-section" for the ad body)
    soup = BeautifulSoup(html, "html.parser")
    for selector in [
        '[data-testid="expandable-section"]',
        '[data-testid="ad-description"]',
        ".object-description-body",
        "section.panel > .panel-body",
        'div[class*="description"]',
    ]:
        el = soup.select_one(selector)
        if el:
            text = el.get_text(separator="\n", strip=True)
            if len(text) > 30:
                return text

    return None


def enrich_listing_texts(
    listings: list[dict[str, Any]],
    min_text_len: int = 100,
) -> int:
    """Fetch full description for listings whose text is shorter than min_text_len.

    Updates listing dicts in-place. Returns count of listings enriched.
    """
    candidates = [l for l in listings if len(l.get("listing_text", "") or "") < min_text_len]
    if not candidates:
        return 0

    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"})

    enriched = 0
    for listing in candidates:
        detail = fetch_listing_detail(listing["listing_id"], session)
        if detail and len(detail) > len(listing.get("listing_text", "") or ""):
            listing["listing_text"] = detail
            enriched += 1
        time.sleep(random.uniform(0.3, 0.7))

    return enriched
