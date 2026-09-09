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
    currency: str  # ISO code, e.g. USD
    name: str = ""  # human name if the API gives one
    buying: Optional[float] = None  # bank buys the foreign currency (sight / TT vary; see `note`)
    selling: Optional[float] = None  # bank sells the foreign currency
    unit: str = ""
    note: str = ""


_CCY_KEYS = ("CurrencyCode", "currencyCode", "Currency", "currency", "Code", "code", "Abbreviation", "curr")
_NAME_KEYS = ("CurrencyName", "currencyName", "Name", "name", "Description", "description")
_BUY_KEYS = ("Buying", "buying", "BuyingRate", "buyingRate", "Buy", "buy", "BuyingSight", "buyingSight", "BuyingTransfer")
_SELL_KEYS = ("Selling", "selling", "SellingRate", "sellingRate", "Sell", "sell")


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
        code = _first(row, _CCY_KEYS)
        if not code:
            continue
        out.append(FxRate(
            currency=str(code).upper().strip(),
            name=str(_first(row, _NAME_KEYS) or "").strip(),
            buying=_to_float(_first(row, _BUY_KEYS)),
            selling=_to_float(_first(row, _SELL_KEYS)),
            unit=str(row.get("Unit") or row.get("unit") or "").strip(),
        ))
    return out


def fx_latest_raw(settings: Settings) -> Any:
    return _get(settings, f"{FX_SERVICE}/GetLatestfxrates")


def fx_last_update(settings: Settings) -> str:
    data = _get(settings, f"{FX_SERVICE}/GetDateTimeLastUpdate")
    if isinstance(data, str):
        return data
    if isinstance(data, dict):
        return str(_first(data, ("DateTime", "dateTime", "LastUpdate", "lastUpdate", "value")) or json.dumps(data)[:80])
    return str(data)[:80]


def fx_rates_raw(settings: Settings, on: _date, round_no: int = 2, lang: str = "en") -> Any:
    """A specific date + intraday round. round_no follows the site: 1 = first of the day, and it rises through the day."""
    return _get(settings, f"{FX_SERVICE}/Getfxrates/{on.day:02d}/{on.month:02d}/{on.year}/{round_no}/{lang}")


def fx_rate(settings: Settings, currency: str, *, lang: str = "en") -> dict:
    """The tool entry point: the latest rate for one currency, plus when it was last updated."""
    ccy = currency.upper().strip()
    rates = normalize_fx(fx_latest_raw(settings))
    hit = next((r for r in rates if r.currency == ccy), None)
    updated = ""
    try:
        updated = fx_last_update(settings)
    except ServiceError:
        pass
    if hit is None:
        return {"found": False, "currency": ccy, "available": [r.currency for r in rates], "as_of": updated}
    return {"found": True, "currency": hit.currency, "name": hit.name, "buying": hit.buying,
            "selling": hit.selling, "unit": hit.unit, "as_of": updated,
            "disclaimer": "Indicative rate, changes through the day; confirm with Bangkok Bank before transacting."}


# ---------------- branch / ATM locator ----------------
def provinces(settings: Settings, lang: str = "en") -> Any:
    return _get(settings, f"{LOC_SERVICE}/GetProvince{'Th' if lang == 'th' else 'En'}")


def countries(settings: Settings, lang: str = "en") -> Any:
    return _get(settings, f"{LOC_SERVICE}/GetCountry{'Th' if lang == 'th' else 'En'}")


def locator_endpoint(settings: Settings, path: str) -> Any:
    """Escape hatch for a locator path whose exact shape we confirm from the live Locate-Us page (branch search by
    province / district / geo). `path` is appended to the location service, e.g. 'GetBranchByProvince/10'."""
    return _get(settings, f"{LOC_SERVICE}/{path.lstrip('/')}")
