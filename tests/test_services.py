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


def test_normalize_fx_is_tolerant_of_field_names():
    # a list straight from the endpoint
    rows = [{"CurrencyCode": "usd", "CurrencyName": "US Dollar", "Buying": "35.50", "Selling": "36,100.00", "Unit": "1"},
            {"currency": "EUR", "buyingRate": 38.1, "sellingRate": 39.2}]
    got = SV.normalize_fx(rows)
    assert [r.currency for r in got] == ["USD", "EUR"]
    assert got[0].name == "US Dollar" and got[0].buying == 35.5 and got[0].selling == 36100.0
    assert got[1].buying == 38.1 and got[1].selling == 39.2
    # a wrapped shape (list nested under some key)
    assert len(SV.normalize_fx({"result": rows, "asOf": "x"})) == 2
    # junk degrades to empty, not a crash
    assert SV.normalize_fx({"nope": 1}) == [] and SV.normalize_fx("boom") == []
    # a row with no recognisable currency code is skipped
    assert SV.normalize_fx([{"foo": "bar"}]) == []


def test_fx_rate_found(settings, monkeypatch):
    monkeypatch.setattr(SV, "fx_latest_raw", lambda s: [{"Currency": "USD", "Buying": "35.5", "Selling": "36.0"}])
    monkeypatch.setattr(SV, "fx_last_update", lambda s: "10/09/2026 09:30")
    out = SV.fx_rate(settings, "usd")
    assert out["found"] and out["currency"] == "USD" and out["buying"] == 35.5 and out["selling"] == 36.0
    assert out["as_of"] == "10/09/2026 09:30" and "confirm with Bangkok Bank" in out["disclaimer"]


def test_fx_rate_missing_currency_lists_what_is_available(settings, monkeypatch):
    monkeypatch.setattr(SV, "fx_latest_raw", lambda s: [{"Currency": "USD"}, {"Currency": "EUR"}])
    monkeypatch.setattr(SV, "fx_last_update", lambda s: "x")
    out = SV.fx_rate(settings, "ZWL")
    assert out["found"] is False and out["available"] == ["USD", "EUR"]


def test_fx_rate_survives_a_last_update_failure(settings, monkeypatch):
    monkeypatch.setattr(SV, "fx_latest_raw", lambda s: [{"Currency": "USD", "Buying": 35.5}])
    def boom(s):
        raise SV.ServiceError("last-update down")
    monkeypatch.setattr(SV, "fx_last_update", boom)
    out = SV.fx_rate(settings, "USD")
    assert out["found"] and out["as_of"] == ""  # the rate still comes back


def test_last_update_handles_string_or_wrapped(settings, monkeypatch):
    monkeypatch.setattr(SV, "_get", lambda s, p: "10/09/2026 09:30")
    assert SV.fx_last_update(settings) == "10/09/2026 09:30"
    monkeypatch.setattr(SV, "_get", lambda s, p: {"lastUpdate": "10/09/2026 10:00"})
    assert SV.fx_last_update(settings) == "10/09/2026 10:00"


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
