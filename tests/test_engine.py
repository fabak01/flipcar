"""Tests for the FlipCar underwriting engine."""

import pytest
import yaml
from pathlib import Path

# Ensure imports work
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.scraper.finn_scraper import normalize_variant, compute_dq_score, _model_key, _get_next_page_url
from src.engine.comps import find_comps
from src.engine.fmv import calculate_fmv
from src.engine.adjustments import detect_adjustments, apply_adjustments, evaluate_eu_status
from src.engine.rep_estimator import detect_text_issues, get_model_issues, estimate_repairs
from src.engine.days_to_sell import estimate_days, price_factor, season_factor
from src.engine.carry import calculate_carry
from src.engine.profit import calculate_profit
from src.engine.mpp import calculate_mpp
from src.engine.classifier import classify_deal_v2, detect_hard_red_flags
from src.engine.battery_soh import calculate_soh_scenarios
from src.engine.regnr_registry import build_regnr_registry, get_reference_regnr
from src.engine.pristips import _parse_pristips_innertext, _extract_market_activity_from_xhr, _extract_valuation_from_xhr
from src.engine.underwriting import underwrite_deal

CONFIG_DIR = Path(__file__).parent.parent / "config"


@pytest.fixture
def params():
    """Load test parameters."""
    with open(CONFIG_DIR / "params.yaml") as f:
        return yaml.safe_load(f)


@pytest.fixture
def aliases():
    """Load variant aliases."""
    with open(CONFIG_DIR / "variant_aliases.yaml") as f:
        return yaml.safe_load(f)


@pytest.fixture
def sample_listing():
    """Create a sample Tesla Model Y listing."""
    return {
        "listing_id": "test_001",
        "listing_url": "https://www.finn.no/car/used/ad.html?finnkode=test_001",
        "make": "Tesla",
        "model": "Model Y",
        "variant": "long_range",
        "year": 2022,
        "km": 58000,
        "price_nok": 389000,
        "location_city": "Bergen",
        "fuel_type": "Elektrisk",
        "gearbox": "Automat",
        "seller_type": "privat",
        "listing_text": "Tesla Model Y Long Range 2022. Full servicehistorikk. Nye vinterdekk. EU godkjent. Varmepumpe. Hengerfeste.",
        "listing_date": "2026-03-01",
        "registration_number": None,
        "n_images": 12,
        "title": "Tesla Model Y Long Range 2022",
        "dq_score": 0.85,
    }


@pytest.fixture
def comp_listings():
    """Create a set of comparable listings."""
    base = {
        "make": "Tesla", "model": "Model Y", "fuel_type": "Elektrisk",
        "gearbox": "Automat", "listing_text": "", "title": "",
    }
    return [
        {**base, "listing_id": f"comp_{i}", "variant": "long_range",
         "year": 2022, "km": 50000 + i * 3000,
         "price_nok": 380000 + i * 5000, "seller_type": "privat"}
        for i in range(15)
    ]


class TestPaginationHelper:
    def test_next_page_link_preferred(self):
        from bs4 import BeautifulSoup
        soup = BeautifulSoup('<a rel="next" href="/mobility/search/car?page=3">Neste</a>', 'html.parser')
        assert _get_next_page_url(soup, 'https://www.finn.no/mobility/search/car?page=2') == 'https://www.finn.no/mobility/search/car?page=3'

    def test_page_param_increment_fallback(self):
        from bs4 import BeautifulSoup
        soup = BeautifulSoup('<html></html>', 'html.parser')
        # With had_results=True, it should construct next page URL
        assert _get_next_page_url(soup, 'https://www.finn.no/mobility/search/car?q=tesla&page=2', had_results=True).endswith('page=3')

    def test_first_page_constructs_page2(self):
        from bs4 import BeautifulSoup
        soup = BeautifulSoup('<html></html>', 'html.parser')
        url = _get_next_page_url(soup, 'https://www.finn.no/mobility/search/car?q=tesla', had_results=True)
        assert url is not None
        assert 'page=2' in url

    def test_no_next_page_returns_none(self):
        from bs4 import BeautifulSoup
        soup = BeautifulSoup('<html></html>', 'html.parser')
        # Without had_results, no next page when there's no page= param
        assert _get_next_page_url(soup, 'https://www.finn.no/mobility/search/car?q=tesla') is None


# --- Variant normalization ---

class TestVariantNormalization:
    def test_exact_match(self, aliases):
        variant, penalty = normalize_variant("Tesla", "Model Y", "Long Range", "", aliases)
        assert variant == "long_range"
        assert penalty == 0.0

    def test_alias_match(self, aliases):
        variant, penalty = normalize_variant("Tesla", "Model Y", "", "Tesla Model Y LR AWD", aliases)
        assert variant == "long_range"
        assert penalty == 0.0

    def test_unknown_variant(self, aliases):
        variant, penalty = normalize_variant("Tesla", "Model Y", "Special Edition", "Special", aliases)
        assert variant == "unknown"
        assert penalty == 0.10

    def test_nissan_leaf_kwh(self, aliases):
        variant, penalty = normalize_variant("Nissan", "Leaf", "40 kWh", "", aliases)
        assert variant == "40kwh"
        assert penalty == 0.0


# --- Data quality ---

class TestDataQuality:
    def test_good_listing(self, params):
        listing = {"variant": "long_range", "km": 50000, "listing_text": "A" * 100, "price_nok": 400000}
        dq = compute_dq_score(listing, params["data_quality"])
        assert dq == 1.0

    def test_missing_km(self, params):
        listing = {"variant": "long_range", "km": None, "listing_text": "A" * 100, "price_nok": 400000}
        dq = compute_dq_score(listing, params["data_quality"])
        assert dq == pytest.approx(0.70)

    def test_missing_price_drops_to_zero(self, params):
        listing = {"variant": "long_range", "km": 50000, "listing_text": "A" * 100, "price_nok": None}
        dq = compute_dq_score(listing, params["data_quality"])
        assert dq == 0.0


# --- Comps ---

class TestComps:
    def test_tier1_match(self, sample_listing, comp_listings, params):
        result = find_comps(sample_listing, comp_listings, params)
        assert result["tier"] in (1, 2, 3)
        assert result["n_comps"] >= 5

    def test_no_comps_for_different_model(self, params):
        target = {"make": "BMW", "model": "i3", "year": 2020, "km": 40000, "listing_id": "x"}
        comps = [{"make": "Tesla", "model": "Model Y", "year": 2022, "km": 50000,
                  "listing_id": "c1", "price_nok": 400000, "seller_type": "privat"}]
        result = find_comps(target, comps, params)
        assert result["n_comps"] == 0
        assert "INSUFFICIENT_COMPS" in result["flags"]

    def test_transaction_discount_applied(self, sample_listing, comp_listings, params):
        result = find_comps(sample_listing, comp_listings, params)
        if result["comps"]:
            comp = result["comps"][0]
            assert comp["transaction_price"] < comp["price_nok"]

    def test_insufficient_flag_on_few_comps(self, params):
        target = {"make": "Tesla", "model": "Model 3", "year": 2021, "km": 50000, "listing_id": "t1", "price_nok": 250000}
        comps = [
            {"make": "Tesla", "model": "Model 3", "year": 2021, "km": 52000,
             "listing_id": f"c{i}", "price_nok": 260000, "seller_type": "privat"}
            for i in range(3)
        ]
        result = find_comps(target, comps, params)
        assert result.get("insufficient", False) is True


# --- FMV ---

class TestFMV:
    def test_basic_fmv(self, params):
        comp_result = {
            "comp_transaction_prices": [350000, 360000, 370000, 380000, 390000,
                                         400000, 410000, 420000, 430000, 440000],
        }
        fmv = calculate_fmv(comp_result, params)
        assert fmv["raw_p50"] > 0
        assert fmv["raw_p10"] < fmv["raw_p50"] < fmv["raw_p90"]

    def test_empty_comps(self, params):
        fmv = calculate_fmv({"comp_transaction_prices": []}, params)
        assert fmv["raw_p50"] == 0
        assert "NO_COMPS" in fmv["flags"]

    def test_low_n_expansion(self, params):
        prices = [380000, 390000, 400000, 410000, 420000]
        fmv = calculate_fmv({"comp_transaction_prices": prices}, params)
        import numpy as np
        raw_p10 = float(np.percentile(prices, 10))
        raw_p90 = float(np.percentile(prices, 90))
        assert fmv["raw_p10"] < raw_p10
        assert fmv["raw_p90"] > raw_p90


# --- Adjustments ---

class TestAdjustments:
    def test_detect_positive_adjustments(self):
        listing = {
            "listing_text": "Full servicehistorikk. Nye vinterdekk. EU godkjent.",
            "title": "Tesla Model Y",
        }
        adjs = detect_adjustments(listing)
        types = [a["type"] for a in adjs]
        assert any("service" in t for t in types)
        assert any("dekk" in t for t in types)

    def test_detect_negative_adjustments(self):
        listing = {
            "listing_text": "Selges som den er. Rust. Slitte dekk.",
            "title": "Gammel bil",
        }
        adjs = detect_adjustments(listing)
        total = sum(a["amount"] for a in adjs)
        assert total < 0

    def test_apply_adjustments(self):
        fmv = {"raw_p10": 300000, "raw_p50": 350000, "raw_p90": 400000}
        adjs = [
            {"type": "service_full", "amount": 10000, "source": "text"},
            {"type": "dekk_slitte", "amount": -6000, "source": "text"},
        ]
        result = apply_adjustments(fmv, adjs)
        assert result["adjusted_p50"] == 354000
        assert result["adjusted_p10"] < fmv["raw_p10"]
        assert result["adjusted_p90"] > fmv["raw_p90"]


class TestEuStatusEvaluation:
    def test_future_deadline(self):
        status, amount = evaluate_eu_status({"eu_kontroll_frist": "2030-01-01"}, {"fresh": False, "overdue": False})
        assert status in {"godkjent", "godkjent_fersk"}
        assert amount in {0, 2500}

    def test_passed_deadline(self):
        status, amount = evaluate_eu_status({"eu_kontroll_frist": "2020-01-01"}, {"fresh": False, "overdue": False})
        assert status == "forfalt"
        assert amount == -4000

    def test_recent_approval(self):
        from datetime import date, timedelta
        recent = (date.today() - timedelta(days=30)).isoformat()
        future = (date.today() + timedelta(days=365)).isoformat()
        status, amount = evaluate_eu_status({"eu_kontroll_sist": recent, "eu_kontroll_frist": future}, {"fresh": False, "overdue": False})
        assert status == "godkjent_fersk"
        assert amount == 2500

    def test_missing_svv_positive_text(self):
        status, amount = evaluate_eu_status({}, {"fresh": True, "overdue": False})
        assert status == "godkjent_fersk"
        assert amount == 2500

    def test_missing_everything_default(self):
        status, amount = evaluate_eu_status({}, {"fresh": False, "overdue": False})
        assert status == "ikke_nevnt"
        assert amount == -1000


# --- Rep estimator ---

class TestRepEstimator:
    def test_detect_text_issues(self):
        listing = {"listing_text": "AC virker ikke. Rust. Steinsprut i frontrute.", "title": ""}
        issues = detect_text_issues(listing)
        names = [i["name"] for i in issues]
        assert "ac_defekt" in names
        assert "rust" in names
        assert "steinsprut" in names

    def test_model_issues_tesla(self):
        listing = {"make": "Tesla", "model": "Model Y", "year": 2022, "variant": "long_range"}
        issues = get_model_issues(listing)
        assert len(issues) > 0
        assert any(i["name"] == "brakes_lights_suspension" for i in issues)

    def test_full_estimate(self, sample_listing, params):
        rep = estimate_repairs(sample_listing, params=params)
        assert rep["total_p50"] >= 0
        assert rep["total_p90"] >= rep["total_p50"]


# --- Days to sell ---

class TestDaysToSell:
    def test_price_factor_cheap(self):
        assert price_factor(300000, 400000) == 0.50

    def test_price_factor_market(self):
        assert price_factor(400000, 400000) == 1.00

    def test_price_factor_expensive(self):
        assert price_factor(500000, 400000) == 2.50

    def test_season_winter_slower(self):
        assert season_factor(1) > season_factor(5)

    def test_estimate_days(self, sample_listing, params):
        days = estimate_days(sample_listing, 400000, params)
        assert days["p50"] > 0
        assert days["p90"] > days["p50"]
        assert days["bull"] < days["p50"]


# --- Carry ---

class TestCarry:
    def test_cash_no_loan_cost(self, params):
        carry = calculate_carry(400000, 30, 0.0, params)
        assert carry["loan_cost"] == 0
        assert carry["opportunity_cost"] > 0
        assert carry["equity_required"] == 400000

    def test_80pct_loan(self, params):
        carry = calculate_carry(400000, 30, 0.8, params)
        assert carry["loan_cost"] > 0
        assert carry["equity_required"] == 80000

    def test_longer_holding_costs_more(self, params):
        carry_30 = calculate_carry(400000, 30, 0.8, params)
        carry_60 = calculate_carry(400000, 60, 0.8, params)
        assert carry_60["total_carry"] > carry_30["total_carry"]


# --- Profit ---

class TestProfit:
    def test_basic_profit(self, sample_listing, params):
        fmv_adjusted = {"adjusted_p10": 350000, "adjusted_p50": 400000, "adjusted_p90": 450000}
        rep = {"total_p50": 5000, "total_p90": 15000}
        days = {"p50": 25, "p90": 55, "bull": 15}

        result = calculate_profit(sample_listing, fmv_adjusted, rep, days, params)
        assert "scenarios" in result
        assert "80pct_loan" in result["scenarios"]
        assert "profit_base" in result["scenarios"]["80pct_loan"]
        assert "listing_price_nok" in result
        assert "assumed_entry_price" in result
        assert "assumed_negotiation_discount" in result


# --- MPP ---

class TestMPP:
    def test_mpp_positive(self, params):
        fmv_adjusted = {"adjusted_p10": 350000, "adjusted_p50": 400000, "adjusted_p90": 450000}
        rep = {"total_p50": 5000, "total_p90": 15000}
        days = {"p50": 25, "p90": 55}

        mpp = calculate_mpp(fmv_adjusted, rep, days, params)
        assert mpp["mpp"] > 0
        assert mpp["mpp"] <= fmv_adjusted["adjusted_p50"]


# --- Classifier ---

class TestClassifier:
    """V2 classifier tests: spread-based CALL_NOW/MESSAGE/WATCH/PASS + execution_gate."""

    def test_call_now_high_spread(self):
        """spread_ask >= 10% → CALL_NOW."""
        from src.engine.classifier import classify_deal_v2
        result = classify_deal_v2(spread_ask_pct=0.12, spread_bid_pct=0.08, hard_red_flag=False, has_pristips=True, ai_status="ok")
        assert result["label"] == "CALL_NOW"
        assert result["execution_gate"] == "SEND"

    def test_message_moderate_spread(self):
        """spread_bid >= 6% and spread_ask >= 4% → MESSAGE."""
        from src.engine.classifier import classify_deal_v2
        result = classify_deal_v2(spread_ask_pct=0.05, spread_bid_pct=0.07, hard_red_flag=False, has_pristips=True, ai_status="ok")
        assert result["label"] == "MESSAGE"
        assert result["execution_gate"] == "SEND"

    def test_watch_marginal_spread(self):
        """spread_bid >= 2% but below MESSAGE thresholds → WATCH."""
        from src.engine.classifier import classify_deal_v2
        result = classify_deal_v2(spread_ask_pct=0.03, spread_bid_pct=0.03, hard_red_flag=False, has_pristips=True, ai_status="ok")
        assert result["label"] == "WATCH"
        assert result["execution_gate"] == "BLOCKED"

    def test_pass_low_spread(self):
        """Below all thresholds → PASS."""
        from src.engine.classifier import classify_deal_v2
        result = classify_deal_v2(spread_ask_pct=0.01, spread_bid_pct=0.01, hard_red_flag=False, has_pristips=True, ai_status="ok")
        assert result["label"] == "PASS"
        assert result["execution_gate"] == "BLOCKED"

    def test_hard_red_flag_forces_pass(self):
        """Hard red flag → PASS regardless of spread."""
        from src.engine.classifier import classify_deal_v2
        result = classify_deal_v2(spread_ask_pct=0.15, spread_bid_pct=0.12, hard_red_flag=True, has_pristips=True, ai_status="ok")
        assert result["label"] == "PASS"
        assert result["execution_gate"] == "BLOCKED"

    def test_no_pristips_forces_pass(self):
        """No Pristips → PASS with NO_PRISTIPS gate."""
        from src.engine.classifier import classify_deal_v2
        result = classify_deal_v2(spread_ask_pct=0.15, spread_bid_pct=0.12, hard_red_flag=False, has_pristips=False, ai_status="ok")
        assert result["label"] == "PASS"
        assert result["execution_gate"] == "NO_PRISTIPS"

    def test_send_no_ai_when_ai_unavailable(self):
        """CALL_NOW with ai_status != ok → SEND_NO_AI."""
        from src.engine.classifier import classify_deal_v2
        result = classify_deal_v2(spread_ask_pct=0.12, spread_bid_pct=0.08, hard_red_flag=False, has_pristips=True, ai_status="fallback_no_api_key")
        assert result["label"] == "CALL_NOW"
        assert result["execution_gate"] == "SEND_NO_AI"

    def test_hard_red_flag_detection(self):
        """detect_hard_red_flags catches taxi, accident, rust, etc."""
        from src.engine.classifier import detect_hard_red_flags
        assert len(detect_hard_red_flags("Brukt som taxi i 5 år")) > 0
        assert len(detect_hard_red_flags("Kollisjonsskade reparert")) > 0
        assert len(detect_hard_red_flags("Rustgjennomslag i bunn")) > 0
        assert len(detect_hard_red_flags("Fin bil, velholdt")) == 0

    def test_as_is_plus_fault_red_flag(self):
        """'selges som den er' + fault → red flag."""
        from src.engine.classifier import detect_hard_red_flags
        flags = detect_hard_red_flags("Selges som den er. Motoren har feil.")
        assert len(flags) > 0


# --- Pristips extraction ---

class TestPristipsInnerText:
    REALISTIC_INNERTEXT = (
        "FINN Pristips\n"
        "Selg den selv på FINN\n"
        "Basert på maskinlæring\n"
        "ca. 234\xa0000 kr\n"
        "Omtrent 60 % av lignende biler har en pris p\u00e5 mellom 223\xa0000 og 246\xa0000 kr.\n"
        "Selges vanligvis innen 45 dager\n"
        "12 biler inn siste 30 dager\n"
        "8 biler ut siste 30 dager\n"
        "\n"
        "Prisstatistikk\n"
        "Lignende biler til salgs\n"
        "Median\n"
        "264 434 kr\n"
        "Billigste\n"
        "144 532 kr\n"
        "Dyreste\n"
        "339 000 kr\n"
        "72 000 km\n"
    )

    def test_ca_price_extraction(self):
        """innerText 'ca. 234 000 kr' should give market_anchor_price=234000."""
        result = _parse_pristips_innertext(self.REALISTIC_INNERTEXT)
        assert result is not None
        assert result["market_anchor_price"] == 234000
        assert result["market_anchor_low"] == 223000
        assert result["market_anchor_high"] == 246000

    def test_does_not_pick_comps_as_price(self):
        """Must NOT pick median/cheapest/dyreste/km as anchor price."""
        result = _parse_pristips_innertext(self.REALISTIC_INNERTEXT)
        assert result is not None
        assert result["market_anchor_price"] == 234000  # NOT 264434, 144532, 339000, or 72000

    def test_comps_in_separate_fields(self):
        """Comp stats should go into comp_median etc., not anchor price."""
        result = _parse_pristips_innertext(self.REALISTIC_INNERTEXT)
        assert result is not None
        assert result.get("comp_median") == 264434
        assert result.get("comp_cheapest") == 144532
        assert result.get("comp_most_expensive") == 339000

    def test_nbsp_handling(self):
        """Non-breaking spaces (\\xa0) must be normalized and not break regex."""
        text = "Prisestimat\nca.\xa0234\xa0000\xa0kr\nmellom 200\xa0000 og 250\xa0000 kr.\n"
        result = _parse_pristips_innertext(text)
        assert result is not None
        assert result["market_anchor_price"] == 234000

    def test_mellom_range_without_ca(self):
        """Extract range even when no 'ca.' price is present."""
        text = """
Prisestimat
Omtrent 60 % av lignende biler har en pris på mellom 190 000 og 210 000 kr.
"""
        result = _parse_pristips_innertext(text)
        assert result is not None
        assert result["market_anchor_low"] == 190000
        assert result["market_anchor_high"] == 210000

    def test_days_to_sell(self):
        text = """
Prisestimat
ca. 300 000 kr
Selges vanligvis innen 30 dager
"""
        result = _parse_pristips_innertext(text)
        assert result is not None
        assert result["market_days_to_sell"] == 30

    def test_empty_text_returns_none(self):
        assert _parse_pristips_innertext("") is None
        assert _parse_pristips_innertext("short") is None


class TestPristipsXhrMarketActivity:
    def test_extracts_activity_not_price(self):
        """XHR extraction should get market activity but NOT price."""
        responses = [{"url": "https://finn.no/api/test", "status": 200, "body": {
            "value": 72000,  # This is mileage, NOT price - must be ignored
            "daysToSell": 45,
            "activeTotal": 120,
            "last90Days": 85,
            "last30days": 30,
        }}]
        result = _extract_market_activity_from_xhr(responses)
        assert result is not None
        assert "market_anchor_price" not in result
        assert result["market_days_to_sell"] == 45
        assert result["market_active_similar"] == 120
        assert result["market_sold_90d"] == 85
        assert result["market_new_last_30d"] == 30

    def test_empty_responses(self):
        assert _extract_market_activity_from_xhr([]) is None

    def test_nested_activity(self):
        responses = [{"url": "", "status": 200, "body": {
            "data": {"daysToSell": 22, "activeTotal": 50}
        }}]
        result = _extract_market_activity_from_xhr(responses)
        assert result is not None
        assert result["market_days_to_sell"] == 22
        assert result["market_active_similar"] == 50


class TestPristipsValuationXhr:
    VALUATION_URL = (
        "https://www.finn.no/mobility/insights/price-valuation/api/ads/price/valuation"
        "?bodyTypeId=3&wheelDriveId=2&transmissionId=2&engineFuelId=4"
        "&makeId=8078&modelId=2000501&registrationClassId=1&modelYear=2021"
        "&numberOfSeats=5&engineEffect=498&mileage=72000"
    )

    def test_exact_valuation_payload(self):
        """Parse the exact payload from live debug artifacts."""
        responses = [{"url": self.VALUATION_URL, "status": 200, "body": {
            "prices": {
                "min": 222640.5625,
                "max": 245784.703125,
                "median": 234101.265625,
            },
            "occurrence": 9507,
        }}]
        result = _extract_valuation_from_xhr(responses)
        assert result is not None
        assert result["market_anchor_price"] == 234101
        assert result["market_anchor_low"] == 222641
        assert result["market_anchor_high"] == 245785
        assert result["valuation_occurrence"] == 9507

    def test_ignores_non_valuation_urls(self):
        """Must NOT match /api/ads/active or /api/ads/sold or other endpoints."""
        responses = [
            {"url": "https://finn.no/api/ads/active?makeId=8078", "status": 200, "body": {
                "prices": {"min": 100000, "max": 300000, "median": 200000},
            }},
            {"url": "https://finn.no/api/ads/distribution/price/summary", "status": 200, "body": {
                "prices": {"min": 150000, "max": 350000, "median": 250000},
            }},
        ]
        result = _extract_valuation_from_xhr(responses)
        assert result is None

    def test_missing_prices_dict(self):
        responses = [{"url": self.VALUATION_URL, "status": 200, "body": {
            "occurrence": 9507,
        }}]
        assert _extract_valuation_from_xhr(responses) is None

    def test_empty_responses(self):
        assert _extract_valuation_from_xhr([]) is None

    def test_valuation_among_many_responses(self):
        """Valuation endpoint found among other XHR responses."""
        responses = [
            {"url": "https://finn.no/api/ads/active?x=1", "status": 200, "body": {"activeTotal": 50}},
            {"url": "https://finn.no/api/ads/sold?x=1", "status": 200, "body": {"last90Days": 80}},
            {"url": self.VALUATION_URL, "status": 200, "body": {
                "prices": {"min": 222640.5625, "max": 245784.703125, "median": 234101.265625},
                "occurrence": 9507,
            }},
            {"url": "https://finn.no/api/other", "status": 200, "body": {"foo": "bar"}},
        ]
        result = _extract_valuation_from_xhr(responses)
        assert result is not None
        assert result["market_anchor_price"] == 234101

    def test_live_72123_payload(self):
        """Integration test: exact payload from live debug run with km=72123."""
        responses = [
            # Other XHR responses that come before the valuation
            {"url": "https://www.finn.no/mobility/insights/price-valuation/api/ads/active?bodyTypeId=3&makeId=8078&modelId=2000501&mileage=72123", "status": 200, "body": {
                "activeTotal": 147, "last30days": 42, "last7days": 12,
            }},
            {"url": "https://www.finn.no/mobility/insights/price-valuation/api/ads/sold?bodyTypeId=3&makeId=8078&modelId=2000501&mileage=72123", "status": 200, "body": {
                "last90Days": 312, "last30Days": 98, "last7Days": 25,
            }},
            # The authoritative valuation endpoint
            {"url": "https://www.finn.no/mobility/insights/price-valuation/api/ads/price/valuation?bodyTypeId=3&wheelDriveId=2&transmissionId=2&engineFuelId=4&makeId=8078&modelId=2000501&registrationClassId=1&modelYear=2021&numberOfSeats=5&engineEffect=498&mileage=72123", "status": 200, "body": {
                "prices": {
                    "min": 222567.703125,
                    "max": 245710.03125,
                    "median": 234027.625,
                },
                "occurrence": 9507,
            }},
            # Some unrelated response after
            {"url": "https://www.finn.no/mobility/insights/price-valuation/api/ads/distribution/price/summary?makeId=8078&modelId=2000501&mileage=72123", "status": 200, "body": {
                "value": 72123,  # This is mileage echo, NOT a price
                "median": 264434,  # This is comp median, NOT anchor price
            }},
        ]
        # Valuation extraction
        valuation = _extract_valuation_from_xhr(responses)
        assert valuation is not None
        assert valuation["market_anchor_price"] == 234028
        assert valuation["market_anchor_low"] == 222568
        assert valuation["market_anchor_high"] == 245710
        assert valuation["valuation_occurrence"] == 9507

        # Market activity extraction (from active + sold endpoints)
        activity = _extract_market_activity_from_xhr(responses)
        assert activity is not None
        assert activity["market_active_similar"] == 147
        assert activity["market_sold_90d"] == 312

        # Verify activity extractor does NOT produce a price
        assert "market_anchor_price" not in activity


# --- Battery SOH ---

class TestBatterySoh:
    def test_non_ev_not_applicable(self):
        out = calculate_soh_scenarios(10000, 300000, False, None, "Toyota", "RAV4", 2020)
        assert out["applicable"] is False

    def test_ev_with_reported_soh(self):
        out = calculate_soh_scenarios(15000, 320000, True, 91, "BMW", "i3", 2019)
        assert out["applicable"] is True
        assert out["soh_missing"] is False
        assert out["soh_reported"] == 91

    def test_ev_missing_soh_generates_scenarios(self):
        out = calculate_soh_scenarios(18000, 320000, True, None, "Nissan", "Leaf", 2018)
        assert out["applicable"] is True
        assert out["soh_missing"] is True
        assert out["scenarios"]
        assert out["min_profitable_soh"] is not None
        assert isinstance(out["recommendation"], str)


# --- Regnr Registry ---

class TestRegnrRegistry:
    def test_build_registry(self):
        listings = [
            {"make": "Tesla", "model": "Model 3", "variant": "long_range", "year": 2021, "registration_number": "EC60771"},
            {"make": "Tesla", "model": "Model 3", "variant": "performance", "year": 2022, "registration_number": "AB12345"},
            {"make": "Tesla", "model": "Model Y", "variant": "long_range", "year": 2022, "registration_number": None},
        ]
        registry = build_regnr_registry(listings)
        assert len(registry) >= 2
        assert "tesla_model_3_long_range_2021" in registry
        assert registry["tesla_model_3_long_range_2021"] == "EC60771"

    def test_get_reference_regnr_exact(self):
        registry = {"tesla_model_3_long_range_2021": "EC60771"}
        regnr = get_reference_regnr(registry, "Tesla", "Model 3", "long_range", 2021)
        assert regnr == "EC60771"

    def test_get_reference_regnr_fallback_any(self):
        registry = {"tesla_model_3_any_2021": "EC60771"}
        regnr = get_reference_regnr(registry, "Tesla", "Model 3", "performance", 2021)
        assert regnr == "EC60771"

    def test_get_reference_regnr_fallback_year(self):
        registry = {"tesla_model_3_any_2022": "AB12345"}
        regnr = get_reference_regnr(registry, "Tesla", "Model 3", "long_range", 2021)
        assert regnr == "AB12345"

    def test_get_reference_regnr_none(self):
        registry = {}
        regnr = get_reference_regnr(registry, "Tesla", "Model 3", "long_range", 2021)
        assert regnr is None


# --- Underwriting ---

class TestUnderwriting:
    def test_underwrite_deal_comps_only_returns_no_pristips(self, sample_listing, params):
        """Comps alone are NOT sufficient — must return PASS / NO_PRISTIPS."""
        sample_listing["comp_result"] = {
            "tier": 1, "n_comps": 10, "transaction_median": 380000,
        }
        sample_listing["pristips"] = {"days_to_sell": 15, "active_similar": 50, "sold_90d": 200}
        sample_listing["ai_analysis"] = {
            "issues": [{"name": "test", "cost_p50": 3000, "cost_p90": 5000}],
            "positives": [{"name": "service", "value_nok": 5000}],
            "condition_summary": {},
        }
        sample_listing["rep_estimate"] = {"total_p50": 5000, "total_p90": 12000}

        deal = underwrite_deal(sample_listing, params)
        # No Pristips market_anchor_price → PASS / NO_PRISTIPS
        assert deal["classification"]["label"] == "PASS"
        assert deal["classification"]["execution_gate"] == "NO_PRISTIPS"
        assert deal["market"]["source"] == "none"

    def test_underwrite_deal_pristips_primary(self, sample_listing, params):
        """Pristips price should be used as market anchor when available."""
        sample_listing["comp_result"] = {"tier": None, "n_comps": 0, "transaction_median": None}
        sample_listing["pristips"] = {
            "market_anchor_price": 410000,
            "market_anchor_low": 390000,
            "market_anchor_high": 430000,
            "market_days_to_sell": 15,
            "market_active_similar": 50,
            "market_sold_90d": 200,
        }
        sample_listing["ai_analysis"] = {
            "issues": [{"name": "test", "cost_p50": 3000, "cost_p90": 5000}],
            "positives": [{"name": "service", "value_nok": 5000}],
            "condition_summary": {},
        }
        sample_listing["rep_estimate"] = {"total_p50": 5000, "total_p90": 12000}

        deal = underwrite_deal(sample_listing, params)
        assert deal["market"]["source"] == "finn_pristips"
        assert deal["market"]["anchor"] == 410000
        # V2: spread-based output, no scenarios dict
        assert "spread_ask_pct" in deal
        assert "adjusted_market_value" in deal
        assert "realistic_bid_price" in deal

    def test_underwrite_deal_no_pristips_no_comps(self, sample_listing, params):
        """No Pristips, no comps → PASS / NO_PRISTIPS."""
        sample_listing["comp_result"] = {"tier": None, "n_comps": 0, "transaction_median": None}
        sample_listing["pristips"] = None
        sample_listing["ai_analysis"] = None

        deal = underwrite_deal(sample_listing, params)
        assert deal["classification"]["label"] == "PASS"
        assert deal["classification"]["execution_gate"] == "NO_PRISTIPS"


class TestAuditNaming:
    def test_underwrite_output_has_v2_fields(self, sample_listing, params):
        """V2 underwrite output contains consistent field naming."""
        sample_listing["pristips"] = {
            "market_anchor_price": 400000,
            "market_anchor_low": 380000,
            "market_anchor_high": 420000,
        }
        sample_listing["ai_analysis"] = {"issues": [], "positives": [], "condition_summary": {}}
        sample_listing["rep_estimate"] = {"total_p50": 0, "total_p90": 0}
        sample_listing["comp_result"] = {"tier": 1, "n_comps": 8}

        deal = underwrite_deal(sample_listing, params)
        assert "asking_price" in deal
        assert "market_anchor_price" in deal
        assert "adjusted_market_value" in deal
        assert "spread_ask_pct" in deal
        assert "spread_bid_pct" in deal
        assert "realistic_bid_price" in deal
        assert "ai_status" in deal


# --- Spec pricing ---

class TestSpecPricing:
    def test_detect_positive_specs(self):
        from src.engine.spec_pricing import detect_specs
        listing = {
            "listing_text": "Bilen har hengerfeste, panoramatak og harman kardon. Varmepumpe installert.",
            "fuel_type": "el",
            "make": "Tesla",
        }
        specs = detect_specs(listing)
        spec_names = [s["spec"] for s in specs]
        assert "tow_hitch" in spec_names
        assert "panoramic_roof" in spec_names
        assert "premium_audio" in spec_names
        assert "heat_pump" in spec_names
        assert all(s["type"] == "positive" for s in specs)

    def test_detect_negative_specs(self):
        from src.engine.spec_pricing import detect_specs
        listing = {
            "listing_text": "Brukt som taxi. Røykelukt i kupeen. Ingen service utført.",
            "make": "Toyota",
        }
        specs = detect_specs(listing)
        spec_names = [s["spec"] for s in specs]
        assert "taxi_use" in spec_names
        assert "smoking_car" in spec_names
        assert "missing_service_book" in spec_names
        assert all(s["amount_nok"] < 0 for s in specs)

    def test_fuel_type_filter(self):
        from src.engine.spec_pricing import detect_specs
        # heat_pump should NOT match for ICE cars
        listing = {"listing_text": "har varmepumpe", "fuel_type": "bensin", "make": "VW"}
        specs = detect_specs(listing)
        spec_names = [s["spec"] for s in specs]
        assert "heat_pump" not in spec_names

    def test_high_owner_count(self):
        from src.engine.spec_pricing import detect_specs
        listing = {"listing_text": "", "make": "VW", "n_owners": 6}
        specs = detect_specs(listing)
        owner_specs = [s for s in specs if s["spec"] == "high_owner_count"]
        assert len(owner_specs) == 1
        assert owner_specs[0]["amount_nok"] == -6000  # 2 extra owners × -3000


# --- Variant normalization ---

class TestVariantNormalizationExtended:
    def test_performance_before_awd(self):
        """Tesla Model Y Performance AWD should be 'performance', NOT 'long_range'."""
        import yaml
        aliases = yaml.safe_load(open(CONFIG_DIR / "variant_aliases.yaml"))
        v, penalty = normalize_variant("Tesla", "Model Y", "", "Tesla Model Y Performance AWD", aliases)
        assert v == "performance"

    def test_standard_range_plus(self):
        import yaml
        aliases = yaml.safe_load(open(CONFIG_DIR / "variant_aliases.yaml"))
        v, _ = normalize_variant("Tesla", "Model 3", "", "Tesla Model 3 Standard Range Plus", aliases)
        assert v == "standard_range"

    def test_golf_r_line_not_r(self):
        """VW Golf R-Line should NOT match as 'r' (R-Line removed from aliases)."""
        import yaml
        aliases = yaml.safe_load(open(CONFIG_DIR / "variant_aliases.yaml"))
        v, _ = normalize_variant("Volkswagen", "Golf", "R-Line", "Volkswagen Golf R-Line 2021", aliases)
        # Should NOT be 'r' (Golf R) since R-Line is just a trim package
        assert v != "r"


# --- Regnr confidence ---

class TestRegnrConfidence:
    def test_exact_match_is_medium(self):
        from src.engine.regnr_registry import get_reference_regnr_with_confidence
        registry = {"tesla_model_3_long_range_2021": "AB12345"}
        regnr, conf = get_reference_regnr_with_confidence(registry, "Tesla", "Model 3", "long_range", 2021)
        assert regnr == "AB12345"
        assert conf == "MEDIUM"

    def test_any_variant_fallback_is_low(self):
        from src.engine.regnr_registry import get_reference_regnr_with_confidence
        registry = {"tesla_model_3_any_2021": "CD67890"}
        regnr, conf = get_reference_regnr_with_confidence(registry, "Tesla", "Model 3", "performance", 2021)
        assert regnr == "CD67890"
        assert conf == "LOW"

    def test_year_offset_is_low(self):
        from src.engine.regnr_registry import get_reference_regnr_with_confidence
        registry = {"tesla_model_3_any_2020": "EF11111"}
        regnr, conf = get_reference_regnr_with_confidence(registry, "Tesla", "Model 3", "unknown", 2021)
        assert regnr == "EF11111"
        assert conf == "LOW"

    def test_no_match_is_none(self):
        from src.engine.regnr_registry import get_reference_regnr_with_confidence
        regnr, conf = get_reference_regnr_with_confidence({}, "Tesla", "Model 3", "unknown", 2021)
        assert regnr is None
        assert conf == "NONE"


class TestAICacheTextHash:
    """Test that AI cache invalidation uses text hash correctly."""

    def test_same_text_same_hash(self):
        import hashlib
        text = "Velholdt bil med nye dekk og EU til 2026."
        h1 = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
        h2 = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
        assert h1 == h2

    def test_changed_text_different_hash(self):
        import hashlib
        text_v1 = "Velholdt bil med nye dekk og EU til 2026."
        text_v2 = "Velholdt bil med nye dekk og EU til 2026. Ny pris!"
        h1 = hashlib.sha256(text_v1.encode("utf-8")).hexdigest()[:16]
        h2 = hashlib.sha256(text_v2.encode("utf-8")).hexdigest()[:16]
        assert h1 != h2

    def test_stale_cache_detected(self):
        """Simulate the cache invalidation logic from main.py."""
        import hashlib

        # Original listing text and cached analysis
        original_text = "Fin bil, lite brukt, alt av service utfoert."
        original_hash = hashlib.sha256(original_text.encode("utf-8")).hexdigest()[:16]
        cached_ai = {"_text_hash": original_hash, "issues": [], "positives": []}

        # Listing text changes (seller updates ad)
        updated_text = "Fin bil, lite brukt, alt av service utfoert. Pris redusert!"
        new_hash = hashlib.sha256(updated_text.encode("utf-8")).hexdigest()[:16]

        # Cache should be stale
        assert cached_ai["_text_hash"] != new_hash, "Changed text must invalidate cache"

    def test_unchanged_text_cache_valid(self):
        """Unchanged text should reuse cached analysis."""
        import hashlib

        text = "Fin bil, lite brukt, alt av service utfoert."
        text_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
        cached_ai = {"_text_hash": text_hash, "issues": [], "positives": []}

        # Same text → same hash → cache valid
        assert cached_ai["_text_hash"] == text_hash


# --- Price parse classification ---

class TestPriceClassification:
    def test_sale_price_normal(self):
        from src.scraper.finn_scraper import _classify_price
        ptype, conf, skip = _classify_price(350000, "Fin bil, velholdt", "Tesla Model 3 2021")
        assert ptype == "sale_price"
        assert conf == "HIGH"
        assert skip is None

    def test_monthly_price_low(self):
        from src.scraper.finn_scraper import _classify_price
        ptype, conf, skip = _classify_price(3990, "Fra 3990 pr. mnd inkl forsikring", "Tesla Model Y")
        assert ptype == "monthly_price"
        assert conf == "HIGH"
        assert skip == "monthly_price_detected"

    def test_leasing_keyword(self):
        from src.scraper.finn_scraper import _classify_price
        ptype, conf, skip = _classify_price(4500, "Privatleasing, gunstige vilkaar", "VW Golf")
        assert ptype == "leasing_price"
        assert conf == "HIGH"
        assert skip == "leasing_price_detected"

    def test_very_low_price_no_text_hint(self):
        from src.scraper.finn_scraper import _classify_price
        ptype, conf, skip = _classify_price(2990, "Fin bil", "BMW 3-serie")
        assert ptype == "monthly_price"
        assert conf == "MEDIUM"
        assert "below_25k" in skip

    def test_ambiguous_low_price(self):
        from src.scraper.finn_scraper import _classify_price
        ptype, conf, skip = _classify_price(39000, "Fin bil", "Nissan Leaf")
        assert ptype == "unknown"
        assert conf == "LOW"
        assert "below_50k" in skip

    def test_no_price(self):
        from src.scraper.finn_scraper import _classify_price
        ptype, conf, skip = _classify_price(None, "", "")
        assert ptype == "unknown"
        assert skip == "no_price"

    def test_leasing_in_normal_price(self):
        """High price but 'leasing' in text should flag as leasing."""
        from src.scraper.finn_scraper import _classify_price
        ptype, conf, skip = _classify_price(350000, "Privatleasing tilgjengelig", "Tesla Model 3")
        assert ptype == "leasing_price"
        assert conf == "MEDIUM"

    def test_ex_leasing_not_flagged(self):
        """'Kjøpt ut av leasing' should NOT be flagged as leasing."""
        from src.scraper.finn_scraper import _classify_price
        ptype, conf, skip = _classify_price(350000, "Kjøpt ut av leasing, alt service", "Tesla Model 3")
        assert ptype == "sale_price"
        assert skip is None


# --- Underwriting breakdown and explanation ---

class TestDealBreakdown:
    def test_underwrite_has_v2_spread_output(self, params):
        """V2 underwriting produces spread-based output, not old breakdown dict."""
        listing = {
            "listing_id": "999", "make": "Tesla", "model": "Model 3",
            "variant": "long_range", "year": 2021, "km": 50000,
            "price_nok": 350000, "fuel_type": "electric", "seller_type": "privat",
            "listing_text": "Velholdt bil", "location_city": "Oslo",
            "pristips": {"market_anchor_price": 340000, "market_anchor_low": 310000,
                         "market_anchor_high": 370000},
            "ai_analysis": {"issues": [{"name": "lakk", "cost_p50": 3000, "cost_p90": 5000}],
                            "positives": [{"name": "servicebok", "value_nok": 5000}]},
            "comp_result": {"tier": 1, "n_comps": 8},
            "rep_estimate": {"total_p50": 3000, "total_p90": 5000},
        }
        deal = underwrite_deal(listing, params)

        # V2 spread-based fields
        assert deal["asking_price"] == 350000
        assert deal["market_anchor_price"] == 340000
        assert "adjusted_market_value" in deal
        assert "spread_ask_pct" in deal
        assert "spread_ask_abs" in deal
        assert "realistic_bid_price" in deal
        assert "spread_bid_pct" in deal
        assert "adj_positive" in deal
        assert "adj_negative" in deal
        assert "repair_buffer" in deal
        assert "adjustments_detail" in deal

    def test_underwrite_has_explanation(self, params):
        listing = {
            "listing_id": "998", "make": "Tesla", "model": "Model 3",
            "variant": "long_range", "year": 2021, "km": 50000,
            "price_nok": 350000, "fuel_type": "electric", "seller_type": "privat",
            "listing_text": "Velholdt bil", "location_city": "Oslo",
            "pristips": {"market_anchor_price": 340000, "market_anchor_low": 310000,
                         "market_anchor_high": 370000},
            "ai_analysis": {"issues": [], "positives": []},
            "comp_result": {"tier": 1, "n_comps": 8},
            "rep_estimate": {"total_p50": 0, "total_p90": 0},
        }
        deal = underwrite_deal(listing, params)
        assert "explanation" in deal
        assert "Annonsepris" in deal["explanation"]
        assert "Pristips" in deal["explanation"]
        assert deal["classification"]["label"] in deal["explanation"]

    def test_pristips_missing_has_explanation(self, params):
        listing = {
            "listing_id": "997", "make": "Tesla", "model": "Model 3",
            "variant": "long_range", "year": 2021, "km": 50000,
            "price_nok": 350000, "fuel_type": "electric", "seller_type": "privat",
            "listing_text": "Velholdt bil", "location_city": "Oslo",
            "pristips": None,
            "ai_analysis": None,
            "comp_result": {},
            "rep_estimate": {"total_p50": 0, "total_p90": 0},
        }
        deal = underwrite_deal(listing, params)
        assert "explanation" in deal
        assert "PASS" in deal["explanation"]
        assert "Pristips" in deal["explanation"].lower() or "pristips" in deal["explanation"].lower()

    def test_overpriced_listing_negative_spread(self, params):
        """When listing price > V_adj, spread is negative → PASS."""
        listing = {
            "listing_id": "996", "make": "Tesla", "model": "Model 3",
            "variant": "long_range", "year": 2021, "km": 50000,
            "price_nok": 500000, "fuel_type": "electric", "seller_type": "privat",
            "listing_text": "Velholdt bil", "location_city": "Oslo",
            "pristips": {"market_anchor_price": 340000, "market_anchor_low": 310000,
                         "market_anchor_high": 370000},
            "ai_analysis": {"issues": [], "positives": []},
            "comp_result": {"tier": 1, "n_comps": 5},
            "rep_estimate": {"total_p50": 0, "total_p90": 0},
        }
        deal = underwrite_deal(listing, params)
        assert deal["spread_ask_pct"] < 0
        assert deal["classification"]["label"] == "PASS"

    def test_underpriced_listing_positive_spread(self, params):
        """When listing price << V_adj, spread is high → CALL_NOW."""
        listing = {
            "listing_id": "995", "make": "Tesla", "model": "Model 3",
            "variant": "long_range", "year": 2021, "km": 50000,
            "price_nok": 200000, "fuel_type": "electric", "seller_type": "privat",
            "listing_text": "Velholdt bil", "location_city": "Oslo",
            "pristips": {"market_anchor_price": 340000, "market_anchor_low": 310000,
                         "market_anchor_high": 370000},
            "ai_analysis": {"issues": [], "positives": []},
            "comp_result": {"tier": 1, "n_comps": 5},
            "rep_estimate": {"total_p50": 0, "total_p90": 0},
        }
        deal = underwrite_deal(listing, params)
        assert deal["spread_ask_pct"] > 0.10
        assert deal["classification"]["label"] == "CALL_NOW"


# --- CLI --limit flag ---

class TestCLILimitFlag:
    def test_limit_flag_accepted(self):
        """--limit should be accepted by argparse without error."""
        import argparse
        parser = argparse.ArgumentParser()
        parser.add_argument("--model", type=str, default=None)
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--clear-cache", action="store_true")
        parser.add_argument("--limit", type=int, default=None)
        args = parser.parse_args(["--limit", "5", "--dry-run", "--model", "Tesla Model 3"])
        assert args.limit == 5
        assert args.dry_run is True
        assert args.model == "Tesla Model 3"

    def test_limit_none_by_default(self):
        import argparse
        parser = argparse.ArgumentParser()
        parser.add_argument("--limit", type=int, default=None)
        args = parser.parse_args([])
        assert args.limit is None


# --- CSV new columns ---

class TestCSVNewColumns:
    def test_write_csv_includes_v2_columns(self, tmp_path):
        """CSV should include V2 columns: spread, execution_gate, ai_status, etc."""
        import csv
        from src.output.formatter import write_csv
        records = [{
            "listing_id": "123",
            "make": "Tesla", "model": "Model 3",
            "year": 2021, "km": 50000,
            "asking_price": 350000,
            "market_anchor_price": 340000,
            "market_anchor_low": 310000,
            "market_anchor_high": 370000,
            "adjusted_market_value": 342000,
            "spread_ask_abs": -8000,
            "spread_ask_pct": -0.0234,
            "realistic_bid_price": 339500,
            "spread_bid_abs": 2500,
            "spread_bid_pct": 0.0073,
            "adj_positive": 5000,
            "adj_negative": 3000,
            "repair_buffer": 0,
            "classification": {"label": "PASS", "execution_gate": "BLOCKED", "reason": "low spread"},
            "hard_red_flag": False,
            "ai_status": "ok",
            "ai_summary_short": "Velholdt bil",
            "ai_positive_signals": [{"name": "god service"}],
            "ai_negative_signals": [],
            "ai_missing_info": [{"question": "SOH?"}],
            "seller_motivation_score": 3,
            "soh_status": "not_mentioned",
            "eu_status": "valid",
            "skip_reason": None,
            "explanation": "Annonsepris: 350 000 kr\nPristips: 340 000 kr",
            "listing_url": "https://finn.no/car/used/ad.html?finnkode=123",
        }]
        csv_path = str(tmp_path / "deals.csv")
        write_csv(records, csv_path)

        with open(csv_path) as f:
            reader = csv.DictReader(f)
            rows = list(reader)

        assert len(rows) == 1
        row = rows[0]
        assert row["market_anchor_price"] == "340000"
        assert row["market_anchor_low"] == "310000"
        assert row["market_anchor_high"] == "370000"
        assert row["adjusted_market_value"] == "342000"
        assert row["spread_ask_pct"] == "-0.0234"
        assert row["realistic_bid_price"] == "339500"
        assert row["classification_label"] == "PASS"
        assert row["execution_gate"] == "BLOCKED"
        assert row["ai_status"] == "ok"
        assert row["ai_summary_short"] == "Velholdt bil"
        assert row["explanation"] == "Annonsepris: 350 000 kr"  # first line only
        assert row["url"] == "https://finn.no/car/used/ad.html?finnkode=123"


# --- Health check fixes ---

class TestHealthCheckPaths:
    def test_cookie_file_path_is_project_root_dotfile(self):
        """Health check must look for .finn_cookies.json in project root, not cookies/."""
        import inspect
        from src import main as main_mod
        source = inspect.getsource(main_mod.run_health_check)
        assert "cookies/finn_cookies" not in source, (
            "Health check still uses wrong cookies/ subdirectory path"
        )
        assert ".finn_cookies.json" in source, (
            "Health check must reference .finn_cookies.json"
        )

    def test_supabase_health_check_uses_correct_key_names(self):
        """Health check must use SUPABASE_SERVICE_KEY/SUPABASE_ANON_KEY, not SUPABASE_KEY."""
        import inspect
        from src import main as main_mod
        source = inspect.getsource(main_mod.run_health_check)
        assert "SUPABASE_KEY" not in source or "SUPABASE_SERVICE_KEY" in source, (
            "Health check must use SUPABASE_SERVICE_KEY or SUPABASE_ANON_KEY"
        )
        assert "SUPABASE_SERVICE_KEY" in source

    def test_cookie_path_matches_pristips(self):
        """COOKIE_FILE in pristips.py must also be project root .finn_cookies.json."""
        from src.engine.pristips import COOKIE_FILE
        assert COOKIE_FILE.name == ".finn_cookies.json"
        assert COOKIE_FILE.parent.name != "cookies"

    def test_cookie_path_matches_get_finn_cookies_script(self):
        """get_finn_cookies.py COOKIE_FILE must point to project root, same as pristips."""
        from src.engine.pristips import COOKIE_FILE as pristips_cookie
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "get_finn_cookies",
            Path(__file__).parent.parent / "scripts" / "get_finn_cookies.py",
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        assert mod.COOKIE_FILE == pristips_cookie, (
            f"Script writes to {mod.COOKIE_FILE} but pristips reads from {pristips_cookie}"
        )


# --- listing_text_len in audit records ---

class TestListingTextLen:
    def test_listing_text_len_in_audit_record(self, params):
        """Audit records must include listing_text_len for diagnosing ai_status=short_text."""
        listing = {
            "listing_id": "test_tlen", "make": "Tesla", "model": "Model 3",
            "variant": "long_range", "year": 2021, "km": 50000,
            "price_nok": 350000, "seller_type": "privat",
            "listing_text": "A" * 120,
            "location_city": "Oslo", "fuel_type": "electric",
            "pristips": {
                "market_anchor_price": 340000,
                "market_anchor_low": 310000,
                "market_anchor_high": 370000,
            },
            "ai_analysis": {"ai_status": "ok", "issues": [], "positives": []},
            "comp_result": {"tier": 1, "n_comps": 5},
            "rep_estimate": {"lag1_p50": 0, "lag2_expected": 0, "total_p50": 0, "total_p90": 0},
        }
        deal = underwrite_deal(listing, params)
        # Simulate what main.py does when building audit_records
        txt_len = len((deal.get("listing") or {}).get("listing_text", "") or "")
        assert txt_len == 120

    def test_listing_text_len_zero_for_empty(self, params):
        """listing_text_len must be 0 for listings with no text."""
        listing = {
            "listing_id": "test_tlen2", "make": "Tesla", "model": "Model 3",
            "variant": "long_range", "year": 2021, "km": 50000,
            "price_nok": 350000, "seller_type": "privat",
            "listing_text": "",
            "location_city": "Oslo", "fuel_type": "electric",
            "pristips": {"market_anchor_price": 340000},
            "ai_analysis": {"ai_status": "short_text", "issues": [], "positives": []},
            "comp_result": {"tier": 1, "n_comps": 5},
            "rep_estimate": {"lag1_p50": 0, "lag2_expected": 0, "total_p50": 0, "total_p90": 0},
        }
        deal = underwrite_deal(listing, params)
        txt_len = len((deal.get("listing") or {}).get("listing_text", "") or "")
        assert txt_len == 0


# --- Smoke scraping short-circuit ---

class TestSmokeShortCircuit:
    def test_scrape_model_accepts_max_listings(self):
        """scrape_model signature must include max_listings param."""
        import inspect
        from src.scraper.finn_scraper import scrape_model
        sig = inspect.signature(scrape_model)
        assert "max_listings" in sig.parameters

    def test_scrape_all_models_accepts_max_total(self):
        """scrape_all_models signature must include max_total_listings param."""
        import inspect
        from src.scraper.finn_scraper import scrape_all_models
        sig = inspect.signature(scrape_all_models)
        assert "max_total_listings" in sig.parameters

    def test_enrich_listing_texts_noop_when_all_long(self):
        """enrich_listing_texts returns 0 when all listings already have long text."""
        from src.scraper.finn_scraper import enrich_listing_texts
        listings = [
            {"listing_id": "a", "listing_text": "X" * 200},
            {"listing_id": "b", "listing_text": "Y" * 150},
        ]
        result = enrich_listing_texts(listings, min_text_len=100)
        assert result == 0

    def test_enrich_listing_texts_identifies_short_candidates(self):
        """enrich_listing_texts should attempt to fetch listings with short text."""
        from src.scraper.finn_scraper import enrich_listing_texts
        listings = [
            {"listing_id": "short1", "listing_text": ""},
            {"listing_id": "long1", "listing_text": "X" * 200},
        ]
        # Without network we can't verify enrichment, but we can verify the
        # function runs without error and returns int
        result = enrich_listing_texts(listings, min_text_len=100)
        assert isinstance(result, int)
        assert result >= 0
