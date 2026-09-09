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
from dataclasses import dataclass
from datetime import date as _date
from typing import Any, Optional

from .config import Settings

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
    try:
        from curl_cffi import requests as creq  # type: ignore

        r = creq.get(url, impersonate="chrome", timeout=_TIMEOUT, headers=headers)
        status, text = r.status_code, r.text
    except ImportError:
        import requests

        r = requests.get(url, timeout=_TIMEOUT, headers=headers)
        status, text = r.status_code, r.text
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

    currency: str  # ISO code parsed out of Description / Family
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
    """The ISO code: an explicit field if one ever appears, else the leading letters of Description / Family."""
    explicit = _first(row, _CCY_KEYS)
    if explicit and _CCY_RE.match(str(explicit)):
        return str(explicit)[:3].upper()
    for key in ("Description", "Family", "FamilyLong"):
        m = _CCY_RE.match(str(row.get(key, "")))
        if m:
            return m.group(1).upper()
    return ""


def _as_of(row: dict) -> str:
    day, time, rnd = (str(row.get(k, "")).strip() for k in ("Ddate", "DTime", "Update"))
    if not day and not time:
        return ""
    return f"{day} {time}".strip() + (f" (round {rnd})" if rnd else "")


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
    day, time, rnd = (str(row.get(k, "")).strip() for k in ("Day", "Time", "Update"))
    if day or time:
        return f"{day} {time}".strip() + (f" (round {rnd})" if rnd else "")
    return str(data)[:80]


def fx_rates_raw(settings: Settings, on: _date, round_no: int = 2, lang: str = "en") -> Any:
    """A specific date + intraday round. round_no follows the site: 1 = first of the day, and it rises through the day."""
    return _get(settings, f"{FX_SERVICE}/Getfxrates/{on.day:02d}/{on.month:02d}/{on.year}/{round_no}/{lang}")


def fx_rate(settings: Settings, currency: str, *, lang: str = "en") -> dict:
    """The tool entry point: today's rate(s) for one currency, with the time the bank published them.

    A currency can have several bank-note denominations at different rates (USD 1-2 vs 50-100), so every matching row
    comes back and the agent quotes the one the customer means."""
    ccy = currency.upper().strip()
    rates = normalize_fx(fx_latest_raw(settings))
    hits = [r for r in rates if r.currency == ccy]
    if not hits:
        return {"found": False, "currency": ccy,
                "available": sorted({r.currency for r in rates}),
                "say": f"{ccy} is not in today's rate table; offer one of the currencies listed in `available`."}
    return {
        "found": True,
        "currency": ccy,
        "as_of": next((r.as_of for r in hits if r.as_of), ""),
        "rates": [{"denomination": r.label or r.name, "buying": r.buying, "selling": r.selling,
                   **({"telegraphic_transfer": r.tt} if r.tt is not None else {})} for r in hits],
        "meaning": "buying = what the bank pays the customer for the foreign notes; selling = what the customer pays "
                   "to buy them. Quote buying first when the customer is exchanging foreign currency into baht.",
        "disclaimer": "Indicative rate for the round shown in as_of; it changes during the day and the branch rate at "
                      "the time applies to an actual transaction.",
    }


# ---------------- branch / ATM locator ----------------
def provinces(settings: Settings, lang: str = "en") -> Any:
    return _get(settings, f"{LOC_SERVICE}/GetProvince{'Th' if lang == 'th' else 'En'}")


def countries(settings: Settings, lang: str = "en") -> Any:
    return _get(settings, f"{LOC_SERVICE}/GetCountry{'Th' if lang == 'th' else 'En'}")


def locator_endpoint(settings: Settings, path: str) -> Any:
    """Escape hatch for a locator path whose exact shape we confirm from the live Locate-Us page (branch search by
    province / district / geo). `path` is appended to the location service, e.g. 'GetBranchByProvince/10'."""
    return _get(settings, f"{LOC_SERVICE}/{path.lstrip('/')}")
