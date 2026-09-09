from pathlib import Path

import pytest

from bankrag.config import Settings
from bankrag import services as SV

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def settings():
    s = Settings.load(ROOT)
    s.bbl_api_subscription = "test-key"
    s.bbl_api_base = "https://example.test/api"
    return s


def test_disabled_without_a_key():
    s = Settings.load(ROOT)
    s.bbl_api_subscription = ""
    assert not SV.configured(s)
    with pytest.raises(SV.ServiceError, match="not set"):
        SV.fx_latest_raw(s)


# one row exactly as GetLatestfxrates returns it (trailing spaces and all)
LIVE_ROW = {"ID": "31155", "Description": "USD: 1-2", "Family": "USD1", "FamilyLong": "US Dollar 1",
            "BuyingRates": "31.55     ", "SellingRates": "33.09   ", "SightBill": "", "Bill_DD_TT": "",
            "TT": "", "Ddate": "9/09/2026", "Update": "2", "DTime": "13:10     "}


def test_normalize_fx_reads_the_live_shape():
    """The service has no currency field: the code lives in Description / Family, and rates are *Rates with padding."""
    got = SV.normalize_fx([LIVE_ROW])
    assert len(got) == 1
    r = got[0]
    assert r.currency == "USD" and r.label == "USD: 1-2" and r.name == "US Dollar 1"
    assert r.buying == 31.55 and r.selling == 33.09
    assert r.tt is None and r.sight_bill is None  # blank strings, not zeros
    assert r.as_of == "9/09/2026 13:10 (round 2)"


def test_normalize_fx_is_tolerant_of_other_field_names():
    rows = [{"Family": "EUR", "FamilyLong": "Euro", "BuyingRates": "38.1", "SellingRates": "39.2", "TT": "38.9"},
            {"CurrencyCode": "JPY", "Buying": "0.2350", "Selling": "0.2450"}]
    got = SV.normalize_fx(rows)
    assert [r.currency for r in got] == ["EUR", "JPY"]
    assert got[0].tt == 38.9 and got[1].buying == 0.235
    assert len(SV.normalize_fx({"result": rows})) == 2  # a wrapped list
    assert SV.normalize_fx({"nope": 1}) == [] and SV.normalize_fx("boom") == []
    assert SV.normalize_fx([{"foo": "bar"}]) == []  # nothing that looks like a currency


def test_fx_rate_returns_every_denomination(settings, monkeypatch):
    """USD comes back as several note families at different rates; the agent needs all of them."""
    rows = [LIVE_ROW,
            {**LIVE_ROW, "Description": "USD: 50-100", "FamilyLong": "US Dollar 50", "BuyingRates": "32.05",
             "SellingRates": "33.09", "TT": "32.20"}]
    monkeypatch.setattr(SV, "fx_latest_raw", lambda s: rows)
    out = SV.fx_rate(settings, "usd")
    assert out["found"] and out["currency"] == "USD" and out["as_of"] == "9/09/2026 13:10 (round 2)"
    assert [r["denomination"] for r in out["rates"]] == ["USD: 1-2", "USD: 50-100"]
    assert out["rates"][0]["buying"] == 31.55 and out["rates"][1]["buying"] == 32.05
    assert out["rates"][1]["telegraphic_transfer"] == 32.20 and "telegraphic_transfer" not in out["rates"][0]
    assert "branch rate at the time" in out["disclaimer"] and "buying =" in out["meaning"]


def test_fx_rate_missing_currency_lists_what_is_available(settings, monkeypatch):
    monkeypatch.setattr(SV, "fx_latest_raw", lambda s: [LIVE_ROW, {"Description": "EUR", "BuyingRates": "38"}])
    out = SV.fx_rate(settings, "ZWL")
    assert out["found"] is False and out["available"] == ["EUR", "USD"] and "not in today" in out["say"]


def test_fx_rate_needs_no_second_call_for_the_timestamp(settings, monkeypatch):
    """as_of comes off the rate row, so a flaky last-update endpoint cannot cost us the rate."""
    def boom(s):
        raise SV.ServiceError("last-update down")
    monkeypatch.setattr(SV, "fx_latest_raw", lambda s: [LIVE_ROW])
    monkeypatch.setattr(SV, "fx_last_update", boom)
    out = SV.fx_rate(settings, "USD")
    assert out["found"] and out["as_of"] == "9/09/2026 13:10 (round 2)"


def test_last_update_parses_the_python_repr_the_endpoint_returns(settings, monkeypatch):
    monkeypatch.setattr(SV, "_get", lambda s, p: "[{'Update': '2', 'Time': '13:10     ', 'Day': '09/09/2026'}]")
    assert SV.fx_last_update(settings) == "09/09/2026 13:10 (round 2)"
    monkeypatch.setattr(SV, "_get", lambda s, p: [{"Day": "10/09/2026", "Time": "09:30", "Update": "1"}])
    assert SV.fx_last_update(settings) == "10/09/2026 09:30 (round 1)"
    monkeypatch.setattr(SV, "_get", lambda s, p: "not a list at all")
    assert SV.fx_last_update(settings) == "not a list at all"


def test_endpoint_paths(settings, monkeypatch):
    seen = {}
    def record(s, p):
        seen["path"] = p
        return []
    monkeypatch.setattr(SV, "_get", record)
    from datetime import date

    SV.fx_rates_raw(settings, date(2026, 9, 5), 2, "th")
    assert seen["path"] == "exchangerateservice/Getfxrates/05/09/2026/2/th"
    SV.provinces(settings, "th")
    assert seen["path"] == "locationsearchservice/GetProvinceTh"
    SV.countries(settings, "en")
    assert seen["path"] == "locationsearchservice/GetCountryEn"


# ---- branch locator ----
def test_branch_search_builds_the_captured_path(settings, monkeypatch):
    """Matches the real Locate Us call: /SearchThaiLandThWithLocation/{province}/{district}/{lat}/{lon}/{kind}."""
    seen = {}
    def record(s, p):
        seen["path"] = p
        return []
    monkeypatch.setattr(SV, "_get", record)
    SV.search_places_raw(settings, "กรุงเทพมหานคร", 13.697057591968564, 100.64558570030171)
    assert seen["path"] == (
        "locationsearchservice/SearchThaiLandThWithLocation/"
        "%E0%B8%81%E0%B8%A3%E0%B8%B8%E0%B8%87%E0%B9%80%E0%B8%97%E0%B8%9E%E0%B8%A1%E0%B8%AB%E0%B8%B2%E0%B8%99%E0%B8%84%E0%B8%A3"
        "/0/13.697057591968564/100.64558570030171/BRC")
    SV.search_places_raw(settings, "Bangkok", 13.0, 100.0, lang="en", district="5", kind="ATM")
    assert seen["path"] == "locationsearchservice/SearchThaiLandEnWithLocation/Bangkok/5/13.0/100.0/ATM"


def test_normalize_places_is_tolerant(settings):
    rows = [{"BranchName": "สาขาสีลม", "Address": "1 ถนนสีลม", "Province": "กรุงเทพมหานคร",
             "Latitude": "13.7", "Longitude": "100.5", "Distance": "1.2", "Tel": "02-000-0000"},
            {"Name": "Silom 2", "FullAddress": "2 Silom Rd"}]
    got = SV.normalize_places(rows)
    assert [p.name for p in got] == ["สาขาสีลม", "Silom 2"]
    assert got[0].lat == 13.7 and got[0].distance_km == 1.2 and got[0].phone == "02-000-0000"
    assert SV.normalize_places([{"nothing": "useful"}]) == []  # no name and no address: not a place
    assert SV.normalize_places("boom") == []


def test_find_branch_returns_the_nearest(settings, monkeypatch):
    monkeypatch.setattr(SV, "search_places_raw", lambda *a, **k: [
        {"BranchName": "A", "Address": "1", "Distance": "0.4"},
        {"BranchName": "B", "Address": "2", "Distance": "1.1"},
        {"BranchName": "C", "Address": "3", "Distance": "9.9"}])
    out = SV.find_branch(settings, 13.7, 100.6, limit=2)
    assert out["found"] and [b["name"] for b in out["branches"]] == ["A", "B"]
    assert out["near"] == {"lat": 13.7, "lon": 100.6}
    assert "can change" in out["disclaimer"]
    assert all("lat" not in b for b in out["branches"])  # blank fields are dropped, not sent as nulls


def test_find_branch_when_nothing_comes_back_or_it_fails(settings, monkeypatch):
    monkeypatch.setattr(SV, "search_places_raw", lambda *a, **k: [])
    out = SV.find_branch(settings, 13.7, 100.6)
    assert out["found"] is False and "province or district" in out["say"]

    def boom(*a, **k):
        raise SV.ServiceError("503 from the bank API")
    monkeypatch.setattr(SV, "search_places_raw", boom)
    out2 = SV.find_branch(settings, 13.7, 100.6)
    assert out2["found"] is False and "Locate Us" in out2["say"] and "503" in out2["error"]


def test_kind_codes_map_and_pass_through():
    assert SV.resolve_kind("branch") == "BRC" and SV.resolve_kind("สาขา") == "BRC"
    assert SV.resolve_kind("atm") == "ATM" and SV.resolve_kind("ATM Plus") == "ATMPLUS"
    assert SV.resolve_kind("") == "BRC"
    assert SV.resolve_kind("exchange") == "FXB" and SV.resolve_kind("แลกเงิน") == "FXB"
    assert SV.resolve_kind("CDM") == "CDM"  # a code we have not seen yet still reaches the service
