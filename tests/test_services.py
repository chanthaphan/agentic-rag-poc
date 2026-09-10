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
    """The service has no currency field: the code lives in Family, and rates are *Rates with padding."""
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


# a branch row exactly as the locator returns it
LIVE_BRANCH = {"BranchName": "สาขาซีคอนสแควร์", "BranchNo": "0232", "Address1": "904 หมู่ 6 ถนนศรีนครินทร์",
               "Address2": "หนองบอน", "Address3": "ประเวศ", "MicroBranchHours": "ทุกวัน 11.00 น. - 19.00 น.",
               "Province": "กรุงเทพมหานคร", "Postcode": "10250", "ATM": "x", "ATMPlus": "", "Branch": "x",
               "BusinessCenter": ""}


def test_normalize_places_reads_the_live_shape():
    """The address arrives split across Address1..3, and services are 'x' columns rather than a list."""
    p = SV.normalize_places([LIVE_BRANCH])[0]
    assert p.name == "สาขาซีคอนสแควร์" and p.branch_no == "0232" and p.postcode == "10250"
    assert p.address == "904 หมู่ 6 ถนนศรีนครินทร์ หนองบอน ประเวศ"
    assert p.hours == "ทุกวัน 11.00 น. - 19.00 น."
    assert p.services == ["ATM", "Branch"]  # only the columns actually marked x
    assert p.lat is None  # the locator returns no coordinates; it orders by the ones we send instead


def test_normalize_places_is_tolerant(settings):
    rows = [{"Name": "Silom 2", "FullAddress": "2 Silom Rd", "Latitude": "13.7", "Longitude": "100.5",
             "Distance": "1.2", "Tel": "02-000-0000"}]
    got = SV.normalize_places(rows)
    assert got[0].name == "Silom 2" and got[0].address == "2 Silom Rd"
    assert got[0].lat == 13.7 and got[0].distance_km == 1.2 and got[0].phone == "02-000-0000"
    assert SV.normalize_places([{"nothing": "useful"}]) == []  # no name and no address: not a place
    assert SV.normalize_places("boom") == []


def test_find_branch_returns_the_nearest(settings, monkeypatch):
    rows = [{**LIVE_BRANCH, "BranchName": n, "BranchNo": no, "Latitude": la, "Longitude": "100.6"}
            for n, no, la in (("A", "1", "13.70"), ("B", "2", "13.75"), ("C", "3", "13.90"))]
    monkeypatch.setattr(SV, "search_places_raw", lambda s, province, *a, **k: rows if province == "กรุงเทพมหานคร" else [])
    out = SV.find_branch(settings, 13.7, 100.6, limit=2)
    # ranked by distance from the caller's point, computed from each row's own coordinates
    assert out["found"] and [b["name"] for b in out["branches"]] == ["A", "B"]
    assert out["near"] == {"lat": 13.7, "lon": 100.6}
    assert "can change" in out["disclaimer"]
    assert out["branches"][0]["services"] == ["ATM", "Branch"]
    assert out["branches"][0]["lat"] and out["branches"][0]["distance_km"] is not None  # usable for a map pin
    assert all("phone" not in b for b in out["branches"])  # genuinely blank fields are dropped, not sent as nulls


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
    assert SV.resolve_kind("fcd") == "FCD" and SV.resolve_kind("foreign currency deposit") == "FCD"
    assert SV.resolve_kind("wealth lounge") == "BEV" and SV.resolve_kind("สำนักธุรกิจ") == "BUC"
    # the locator says Wealth Lounge; customers and our own About-Us pages say Wealth Center / เวลท์
    for said in ("Wealth Center", "wealth centre", "wealth management", "เวลท์เซ็นเตอร์", "เวลท์"):
        assert SV.resolve_kind(said) == "BEV", said
    assert SV.resolve_kind("CDM") == "CDM"  # a code we have not seen yet still reaches the service


def test_an_unknown_service_phrase_falls_back_to_branches(settings, monkeypatch):
    """A phrase the locator has no type for would 404; searching branches at least answers the customer."""
    assert SV.resolve_kind("safe deposit box") == "BRC"
    seen = []
    monkeypatch.setattr(SV, "search_places_raw",
                        lambda s, province, lat, lon, **kw: seen.append(kw.get("kind")) or [])
    SV.find_branch(settings, 13.7, 100.6, kind="safe deposit box")
    assert set(seen) == {"BRC"}


def test_find_branch_reports_the_type_it_searched(settings, monkeypatch):
    """The answer must be able to say 'no Wealth Lounge nearby' rather than sounding like a branch failure."""
    monkeypatch.setattr(SV, "search_places_raw", lambda s, province, lat, lon, **kw: [])
    out = SV.find_branch(settings, 13.7, 100.6, kind="Wealth Center")
    assert out["found"] is False and out["kind"] == "BEV" and "BEV" in out["say"]

    monkeypatch.setattr(SV, "search_places_raw", lambda s, province, lat, lon, **kw: [
        {"BranchName": "สินธร", "BranchNo": "9001", "Latitude": "13.74", "Longitude": "100.54"}])
    out2 = SV.find_branch(settings, 13.7, 100.6, kind="เวลท์")
    assert out2["found"] and out2["kind"] == "BEV"


def test_currency_names_resolve_to_iso_codes():
    """Customers ask for 'เยน', not 'JPY'; the tool must not answer 'not in today's list' for that."""
    for word in ("เยน", "yen", "JPY", "jpy", "อัตราเงินเยน", "Japanese Yen"):
        assert SV.resolve_currency(word) == "JPY", word
    assert SV.resolve_currency("ดอลลาร์") == "USD" and SV.resolve_currency("euro") == "EUR"
    assert SV.resolve_currency("ริงกิต") == "MYR" and SV.resolve_currency("หยวน") == "CNY"
    assert SV.resolve_currency("xyz") == "XYZ"  # unknown still reaches the lookup, which reports it honestly


def test_fx_rate_accepts_a_currency_name(settings, monkeypatch):
    monkeypatch.setattr(SV, "fx_latest_raw", lambda s: [
        {**LIVE_ROW, "Description": "Japan", "Family": "JPY", "FamilyLong": "Japanese Yen",
         "BuyingRates": "0.2150", "SellingRates": "0.2250"}])
    out = SV.fx_rate(settings, "เยน")
    assert out["found"] and out["currency"] == "JPY" and out["rates"][0]["buying"] == 0.215


def test_unknown_currency_says_what_was_asked_for(settings, monkeypatch):
    monkeypatch.setattr(SV, "fx_latest_raw", lambda s: [LIVE_ROW])
    out = SV.fx_rate(settings, "เงินดาวอังคาร")
    assert out["found"] is False and out["asked_for"] == "เงินดาวอังคาร" and out["available"] == ["USD"]


# ---- deriving the province the locator requires ----
def test_province_is_derived_from_coordinates():
    from bankrag import provinces as PROV

    assert PROV.nearest(13.7563, 100.5018, 1) == ["กรุงเทพมหานคร"]
    assert PROV.nearest(18.7883, 98.9853, 1) == ["เชียงใหม่"]
    assert PROV.nearest(7.8804, 98.3923, 1) == ["ภูเก็ต"]
    # a point on the Bangkok/Samut Prakan border returns both, which is why two are searched
    assert set(PROV.nearest(13.697057, 100.645585, 2)) == {"สมุทรปราการ", "กรุงเทพมหานคร"}
    assert round(PROV.haversine_km(13.7563, 100.5018, 18.7883, 98.9853)) == 582


def test_find_branch_merges_two_provinces_and_sorts_by_real_distance(settings, monkeypatch):
    """The locator 404s without a province, so we derive two and rank the merged rows ourselves."""
    calls = []

    def fake(s, province, lat, lon, **kw):
        calls.append(province)
        if province == "สมุทรปราการ":
            return [{"BranchName": "far", "BranchNo": "1", "Address1": "a", "Latitude": "13.60", "Longitude": "100.60"}]
        return [{"BranchName": "near", "BranchNo": "2", "Address1": "b", "Latitude": "13.6975", "Longitude": "100.6455"},
                {"BranchName": "far", "BranchNo": "1", "Address1": "a", "Latitude": "13.60", "Longitude": "100.60"}]

    monkeypatch.setattr(SV, "search_places_raw", fake)
    out = SV.find_branch(settings, 13.697057, 100.645585, limit=5)
    assert len(calls) == 2 and set(calls) == {"สมุทรปราการ", "กรุงเทพมหานคร"}
    names = [b["name"] for b in out["branches"]]
    assert names == ["near", "far"]  # ordered by true distance, not by which province answered first
    assert out["branches"][0]["distance_km"] < 0.2  # metres away, computed from the row's own coordinates
    assert names.count("far") == 1  # the same branch from both provinces is returned once


def test_an_explicit_province_is_not_second_guessed(settings, monkeypatch):
    calls = []
    monkeypatch.setattr(SV, "search_places_raw", lambda s, province, *a, **k: calls.append(province) or [])
    SV.find_branch(settings, 13.7, 100.6, province="เชียงใหม่")
    assert calls == ["เชียงใหม่"]


def test_a_province_alone_is_enough_to_search(settings, monkeypatch):
    """The customer says where they are instead of sharing a location: search from that province's centre."""
    seen = {}

    def fake(s, province, lat, lon, **kw):
        seen.update(province=province, lat=lat, lon=lon)
        return [{"BranchName": "สีลม", "BranchNo": "1", "Latitude": "13.72", "Longitude": "100.53"}]

    monkeypatch.setattr(SV, "search_places_raw", fake)
    out = SV.find_branch(settings, province="กรุงเทพ", kind="wealth center")
    assert out["found"] and out["kind"] == "BEV"
    assert seen["province"] == "กรุงเทพมหานคร"  # the short form the customer used is not what the locator wants
    assert (round(seen["lat"], 4), round(seen["lon"], 4)) == (13.7563, 100.5018)


def test_no_location_and_no_province_asks_instead_of_guessing(settings, monkeypatch):
    monkeypatch.setattr(SV, "search_places_raw",
                        lambda *a, **k: pytest.fail("must not call the locator without somewhere to search"))
    out = SV.find_branch(settings, province="ที่ไหนสักแห่ง")
    assert out["found"] is False and "province" in out["say"] and "invent" in out["say"]
    assert SV.find_branch(settings)["found"] is False


ROWS_BKK = [
    {"BranchName": "สาขาสะพานผ่านฟ้า", "BranchNo": "0101", "Latitude": "13.7570", "Longitude": "100.5020"},
    {"BranchName": "สาขาซีคอนสแควร์", "BranchNo": "0232", "Latitude": "13.6942", "Longitude": "100.6482",
     "MicroBranchHours": "ทุกวัน 11.00 น. - 19.00 น.", "Tel": "02-721-8646-50"},
    {"BranchName": "สาขาซีคอน บางแค", "BranchNo": "0233", "Latitude": "13.6960", "Longitude": "100.4090"},
]


def test_a_named_branch_is_found_however_far_it_is(settings, monkeypatch):
    """The province returns every branch; ranking by distance and cutting to `limit` used to lose the named one."""
    monkeypatch.setattr(SV, "search_places_raw", lambda s, province, lat, lon, **kw: list(ROWS_BKK))
    out = SV.find_branch(settings, 13.7563, 100.5018, province="กรุงเทพ", name="ซีคอนสแควร์", limit=3)
    assert out["found"] and [b["name"] for b in out["branches"]] == ["สาขาซีคอนสแควร์"]
    assert out["branches"][0]["hours"] == "ทุกวัน 11.00 น. - 19.00 น."
    # "สาขา" and spacing are how the customer types it, not a different branch
    for said in ("สาขาซีคอนสแควร์", " ซีคอนสแควร์ ", "ซีคอน"):
        assert SV.find_branch(settings, 13.75, 100.50, province="กรุงเทพ", name=said)["found"], said


def test_a_name_we_cannot_find_never_denies_the_branch_exists(settings, monkeypatch):
    monkeypatch.setattr(SV, "search_places_raw", lambda s, province, lat, lon, **kw: list(ROWS_BKK))
    out = SV.find_branch(settings, 13.7563, 100.5018, province="กรุงเทพ", name="ท่าแพ")
    assert out["found"] is False and out["looked_for"] == "ท่าแพ"
    assert "no such branch" in out["say"] and "which province" in out["say"]


def test_the_location_travels_with_the_question(monkeypatch):
    """The concierge relays the question verbatim, so the coordinates ride along with it rather than in a note it
    may or may not copy - the failure that sent a customer in Prawet to branches in the old town."""
    from bankrag.chat import question_with_location

    plain = "สาขาซีคอนสแควร์มีบริการเปิดบัญชีไหม"
    assert question_with_location(plain, None) == plain
    with_loc = question_with_location(plain, (13.697058, 100.645586))
    assert with_loc.startswith(plain)  # the customer's own words come first and are not altered
    assert "13.697058" in with_loc and "100.645586" in with_loc
    assert "never be shown" in with_loc


def test_an_echoed_location_line_never_reaches_the_customer():
    from bankrag.chat import strip_markers

    said = strip_markers("สาขาใกล้ที่สุดคือสาขาซีคอนสแควร์ค่ะ\n[customer location: latitude 13.697058, longitude 100.645586 - pass this line on]")
    assert "13.697058" not in said and "customer location" not in said
    assert "ซีคอนสแควร์" in said


# ---- the rate table's own field names ----
LIVE_JPY = {"ID": "31155", "Description": "Japan (:100)", "Family": "JPY", "FamilyLong": "Japanese Yen",
            "BuyingRates": "20.91     ", "SellingRates": "22.08     ", "TT": "21.16500  ",
            "Ddate": "9/09/2026", "Update": "2", "DTime": "13:10     "}
LIVE_VND = {"Description": "Vietnam (:1000)", "Family": "VND", "FamilyLong": "Vietnam Dong",
            "BuyingRates": "1.06", "SellingRates": "1.33", "Ddate": "9/09/2026", "DTime": "13:10"}


def test_the_iso_code_comes_from_family_not_from_the_country_name(settings, monkeypatch):
    """Description is a country ("Japan (:100)"), so reading the code from it gave JAP and lost the yen rate."""
    monkeypatch.setattr(SV, "fx_latest_raw", lambda s: [LIVE_JPY, LIVE_VND,
                                                        {**LIVE_JPY, "Family": "MYR", "Description": "Malaysia"}])
    assert {r.currency for r in SV.normalize_fx(SV.fx_latest_raw(settings))} == {"JPY", "VND", "MYR"}
    out = SV.fx_rate(settings, "เยน")
    assert out["found"] and out["currency"] == "JPY" and out["rates"][0]["buying"] == 20.91


def test_a_rate_quoted_per_100_converts_an_amount_correctly(settings, monkeypatch):
    """30,000 yen costs 6,624 baht, not 662,400: the bank quotes the yen per 100 units."""
    monkeypatch.setattr(SV, "fx_latest_raw", lambda s: [LIVE_JPY, LIVE_VND])
    jpy = SV.fx_rate(settings, "JPY")["rates"][0]
    assert jpy["units"] == 100 and jpy["baht_per_1"]["selling"] == 0.2208
    assert round(30000 * jpy["baht_per_1"]["selling"], 2) == 6624.00
    vnd = SV.fx_rate(settings, "VND")["rates"][0]
    assert vnd["units"] == 1000 and vnd["baht_per_1"]["buying"] == 0.00106
    usd = SV.fx_rate(settings, "USD")
    assert usd["found"] is False  # not in this stub, and the tool says so rather than inventing one
