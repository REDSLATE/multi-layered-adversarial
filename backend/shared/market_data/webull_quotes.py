"""Webull REST market-data client used by the doctrine enricher.

Operator doctrine (2026-06-11):
    Webull equity quote data was unlocked via Open API Advanced Quotes
    ("Nasdaq Basic - Non Display", Free Authorized through 2027-06-10).
    Crypto data is bundled with the base entitlement. Options (OPRA)
    are NOT subscribed — anything that would need that should fail
    closed.

    This module is the ONE place the brain runner pulls Webull
    market data from. It is:
      * Sync — wraps the SDK's blocking REST calls. Callers in async
        contexts use `loop.run_in_executor` to keep the event loop
        clear.
      * Cached — small in-process TTL cache per (operation, key) so a
        45-second brain tick across the 48-ticker equity universe
        doesn't burn the 60/min snapshot budget. TTL is conservative
        (snapshot 5s, screener 60s, instrument-meta 1h).
      * Read-only — never places orders. The broker adapter
        (`shared/broker/webull.py`) owns trading.
      * Fail-soft — every public method returns `None` (or empty
        list/dict) on SDK error rather than raising. Callers treat
        absence as "no enrichment available this tick" and proceed
        with the base snapshot.

Entitlement awareness:
    `get_app_subscriptions()` is the source of truth for whether the
    app key has US equity / OPRA / crypto. The `/api/admin/webull/
    entitlements` endpoint reads this same client.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger("risedual.market_data.webull")

# ── Cache TTLs (seconds). Tuned for the 60/min snapshot rate ceiling
#    with the 45s tick interval — at 1 call per 12 tickers the bound
#    is ~4 calls/min if we batch 12-wide.
SNAPSHOT_TTL_SEC = 5.0
BARS_TTL_SEC = 30.0
SCREENER_TTL_SEC = 60.0
INSTRUMENT_TTL_SEC = 3600.0
ENTITLEMENTS_TTL_SEC = 60.0


class _TTLCache:
    __slots__ = ("_data", "_lock")

    def __init__(self) -> None:
        self._data: Dict[Any, tuple[float, Any]] = {}
        self._lock = threading.Lock()

    def get(self, key: Any, ttl: float) -> Any:
        with self._lock:
            row = self._data.get(key)
            if not row:
                return None
            ts, val = row
            if time.time() - ts > ttl:
                self._data.pop(key, None)
                return None
            return val

    def set(self, key: Any, val: Any) -> None:
        with self._lock:
            self._data[key] = (time.time(), val)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()


_CACHE = _TTLCache()
_CLIENT_LOCK = threading.Lock()
_CLIENT: Optional["WebullQuotesClient"] = None


def get_quotes_client() -> Optional["WebullQuotesClient"]:
    """Process-wide singleton. Returns `None` if creds are missing or
    the SDK isn't installed — callers must handle absence."""
    global _CLIENT
    if _CLIENT is not None:
        return _CLIENT
    with _CLIENT_LOCK:
        if _CLIENT is not None:
            return _CLIENT
        app_key = (os.environ.get("WEBULL_APP_KEY") or "").strip()
        app_secret = (os.environ.get("WEBULL_APP_SECRET") or "").strip()
        if not app_key or not app_secret:
            return None
        try:
            from webull.core.client import ApiClient  # type: ignore  # noqa: WPS433
            from webull.data.data_client import DataClient  # type: ignore  # noqa: WPS433
            from webull.trade.trade_client import TradeClient  # type: ignore  # noqa: WPS433
        except Exception as e:  # noqa: BLE001
            logger.warning("Webull SDK not importable: %s", e)
            return None
        region_id = (os.environ.get("WEBULL_REGION_ID") or "us").strip()
        environment = (os.environ.get("WEBULL_ENVIRONMENT") or "prod").strip().lower()
        try:
            api = ApiClient(app_key, app_secret, region_id)
            if environment == "uat":
                api.add_endpoint(region_id, "us-openapi-alb.uat.webullbroker.com")
            _CLIENT = WebullQuotesClient(
                data=DataClient(api), trade=TradeClient(api),
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("Webull quotes client init failed: %s", e)
            return None
        return _CLIENT


def reset_quotes_client_for_tests() -> None:
    """Tests rebind the singleton + clear cache. Production never calls this."""
    global _CLIENT
    with _CLIENT_LOCK:
        _CLIENT = None
    _CACHE.clear()


def _coerce_body(resp: Any) -> Any:
    """SDK responses are `requests.Response`-like. Pull JSON defensively."""
    if resp is None:
        return None
    if hasattr(resp, "json"):
        try:
            return resp.json()
        except Exception:  # noqa: BLE001
            return None
    return resp


class WebullQuotesClient:
    """Thin, cached, fail-soft wrapper over `DataClient` + `TradeClient`."""

    def __init__(self, data: Any, trade: Any) -> None:
        self._data = data
        self._trade = trade

    # ── entitlements ────────────────────────────────────────────────
    def get_entitlements(self) -> Dict[str, Any]:
        """Returns `{base_subscription, data_classes: {...}, raw}`.

        `data_classes` is what the operator UI cares about — we probe
        the gated endpoints with a tiny payload and infer entitlement
        from whether they 401 or return data.
        """
        cached = _CACHE.get(("entitlements",), ENTITLEMENTS_TTL_SEC)
        if cached is not None:
            return cached
        out: Dict[str, Any] = {
            "base_subscription": False,
            "data_classes": {
                "us_stock_quotes": False,
                "us_option_quotes": False,
                "us_crypto": False,
            },
            "subscriptions": [],
            "checked_at": time.time(),
        }
        # Base entitlement: any row returned from get_app_subscriptions
        try:
            r = self._trade.account.get_app_subscriptions()
            body = _coerce_body(r) or []
            if isinstance(body, list):
                out["subscriptions"] = body
                out["base_subscription"] = bool(body)
        except Exception as e:  # noqa: BLE001
            logger.debug("get_app_subscriptions failed: %s", e)

        # Equity probe — get_snapshot returns 401 when not subscribed
        out["data_classes"]["us_stock_quotes"] = self._probe_equity()
        out["data_classes"]["us_option_quotes"] = self._probe_options()
        out["data_classes"]["us_crypto"] = self._probe_crypto()

        _CACHE.set(("entitlements",), out)
        return out

    def _probe_equity(self) -> bool:
        try:
            r = self._data.market_data.get_snapshot(["AAPL"], "US_STOCK")
            body = _coerce_body(r)
            return isinstance(body, list) and bool(body)
        except Exception:  # noqa: BLE001
            return False

    def _probe_options(self) -> bool:
        try:
            r = self._data.option_market_data.get_option_snapshot(["AAPL"], "US_OPTION")
            body = _coerce_body(r)
            return isinstance(body, list) and bool(body)
        except Exception:  # noqa: BLE001
            return False

    def _probe_crypto(self) -> bool:
        try:
            r = self._data.crypto_market_data.get_crypto_snapshot(["BTCUSD"])
            body = _coerce_body(r)
            return isinstance(body, list) and bool(body)
        except Exception:  # noqa: BLE001
            return False

    # ── snapshots ──────────────────────────────────────────────────
    def equity_snapshot(self, symbol: str) -> Optional[Dict[str, Any]]:
        sym = (symbol or "").upper()
        if not sym:
            return None
        cached = _CACHE.get(("eq_snap", sym), SNAPSHOT_TTL_SEC)
        if cached is not None:
            return cached
        try:
            r = self._data.market_data.get_snapshot([sym], "US_STOCK")
            body = _coerce_body(r)
        except Exception as e:  # noqa: BLE001
            logger.debug("equity_snapshot %s failed: %s", sym, e)
            return None
        if not isinstance(body, list) or not body:
            return None
        row = body[0]
        _CACHE.set(("eq_snap", sym), row)
        return row

    def crypto_snapshot(self, symbol: str) -> Optional[Dict[str, Any]]:
        """`symbol` here is the Webull concat form (e.g. "BTCUSD")."""
        sym = (symbol or "").upper().replace("-", "").replace("/", "")
        if not sym:
            return None
        cached = _CACHE.get(("cr_snap", sym), SNAPSHOT_TTL_SEC)
        if cached is not None:
            return cached
        try:
            r = self._data.crypto_market_data.get_crypto_snapshot([sym])
            body = _coerce_body(r)
        except Exception as e:  # noqa: BLE001
            logger.debug("crypto_snapshot %s failed: %s", sym, e)
            return None
        if not isinstance(body, list) or not body:
            return None
        row = body[0]
        _CACHE.set(("cr_snap", sym), row)
        return row

    # ── bars (for momentum / pullback detection) ───────────────────
    def equity_bars(self, symbol: str, timespan: str = "M1", count: int = 30) -> List[Dict[str, Any]]:
        sym = (symbol or "").upper()
        if not sym:
            return []
        key = ("eq_bars", sym, timespan, count)
        cached = _CACHE.get(key, BARS_TTL_SEC)
        if cached is not None:
            return cached
        try:
            r = self._data.market_data.get_history_bar(
                sym, "US_STOCK", timespan, count=str(count),
            )
            body = _coerce_body(r)
        except Exception as e:  # noqa: BLE001
            logger.debug("equity_bars %s failed: %s", sym, e)
            return []
        bars = body if isinstance(body, list) else []
        _CACHE.set(key, bars)
        return bars

    # ── instrument metadata (mostly static; long TTL) ──────────────
    def instrument(self, symbol: str) -> Optional[Dict[str, Any]]:
        sym = (symbol or "").upper()
        if not sym:
            return None
        cached = _CACHE.get(("instr", sym), INSTRUMENT_TTL_SEC)
        if cached is not None:
            return cached
        try:
            r = self._data.instrument.get_instrument([sym])
            body = _coerce_body(r)
        except Exception as e:  # noqa: BLE001
            logger.debug("instrument %s failed: %s", sym, e)
            return None
        if not isinstance(body, list) or not body:
            return None
        row = body[0]
        _CACHE.set(("instr", sym), row)
        return row

    # ── screener (relative volume signal for the brain universe) ───
    def most_active_map(self) -> Dict[str, Dict[str, Any]]:
        """Return `{symbol: row}` of the current most-active list.

        Used by the enricher to pull `relative_volume_10d` and
        `turnover_rate` per ticker without making per-symbol screener
        calls. One call covers all hot names.
        """
        cached = _CACHE.get(("screener", "most_active"), SCREENER_TTL_SEC)
        if cached is not None:
            return cached
        try:
            r = self._data.screener.get_most_active(
                "US_STOCK", rank_type="VOLUME", sort_by="VOLUME", direction="DESC",
            )
            body = _coerce_body(r) or {}
        except Exception as e:  # noqa: BLE001
            logger.debug("most_active failed: %s", e)
            return {}
        rows = body.get("data") if isinstance(body, dict) else []
        out: Dict[str, Dict[str, Any]] = {}
        if isinstance(rows, list):
            for row in rows:
                if not isinstance(row, dict):
                    continue
                sym = (row.get("symbol") or "").upper()
                if sym:
                    out[sym] = row
        _CACHE.set(("screener", "most_active"), out)
        return out
