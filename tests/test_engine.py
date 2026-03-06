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
from src.engine.classifier import classify_deal
from src.engine.battery_soh import calculate_soh_scenarios
from src.output.formatter import build_audit_record

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
        assert _get_next_page_url(soup, 'https://www.finn.no/mobility/search/car?q=tesla&page=2').endswith('page=3')

    def test_no_next_page_returns_none(self):
        from bs4 import BeautifulSoup
        soup = BeautifulSoup('<html></html>', 'html.parser')
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
        assert result["tier"] in (1, 2)  # tier depends on km spread of fixture
        assert result["n_comps"] >= 8

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
        # With expansion, range should be wider than raw percentiles
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
        assert result["adjusted_p10"] < fmv["raw_p10"]  # negative amplified
        assert result["adjusted_p90"] > fmv["raw_p90"]  # positive dampened


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
        assert price_factor(400000, 400000) == 1.00  # ratio=1.0 falls in [1.00, 1.05)

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
    def test_green_deal(self, params):
        profit_result = {"scenarios": {"80pct_loan": {"profit_base": 25000, "profit_bear": 5000}}}
        comp_result = {"tier": 1, "n_comps": 12, "flags": []}
        listing = {"dq_score": 0.90}
        result = classify_deal(profit_result, comp_result, listing, params)
        assert result["classification"] == "KONTAKT"

    def test_hard_pass(self, params):
        profit_result = {"scenarios": {"80pct_loan": {"profit_base": -20000, "profit_bear": -50000}}}
        comp_result = {"tier": 2, "n_comps": 10, "flags": []}
        listing = {"dq_score": 0.80}
        result = classify_deal(profit_result, comp_result, listing, params)
        assert result["classification"] == "HARD PASS"

    def test_low_comps_flag(self, params):
        profit_result = {"scenarios": {"80pct_loan": {"profit_base": 15000, "profit_bear": 0}}}
        comp_result = {"tier": 3, "n_comps": 4, "flags": ["INSUFFICIENT_COMPS"]}
        listing = {"dq_score": 0.90}
        result = classify_deal(profit_result, comp_result, listing, params)
        assert "TYNT COMP-GRUNNLAG" in result["flags"]
        assert "FOR FA COMPS" in result["flags"]

    def test_loan_recommendation(self, params):
        profit_result = {"scenarios": {"80pct_loan": {"profit_base": 25000, "profit_bear": 15000}}}
        comp_result = {"tier": 1, "n_comps": 12, "flags": []}
        listing = {"dq_score": 0.90}
        result = classify_deal(profit_result, comp_result, listing, params)
        assert "80%" in result["loan_recommendation"]


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


class TestAuditNaming:
    def test_record_contains_consistent_price_naming(self, sample_listing, params):
        comp_result = {"tier": 1, "n_comps": 10, "comp_ids": [], "median_price": 380000, "transaction_median": 360000}
        fmv_raw = {"raw_p10": 340000, "raw_p50": 390000, "raw_p90": 440000}
        fmv_adjusted = {"adjusted_p10": 335000, "adjusted_p50": 385000, "adjusted_p90": 445000, "adjustments": []}
        rep = {"lag1_issues": [], "lag2_issues": [], "correlation_factor": 1.0, "uncertainty_multiplier": 1.0, "total_p50": 5000, "total_p90": 9000}
        days = {"p50": 25, "p90": 55, "bull": 15}
        profit_result = calculate_profit(sample_listing, fmv_adjusted, {"total_p50": 5000, "total_p90": 9000}, days, params)
        mpp_data = calculate_mpp(fmv_adjusted, {"total_p50": 5000, "total_p90": 9000}, {"p50": 25, "p90": 55}, params)
        classification = classify_deal({"scenarios": {"80pct_loan": {"profit_base": 10000, "profit_bear": 1000}}}, {"tier": 1, "n_comps": 10, "flags": []}, {"dq_score": 0.9}, params)
        record = build_audit_record(sample_listing, comp_result, fmv_raw, fmv_adjusted, rep, days, profit_result, mpp_data, classification)
        assert "listing_price_nok" in record
        assert "assumed_entry_price" in record
        assert "required_discount_to_mpp" in record

