"""Live Bangkok Bank services: today's foreign-exchange rates and the branch locator.

These are the public JSON APIs the bangkokbank.com website calls for its FX and Locate-Us pages, behind Azure API
Management. Unlike the knowledge base (static product documents), these are live lookups, so an agent must call them
at answer time rather than retrieve them - a rate quoted from a document would be stale and, for a bank, wrong.

Auth is the `Ocp-Apim-Subscription-Key` the site sends with its own calls (Settings.bbl_api_subscription, from
BBL_API_KEY). The POC uses the site's own value; swap in a dedicated POC subscription for anything beyond that. The
site sits behind Akamai bot management, so requests use curl_cffi's Chrome impersonation when it is installed, the same
as the crawler.

The response shapes are normalised defensively: the raw JSON is always available, and the normalisers pull the fields
we are confident about and fall back to returning the raw item, so an upstream shape change degrades to "show less"
rather than a crash. `bankrag services probe` prints the live shapes for confirmation.
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import date as _date
from typing import Any, Optional

from . import provinces as PROV
from .config import Settings

log = logging.getLogger("bankrag.services")
FX_SERVICE = "exchangerateservice"
LOC_SERVICE = "locationsearchservice"
_TIMEOUT = 40


class ServiceError(RuntimeError):
    """A live-service call failed (not configured, HTTP error, or unparseable body)."""


def configured(settings: Settings) -> bool:
    return bool(settings.bbl_api_subscription)


def _get(settings: Settings, path: str) -> Any:
    """GET {base}/{path} with the subscription header; return parsed JSON or raise ServiceError."""
    if not settings.bbl_api_subscription:
        raise ServiceError("BBL_API_KEY is not set: the FX and branch services are disabled")
    url = f"{settings.bbl_api_base}/{path.lstrip('/')}"
    headers = {
        "Ocp-Apim-Subscription-Key": settings.bbl_api_subscription,
        "Accept": "application/json",
        "Accept-Language": "th,en;q=0.8",
        "Referer": "https://www.bangkokbank.com/",
    }
    t0 = time.perf_counter()
    try:
        from curl_cffi import requests as creq  # type: ignore

        r = creq.get(url, impersonate="chrome", timeout=_TIMEOUT, headers=headers)
        status, text = r.status_code, r.text
    except ImportError:
        import requests

        r = requests.get(url, timeout=_TIMEOUT, headers=headers)
        status, text = r.status_code, r.text
    # every live lookup leaves a line: for a bank POC "did it really call the bank, or did the model make it up" has to
    # be answerable from the logs rather than inferred. No key, no customer data - just what was fetched and what came back.
    log.info("bank api GET %s -> %s in %d ms, %d bytes", path, status, int((time.perf_counter() - t0) * 1000), len(text or ""))
    if status == 401:
        raise ServiceError("401 from the bank API: the subscription key is missing, wrong or expired")
    if status >= 400:
        raise ServiceError(f"{status} from {path}: {text[:200]}")
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise ServiceError(f"non-JSON response from {path}: {text[:200]}") from e


# ---------------- foreign exchange ----------------
@dataclass
class FxRate:
    """One row of the bank's rate table.

    The service returns a row per denomination family, not per currency: USD alone comes back as 'USD: 1-2',
    'USD: 5-20', 'USD: 50-100', which really do carry different bank-note rates, so `label` matters to the answer."""

    currency: str  # ISO code, read from Family ("JPY", "USD50")
    units: int = 1  # the rate is baht per this many units: 100 for JPY, 1000 for VND, 1 for most
    label: str = ""  # the denomination family as the bank words it, e.g. "USD: 50-100"
    name: str = ""  # FamilyLong, e.g. "US Dollar 50-100"
    buying: Optional[float] = None  # bank buys the notes from the customer
    selling: Optional[float] = None  # bank sells the notes to the customer
    tt: Optional[float] = None  # telegraphic transfer, used for remittances rather than cash
    sight_bill: Optional[float] = None
    as_of: str = ""  # date + time + round, straight off the row


_CCY_KEYS = ("CurrencyCode", "currencyCode", "Currency", "currency", "Code", "code", "Abbreviation", "curr")
_NAME_KEYS = ("FamilyLong", "CurrencyName", "currencyName", "Name", "name", "Description", "description")
_BUY_KEYS = ("BuyingRates", "Buying", "buying", "BuyingRate", "buyingRate", "Buy", "buy")
_SELL_KEYS = ("SellingRates", "Selling", "selling", "SellingRate", "sellingRate", "Sell", "sell")
_CCY_RE = __import__("re").compile(r"^\s*([A-Za-z]{3})")


def _currency_of(row: dict) -> str:
    """The ISO code.

    `Family` is the one field that really holds it - "JPY", "MYR", and "USD1"/"USD5"/"USD50" for the dollar's
    denominations. `Description` is a country ("Japan (:100)", "Malaysia"), so reading the code from there produced
    JAP and MAL and the yen rate could not be found by asking for JPY. Description is kept only as a last resort, for
    a round where Family is missing.
    """
    explicit = _first(row, _CCY_KEYS)
    if explicit and _CCY_RE.match(str(explicit)):
        return str(explicit)[:3].upper()
    for key in ("Family", "Description", "FamilyLong"):
        m = _CCY_RE.match(str(row.get(key, "")))
        if m:
            return m.group(1).upper()
    return ""


_UNITS_RE = re.compile(r"\(\s*:\s*(\d+)\s*\)")


def _units_of(row: dict) -> int:
    """How many units of the currency the rate is quoted for.

    The bank quotes the small-denomination currencies per 100 or per 1000 and says so in the description: "Japan
    (:100)", "Vietnam (:1000)". Miss it and 30,000 yen costs 662,400 baht instead of 6,624.
    """
    m = _UNITS_RE.search(str(row.get("Description", "")))
    return int(m.group(1)) if m else 1


def _as_of(row: dict) -> str:
    day, hhmm, rnd = (str(row.get(k, "")).strip() for k in ("Ddate", "DTime", "Update"))
    if not day and not hhmm:
        return ""
    return f"{day} {hhmm}".strip() + (f" (round {rnd})" if rnd else "")


def _first(d: dict, keys: tuple) -> Any:
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return None


def _to_float(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(str(v).replace(",", "").strip())
    except (ValueError, TypeError):
        return None


def _rate_rows(data: Any) -> list[dict]:
    """The FX endpoints wrap the rate list differently across pages; dig out the first list of dicts."""
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        for v in data.values():
            if isinstance(v, list) and v and isinstance(v[0], dict):
                return v
    return []


def normalize_fx(data: Any) -> list[FxRate]:
    out: list[FxRate] = []
    for row in _rate_rows(data):
        code = _currency_of(row)
        if not code:
            continue
        out.append(FxRate(
            currency=code,
            units=_units_of(row),
            label=str(row.get("Description") or row.get("Family") or "").strip(),
            name=str(_first(row, _NAME_KEYS) or "").strip(),
            buying=_to_float(_first(row, _BUY_KEYS)),
            selling=_to_float(_first(row, _SELL_KEYS)),
            tt=_to_float(row.get("TT")),
            sight_bill=_to_float(row.get("SightBill")),
            as_of=_as_of(row),
        ))
    return out


def fx_latest_raw(settings: Settings) -> Any:
    return _get(settings, f"{FX_SERVICE}/GetLatestfxrates")


def fx_last_update(settings: Settings) -> str:
    """When the bank last published rates. The endpoint answers with a Python-repr string rather than JSON
    ("[{'Update': '2', 'Time': '13:10', 'Day': '09/09/2026'}]"), so parse that shape before falling back to raw."""
    data = _get(settings, f"{FX_SERVICE}/GetDateTimeLastUpdate")
    if isinstance(data, str):
        try:
            import ast

            data = ast.literal_eval(data)
        except (ValueError, SyntaxError):
            return data.strip()[:80]
    rows = data if isinstance(data, list) else [data]
    row = rows[0] if rows and isinstance(rows[0], dict) else {}
    day, hhmm, rnd = (str(row.get(k, "")).strip() for k in ("Day", "Time", "Update"))
    if day or hhmm:
        return f"{day} {hhmm}".strip() + (f" (round {rnd})" if rnd else "")
    return str(data)[:80]


def fx_rates_raw(settings: Settings, on: _date, round_no: int = 2, lang: str = "en") -> Any:
    """A specific date + intraday round. round_no follows the site: 1 = first of the day, and it rises through the day."""
    return _get(settings, f"{FX_SERVICE}/Getfxrates/{on.day:02d}/{on.month:02d}/{on.year}/{round_no}/{lang}")


# Customers ask for "เยน", not "JPY". The tool takes an ISO code, but a model handed a Thai or English currency name
# would otherwise get "not in today's list" for a currency the bank very much trades.
_CCY_ALIASES = {
    "usd": "USD", "dollar": "USD", "us dollar": "USD", "ดอลลาร์": "USD", "ดอลล่าร์": "USD", "เหรียญ": "USD",
    "jpy": "JPY", "yen": "JPY", "japanese yen": "JPY", "เยน": "JPY", "เงินเยน": "JPY", "ญี่ปุ่น": "JPY",
    "eur": "EUR", "euro": "EUR", "ยูโร": "EUR",
    "gbp": "GBP", "pound": "GBP", "sterling": "GBP", "ปอนด์": "GBP",
    "cny": "CNY", "yuan": "CNY", "rmb": "CNY", "หยวน": "CNY", "จีน": "CNY",
    "krw": "KRW", "won": "KRW", "วอน": "KRW", "เกาหลี": "KRW",
    "aud": "AUD", "ออสเตรเลีย": "AUD", "nzd": "NZD", "chf": "CHF", "ฟรังก์": "CHF",
    "sgd": "SGD", "สิงคโปร์": "SGD", "myr": "MYR", "ริงกิต": "MYR", "มาเลเซีย": "MYR",
    "hkd": "HKD", "ฮ่องกง": "HKD", "twd": "TWD", "ไต้หวัน": "TWD",
    "vnd": "VND", "ดong": "VND", "ดอง": "VND", "เวียดนาม": "VND",
    "idr": "IDR", "รูเปียห์": "IDR", "inr": "INR", "รูปี": "INR", "อินเดีย": "INR",
    "cad": "CAD", "แคนาดา": "CAD", "lak": "LAK", "กีบ": "LAK", "ลาว": "LAK",
    "mmk": "MMK", "จ๊าด": "MMK", "พม่า": "MMK", "php": "PHP", "เปโซ": "PHP", "ฟิลิปปินส์": "PHP",
    "dkk": "DKK", "nok": "NOK", "sek": "SEK", "zar": "ZAR", "aed": "AED", "sar": "SAR", "rub": "RUB",
}


def resolve_currency(text: str, rates: Optional[list] = None) -> str:
    """'เยน' / 'yen' / 'JPY' -> JPY. Falls back to matching the bank's own row names."""
    raw = str(text or "").strip()
    key = raw.lower()
    if key in _CCY_ALIASES:
        return _CCY_ALIASES[key]
    if len(raw) == 3 and raw.isalpha():
        return raw.upper()
    for alias, code in _CCY_ALIASES.items():
        if alias and alias in key:
            return code
    for r in rates or []:  # last resort: the bank's own wording for the row
        blob = f"{r.currency} {r.name} {r.label}".lower()
        if key and key in blob:
            return r.currency
    return raw.upper()


def _per_unit(rate: Optional[float], units: int) -> Optional[float]:
    """The rate for one unit, so an amount can be converted by a single multiplication."""
    return None if rate is None else round(rate / max(1, units), 6)


def fx_rate(settings: Settings, currency: str, *, lang: str = "en") -> dict:
    """The tool entry point: today's rate(s) for one currency, with the time the bank published them.

    A currency can have several bank-note denominations at different rates (USD 1-2 vs 50-100), so every matching row
    comes back and the agent quotes the one the customer means."""
    rates = normalize_fx(fx_latest_raw(settings))
    ccy = resolve_currency(currency, rates)
    hits = [r for r in rates if r.currency == ccy]
    log.info("fx_rate(%r) -> %s: %d row(s)", currency, ccy, len(hits))
    if not hits:
        return {"found": False, "currency": ccy,
                "available": sorted({r.currency for r in rates}),
                "asked_for": currency,
                "say": f"{ccy} is not in today's rate table; offer one of the currencies listed in `available`."}
    return {
        "found": True,
        "currency": ccy,
        "as_of": next((r.as_of for r in hits if r.as_of), ""),
        "rates": [{"denomination": r.label or r.name, "units": r.units, "buying": r.buying, "selling": r.selling,
                   "baht_per_1": {"buying": _per_unit(r.buying, r.units), "selling": _per_unit(r.selling, r.units)},
                   **({"telegraphic_transfer": r.tt} if r.tt is not None else {})} for r in hits],
        "meaning": "buying = what the bank pays the customer for the foreign notes; selling = what the customer pays "
                   "to buy them. Quote buying first when the customer is exchanging foreign currency into baht.",
        "converting": "buying and selling are baht per `units` of the currency - 100 for JPY, 1000 for VND. To convert "
                      "an amount, multiply it by `baht_per_1`, which is already divided down: 30,000 JPY to buy at "
                      "baht_per_1.selling 0.2208 = 6,624 baht. Never multiply the amount by `selling` directly.",
        "disclaimer": "Indicative rate for the round shown in as_of; it changes during the day and the branch rate at "
                      "the time applies to an actual transaction.",
    }


# ---------------- branch / ATM locator ----------------
def provinces(settings: Settings, lang: str = "en") -> Any:
    return _get(settings, f"{LOC_SERVICE}/GetProvince{'Th' if lang == 'th' else 'En'}")


def countries(settings: Settings, lang: str = "en") -> Any:
    return _get(settings, f"{LOC_SERVICE}/GetCountry{'Th' if lang == 'th' else 'En'}")


# The Locate Us page's own type codes, confirmed from its network calls and its own filter labels: BRC, ATM, ATMPLUS,
# FXB, FCD, BEV and BUC. An unrecognised code is passed through uppercased rather than rejected, so a new one works the
# day it is found without a code change.
KIND_BRANCH = "BRC"
KIND_ATM = "ATM"
KIND_ATM_PLUS = "ATMPLUS"
KIND_FX_BOOTH = "FXB"  # a currency-exchange booth, which is not the same thing as a branch that happens to do FX
KIND_FCD = "FCD"  # a branch that handles foreign-currency deposit accounts: opening one, not exchanging cash
KIND_WEALTH_LOUNGE = "BEV"  # Wealth Lounge
KIND_BUSINESS_CENTER = "BUC"  # สำนักธุรกิจ / Business Center, for business banking rather than a retail counter
_KINDS = {"branch": KIND_BRANCH, "brc": KIND_BRANCH, "สาขา": KIND_BRANCH,
          # the Locate Us "Select Service" labels, verbatim in both languages
          "bangkok bank branches": KIND_BRANCH, "สาขาธนาคารกรุงเทพ": KIND_BRANCH,
          "บริการเงินฝากเงินตราต่างประเทศ": KIND_FCD,
          "atm": KIND_ATM, "ตู้เอทีเอ็ม": KIND_ATM, "เอทีเอ็ม": KIND_ATM,
          "atmplus": KIND_ATM_PLUS, "atm plus": KIND_ATM_PLUS, "atm+": KIND_ATM_PLUS,
          "fxb": KIND_FX_BOOTH, "fx": KIND_FX_BOOTH, "exchange": KIND_FX_BOOTH, "fx booth": KIND_FX_BOOTH,
          "บูธแลกเปลี่ยนเงินตราต่างประเทศ": KIND_FX_BOOTH,
          "currency exchange": KIND_FX_BOOTH, "money exchange": KIND_FX_BOOTH,
          "แลกเงิน": KIND_FX_BOOTH, "บูธแลกเงิน": KIND_FX_BOOTH, "ที่แลกเงิน": KIND_FX_BOOTH,
          "fcd": KIND_FCD, "foreign currency deposit": KIND_FCD, "fcd account": KIND_FCD,
          "บัญชีเงินตราต่างประเทศ": KIND_FCD, "เงินฝากสกุลต่างประเทศ": KIND_FCD,
          # the lounge is what the locator calls it; customers and our own knowledge say Wealth Center / เวลท์ / สินธร
          "bev": KIND_WEALTH_LOUNGE, "wealth lounge": KIND_WEALTH_LOUNGE, "wealth": KIND_WEALTH_LOUNGE,
          "wealth center": KIND_WEALTH_LOUNGE, "wealth centre": KIND_WEALTH_LOUNGE,
          "wealth management": KIND_WEALTH_LOUNGE, "bualuang exclusive": KIND_WEALTH_LOUNGE,
          "เวลท์เลานจ์": KIND_WEALTH_LOUNGE, "เลานจ์": KIND_WEALTH_LOUNGE, "เวลท์": KIND_WEALTH_LOUNGE,
          "เวลท์เซ็นเตอร์": KIND_WEALTH_LOUNGE, "ศูนย์เวลท์": KIND_WEALTH_LOUNGE,
          "buc": KIND_BUSINESS_CENTER, "business center": KIND_BUSINESS_CENTER,
          "business centre": KIND_BUSINESS_CENTER, "สำนักธุรกิจ": KIND_BUSINESS_CENTER}
_CODE = re.compile(r"^[A-Za-z0-9+]{2,10}$")  # what a locator type code looks like, so a new one works without a change


def resolve_kind(kind: str) -> str:
    """'branch' / 'atm' / 'atm plus' (or a raw code) -> the code the locator expects.

    A phrase we do not know is searched as a branch rather than sent on: the locator has no such type, so passing it
    through would only produce an error, while the nearest branches are at least an answer the customer can use."""
    k = str(kind or "").strip().lower()
    if k in _KINDS:
        return _KINDS[k]
    if not k:
        return KIND_BRANCH
    if _CODE.match(k):
        return k.upper()
    log.info("resolve_kind(%r): not a known service, searching branches instead", kind)
    return KIND_BRANCH


@dataclass
class Place:
    name: str = ""
    branch_no: str = ""
    address: str = ""  # the locator splits this across Address1..3: street, then sub-district and district
    province: str = ""
    postcode: str = ""
    phone: str = ""
    hours: str = ""
    services: Optional[list[str]] = None  # the "x" columns on the row: Branch, ATM, ATMPlus, BusinessCenter, FX...
    lat: Optional[float] = None
    lon: Optional[float] = None
    distance_km: Optional[float] = None


_PLACE_KEYS = {
    "name": ("BranchName", "Name", "name", "branchName", "Title", "LocationName"),
    "branch_no": ("BranchNo", "BranchCode", "Code"),
    "province": ("Province", "province", "City"),
    "postcode": ("Postcode", "PostCode", "Zipcode"),
    "phone": ("Tel", "Telephone", "Phone", "phone", "TelNo", "Telno"),
    "hours": ("MicroBranchHours", "BranchHours", "OpenTime", "OpeningHours", "Hours", "ServiceTime", "Time"),
    "lat": ("Latitude", "latitude", "Lat", "lat"),
    "lon": ("Longitude", "longitude", "Lng", "lng", "Lon", "lon"),
    "distance_km": ("Distance", "distance", "DistanceKm"),
}
_ADDRESS_KEYS = ("Address1", "Address2", "Address3", "Address4", "Address", "FullAddress", "AddressTh", "AddressEn")
# a row marks each service it offers with "x" in a column named after that service, which is how we know whether a
# place actually exchanges currency rather than just being the nearest branch
_NOT_A_SERVICE = {"branchname", "branchno", "province", "postcode", "tel", "telephone", "phone",
                  "latitude", "longitude", "distance", "microbranchhours", "branchhours"}


def _services_of(row: dict) -> list[str]:
    return [k for k, v in row.items()
            if str(v).strip().lower() == "x" and k.lower() not in _NOT_A_SERVICE and not k.lower().startswith("address")]


# What the locator writes in a field it has nothing for. An ATM row carries Tel "BeID" (an internal device marker) or
# "-", and MicroBranchHours "-", because an ATM has no counter and no phone. Passed on, they become an assistant
# telling a customer to call "BeID", or filling the gap where the hours should be with a plausible invention.
_PLACEHOLDERS = {"-", "--", "---", "n/a", "na", "none", "null", "beid", "no data", "ไม่มี", "ไม่มีข้อมูล"}


def _clean(value: Any) -> str:
    text = str(value or "").strip()
    return "" if text.lower() in _PLACEHOLDERS else text


def _clean_phone(value: Any) -> str:
    """A phone number the customer can dial, or nothing. "BeID" is not a number, and neither is any other word."""
    text = _clean(value)
    return text if any(ch.isdigit() for ch in text) else ""


def normalize_places(data: Any) -> list[Place]:
    """Branch rows, normalised the same defensive way as the rates: unknown spellings degrade to blank, not a crash."""
    out: list[Place] = []
    for row in _rate_rows(data):
        p = Place(services=[])
        for field, keys in _PLACE_KEYS.items():
            raw = _first(row, keys)
            if raw in (None, ""):
                continue
            if field in ("lat", "lon", "distance_km"):
                setattr(p, field, _to_float(raw))
            elif field == "phone":
                setattr(p, field, _clean_phone(raw))
            else:
                setattr(p, field, _clean(raw))
        parts = [str(row[k]).strip() for k in _ADDRESS_KEYS if str(row.get(k, "")).strip()]
        p.address = " ".join(dict.fromkeys(parts))
        p.services = _services_of(row)
        if p.name or p.address:
            out.append(p)
    return out


def search_places_raw(settings: Settings, province: str, lat: float, lon: float, *,
                      district: str = "0", kind: str = KIND_BRANCH, lang: str = "th") -> Any:
    """The Locate Us search: /Search{Thailand}{Lang}WithLocation/{province}/{district}/{lat}/{lon}/{kind}.

    The coordinates are what make it a 'near me' search; the service orders the results by distance from them.

    Every segment is escaped with safe="": these values are chosen by a model from what a customer typed, and quote()
    leaves "/" alone by default, so a slash in one of them would silently become a different endpoint."""
    from urllib.parse import quote

    seg = {"province": quote(province, safe=""), "district": quote(str(district), safe=""),
           "kind": quote(kind, safe="")}
    where = "SearchThaiLandTh" if lang == "th" else "SearchThaiLandEn"
    path = f"{LOC_SERVICE}/{where}WithLocation/{seg['province']}/{seg['district']}/{lat}/{lon}/{seg['kind']}"
    return _get(settings, path)


# The locator names an ATM after the branch it stands at - "สาขาประตูเชียงใหม่ #1" is a machine, not a counter - and a
# model reading that name will reach for branch opening hours and quote them. Saying so in the result works where a
# rule in the prompt does not: it is in front of the model at the moment it writes the sentence.
_KIND_NOTES = {
    KIND_BRANCH: "Opening hours and services can change; suggest calling the branch before travelling.",
    KIND_ATM: "These are cash machines, not branch counters. The name is the branch each one stands at, not a place "
              "open at branch hours. An ATM has no opening hours and no phone number: give the location only, never "
              "state hours, never say 24 hours, and never tell the customer to call it.",
    KIND_FX_BOOTH: "These are currency-exchange booths. Hours and the currencies stocked can change; suggest calling "
                   "ahead for a large amount.",
    KIND_FCD: "These branches open and service foreign-currency deposit accounts; that is not the same as exchanging "
              "cash over the counter.",
    KIND_WEALTH_LOUNGE: "These are Wealth Lounges (Bualuang Exclusive), for customers who qualify for that service.",
    KIND_BUSINESS_CENTER: "These are business centres (สำนักธุรกิจ), for business banking rather than a retail counter.",
}
_KIND_NOTES[KIND_ATM_PLUS] = _KIND_NOTES[KIND_ATM]


def _name_key(text: str) -> str:
    """A branch name reduced to what a customer would actually type: no "สาขา", no spaces, no case."""
    t = str(text or "").lower()
    for word in ("สาขา", "branch", "ธนาคารกรุงเทพ", "bangkok bank"):
        t = t.replace(word, " ")
    return "".join(t.split())


def find_branch(settings: Settings, lat: Optional[float] = None, lon: Optional[float] = None, *, province: str = "",
                district: str = "0", kind: str = KIND_BRANCH, lang: str = "th", limit: int = 5,
                name: str = "") -> dict:
    """The tool entry point: the places nearest a pair of coordinates, or nearest the middle of a named province.

    A customer who has not shared their location can still be helped if they say where they are, so a province on its
    own is enough: we search from its centre and the distances are measured from there.

    `name` answers the other half of the question - "does สาขาซีคอนสแควร์ open on Saturday". The locator returns every
    place in the province (196 of them in Bangkok), so a named branch is found by filtering that list, not by hoping it
    lands in the nearest few: ranking by distance and cutting to `limit` is exactly what used to lose it.
    """
    kind = resolve_kind(kind)  # accept a label as well as a code, and never build a URL from a phrase
    named = PROV.resolve_province(province) if province else ""
    if province and not named:
        # "แถวสีลม" is a district, a landmark or a typo, not a province. The locator only understands the 77 names, so
        # sending it on would 404; the coordinates, if we have them, are the better answer than a question.
        log.info("find_branch: %r is not a province I know; %s", province,
                 "searching from the coordinates instead" if lat is not None and lon is not None else "asking")
    if lat is None or lon is None:
        here = PROV.CENTROIDS.get(named) if named else None
        if here is None:
            return {"found": False, "kind": kind, "looked_for": province,
                    "say": "No location, and no province I recognise. Ask the customer which province they are in "
                           "(the 77 Thai provinces - a district or a landmark is not enough), or to share their "
                           "location, then look again. Do not invent a branch."}
        lat, lon = here
    # The locator needs a province in the path (an empty one 404s), but a phone only gives coordinates - so derive it.
    # Two provinces, because a customer near a boundary should still be offered the branch across the line.
    searched = [named] if named else PROV.nearest(lat, lon, 2)
    rows: list[dict] = []
    errors: list[str] = []
    for prov in searched:
        try:
            got = search_places_raw(settings, prov, lat, lon, district=district, kind=kind, lang=lang)
        except ServiceError as e:
            errors.append(str(e))
            continue
        rows += [r for r in _rate_rows(got)]
    if not rows and errors:
        return {"found": False, "error": errors[0],
                "say": "The branch lookup is not available right now; point the customer at the bank's Locate Us page."}
    places = normalize_places(rows)
    seen_ids: set[str] = set()  # two neighbouring provinces can return the same branch
    unique: list[Place] = []
    for p in places:
        key = p.branch_no or f"{p.name}|{p.address}"
        if key in seen_ids:
            continue
        seen_ids.add(key)
        unique.append(p)
    places = unique
    if name:
        wanted = _name_key(name)
        matched = [p for p in places if wanted and wanted in _name_key(p.name)]
        log.info("find_branch(name=%r) -> %d of %d place(s) in %s match", name, len(matched), len(places), searched)
        if not matched:
            return {"found": False, "province": ", ".join(searched), "kind": kind, "looked_for": name,
                    "say": f"No place called {name!r} in {', '.join(searched)}. This search covers those provinces "
                           "only, so say you could not find that one there and ask which province or area it is in. "
                           "Do NOT tell the customer the bank has no such branch - you cannot know that."}
        places = matched
    # the rows carry their own coordinates, so order by real distance rather than by the order each province came back
    for p in places:
        if p.distance_km is None and p.lat is not None and p.lon is not None:
            p.distance_km = round(PROV.haversine_km(lat, lon, p.lat, p.lon), 1)
    places.sort(key=lambda p: p.distance_km if p.distance_km is not None else 1e9)
    places = places[:limit]
    log.info("find_branch(lat=%.4f, lon=%.4f, provinces=%s, kind=%s) -> %d place(s), nearest %s km",
             lat, lon, searched, kind, len(places), places[0].distance_km if places else "-")
    if not places:
        what = "branch" if kind == KIND_BRANCH else f"place of type {kind}"
        return {"found": False, "province": ", ".join(searched), "kind": kind,
                "say": f"No {what} came back for that spot. Say plainly that none was found nearby, then ask the "
                       "customer which province or district they mean, or suggest the bank's Locate Us page."}
    return {
        "found": True,
        "near": {"lat": lat, "lon": lon},
        "province": ", ".join(searched),
        "kind": kind,
        "branches": [{k: v for k, v in vars(p).items() if v not in (None, "")} for p in places],
        "note": _KIND_NOTES.get(kind, _KIND_NOTES[KIND_BRANCH]),
    }


def locator_endpoint(settings: Settings, path: str) -> Any:
    """Escape hatch for a locator path whose exact shape we confirm from the live Locate-Us page (branch search by
    province / district / geo). `path` is appended to the location service, e.g. 'GetBranchByProvince/10'."""
    return _get(settings, f"{LOC_SERVICE}/{path.lstrip('/')}")
