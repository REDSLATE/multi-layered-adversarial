"""Moomoo (moomoo US) adapters — 2026-06 operator directive.

TWO separate adapters, mirroring the directive:
  MoomooMarketDataAdapter  quote / bid-ask / order book / candles /
                           market status / entitlements
  MoomooBrokerAdapter      account / buying power / positions /
                           submit / cancel / order status / fills

OpenD is a USER-HOSTED gateway (VPS/home machine) — never a subprocess
in this pod. The backend connects to OPEND_HOST:OPEND_PORT over a
private network, RSA-encrypted when MOOMOO_RSA_PRIVATE_KEY_PEM is set.

CREDENTIALS: environment variables ONLY (production: Deploy → Env
Variables). Nothing broker-secret touches MongoDB, logs or the UI:
  OPEND_HOST / OPEND_PORT
  MOOMOO_RSA_PRIVATE_KEY_PEM   (PKCS#1 PEM, required for remote OpenD)
  MOOMOO_TRADE_PASSWORD_MD5    (32-hex md5 — NEVER the plaintext)
  MOOMOO_TRADING_ENV           SIMULATE | REAL
  MOOMOO_ACC_ID                stable account id (0 = first)

V1 live limits (runtime_flags _id=moomoo_limits): RTH only, one
position at a time, tiny notional cap, NO broker fallback — a Moomoo
rejection surfaces as BrokerRouteBlocked, never silently re-venued.
Options are implemented in the schema but autonomous option orders
stay disabled until the options execution policy is ready.
"""
from __future__ import annotations

import asyncio
import logging
import os
import tempfile
import time
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger("risedual.moomoo")

_SDK_LOCK = asyncio.Semaphore(2)  # cap concurrent sync SDK calls
LIMITS_FLAG = "moomoo_limits"
LIMITS_DEFAULTS = {"enabled": True, "max_notional_usd": 25.0,
                   "one_position_at_a_time": True, "rth_only": True,
                   "allow_autonomous_options": False}


def moomoo_config() -> Optional[dict]:
    """None when unconfigured. Never returns secret VALUES to callers
    that serialize — use configured_summary() for the UI."""
    host = os.environ.get("OPEND_HOST")
    if not host:
        return None
    return {
        "host": host,
        "port": int(os.environ.get("OPEND_PORT") or 11111),
        "rsa_pem": os.environ.get("MOOMOO_RSA_PRIVATE_KEY_PEM"),
        "pwd_md5": os.environ.get("MOOMOO_TRADE_PASSWORD_MD5"),
        "env": (os.environ.get("MOOMOO_TRADING_ENV") or "SIMULATE").upper(),
        "acc_id": int(os.environ.get("MOOMOO_ACC_ID") or 0),
    }


def configured_summary() -> dict:
    cfg = moomoo_config()
    if not cfg:
        return {"configured": False,
                "missing": ["OPEND_HOST", "OPEND_PORT",
                            "MOOMOO_RSA_PRIVATE_KEY_PEM",
                            "MOOMOO_TRADE_PASSWORD_MD5"],
                "how": ("host OpenD on a machine you control, then set the "
                        "env vars in production via Deploy → Environment "
                        "Variables — secrets never enter MongoDB or the UI")}
    return {"configured": True, "host": cfg["host"], "port": cfg["port"],
            "encrypted": bool(cfg["rsa_pem"]),
            "trading_env": cfg["env"],
            "unlock_ready": bool(cfg["pwd_md5"])
            or cfg["env"] == "SIMULATE"}


def _us(symbol: str) -> str:
    s = (symbol or "").upper().strip()
    return s if s.startswith("US.") else f"US.{s}"


def _plain(code: str) -> str:
    return (code or "").replace("US.", "")


class _Ctx:
    """One short-lived context pair per call — V1 correctness over
    throughput (tiny-notional validation phase)."""

    def __init__(self, cfg: dict, need_trade: bool):
        self.cfg, self.need_trade = cfg, need_trade
        self.q = self.t = None
        self._key_path = None

    def __enter__(self):
        from moomoo import (  # noqa: WPS433
            OpenQuoteContext, OpenSecTradeContext, RET_OK, SecurityFirm,
            SysConfig, TrdEnv, TrdMarket,
        )
        encrypt = bool(self.cfg["rsa_pem"])
        if encrypt:
            f = tempfile.NamedTemporaryFile(
                mode="w", suffix=".pem", delete=False)
            f.write(self.cfg["rsa_pem"])
            f.flush()
            os.chmod(f.name, 0o600)
            f.close()
            self._key_path = f.name
            SysConfig.enable_proto_encrypt(True)
            SysConfig.set_init_rsa_file(f.name)
        self.q = OpenQuoteContext(host=self.cfg["host"],
                                  port=self.cfg["port"],
                                  is_encrypt=encrypt)
        if self.need_trade:
            self.t = OpenSecTradeContext(
                filter_trdmarket=TrdMarket.US, host=self.cfg["host"],
                port=self.cfg["port"], is_encrypt=encrypt,
                security_firm=SecurityFirm.FUTUINC)
            if self.cfg["env"] == "REAL":
                pwd = (self.cfg["pwd_md5"] or "").lower()
                if len(pwd) != 32:
                    raise RuntimeError(
                        "MOOMOO_TRADE_PASSWORD_MD5 must be 32 hex chars")
                ret, data = self.t.unlock_trade(
                    password_md5=pwd, is_unlock=True)
                if ret != RET_OK:
                    raise RuntimeError(f"trade unlock failed: {data}")
        return self

    def __exit__(self, *exc):
        for c in (self.q, self.t):
            try:
                if c:
                    c.close()
            except Exception:  # noqa: BLE001
                pass
        if self._key_path:
            try:
                os.unlink(self._key_path)
            except OSError:
                pass
        return False


def _ok(ret_data):
    from moomoo import RET_OK  # noqa: WPS433
    ret, data = ret_data
    if ret != RET_OK:
        raise RuntimeError(str(data)[:300])
    return data


def _records(df) -> list[dict]:
    return df.to_dict("records") if hasattr(df, "to_dict") else list(df)


class MoomooMarketDataAdapter:
    """Quote surface. High-frequency data is NOT duplicated into
    MongoDB (directive) — callers consume it in memory."""

    def __init__(self, cfg: dict):
        self.cfg = cfg

    async def _run(self, fn):
        async with _SDK_LOCK:
            return await asyncio.to_thread(fn)

    async def quote(self, symbol: str) -> dict:
        code = _us(symbol)

        def _do():
            from moomoo import SubType  # noqa: WPS433
            with _Ctx(self.cfg, need_trade=False) as c:
                _ok(c.q.subscribe([code], [SubType.QUOTE],
                                  subscribe_push=False))
                rows = _records(_ok(c.q.get_stock_quote([code])))
                return rows[0] if rows else {}
        return await self._run(_do)

    async def bid_ask(self, symbol: str) -> dict:
        book = await self.order_book(symbol, depth=1)
        bids, asks = book.get("Bid") or [], book.get("Ask") or []
        return {"bid": bids[0][0] if bids else None,
                "ask": asks[0][0] if asks else None,
                "fetched_at": datetime.now(timezone.utc).isoformat()}

    async def order_book(self, symbol: str, depth: int = 10) -> dict:
        code = _us(symbol)

        def _do():
            from moomoo import SubType  # noqa: WPS433
            with _Ctx(self.cfg, need_trade=False) as c:
                _ok(c.q.subscribe([code], [SubType.ORDER_BOOK],
                                  subscribe_push=False))
                return _ok(c.q.get_order_book(code, num=depth))
        return await self._run(_do)

    async def candles(self, symbol: str, num: int = 100) -> list[dict]:
        code = _us(symbol)

        def _do():
            from moomoo import KLType, SubType  # noqa: WPS433
            with _Ctx(self.cfg, need_trade=False) as c:
                _ok(c.q.subscribe([code], [SubType.K_1M],
                                  subscribe_push=False))
                return _records(_ok(c.q.get_cur_kline(
                    code, num=num, ktype=KLType.K_1M)))
        return await self._run(_do)

    async def market_state(self, symbol: str = "AAPL") -> dict:
        code = _us(symbol)

        def _do():
            with _Ctx(self.cfg, need_trade=False) as c:
                rows = _records(_ok(c.q.get_market_state([code])))
                return rows[0] if rows else {}
        return await self._run(_do)

    async def entitlements(self) -> list[dict]:
        def _do():
            with _Ctx(self.cfg, need_trade=False) as c:
                return _records(_ok(c.q.get_user_security_group(
                    group_type=None))) if False else \
                    _records(_ok(c.q.query_subscription()))
        return await self._run(_do)

    async def option_chain(self, underlying: str, start: str,
                           end: str) -> list[dict]:
        code = _us(underlying)

        def _do():
            with _Ctx(self.cfg, need_trade=False) as c:
                return _records(_ok(c.q.get_option_chain(
                    code, start=start, end=end)))
        return await self._run(_do)


class MoomooBrokerAdapter:
    """Trading surface with V1 guardrails baked in. NEVER falls back
    to another venue — rejections raise and stay observable."""

    broker_name = "moomoo"

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.md = MoomooMarketDataAdapter(cfg)

    async def _run(self, fn):
        async with _SDK_LOCK:
            return await asyncio.to_thread(fn)

    async def _limits(self) -> dict:
        try:
            from db import db  # noqa: WPS433
            doc = await db["runtime_flags"].find_one(
                {"_id": LIMITS_FLAG}, {"_id": 0}, max_time_ms=3000) or {}
        except Exception:  # noqa: BLE001
            doc = {}
        return {**LIMITS_DEFAULTS, **doc}

    async def account(self) -> dict:
        def _do():
            from moomoo import Currency, TrdEnv  # noqa: WPS433
            env = TrdEnv.REAL if self.cfg["env"] == "REAL" else TrdEnv.SIMULATE
            with _Ctx(self.cfg, need_trade=True) as c:
                rows = _records(_ok(c.t.accinfo_query(
                    trd_env=env, acc_id=self.cfg["acc_id"],
                    currency=Currency.USD)))
                return rows[0] if rows else {}
        return await self._run(_do)

    async def buying_power(self) -> Optional[float]:
        acc = await self.account()
        for k in ("us_power", "power", "max_power_short", "cash"):
            v = acc.get(k)
            if v is not None:
                try:
                    return float(v)
                except (TypeError, ValueError):
                    continue
        return None

    async def positions(self) -> list[dict]:
        def _do():
            from moomoo import Currency, TrdEnv, TrdMarket  # noqa: WPS433
            env = TrdEnv.REAL if self.cfg["env"] == "REAL" else TrdEnv.SIMULATE
            with _Ctx(self.cfg, need_trade=True) as c:
                return _records(_ok(c.t.position_list_query(
                    trd_env=env, acc_id=self.cfg["acc_id"],
                    position_market=TrdMarket.US, currency=Currency.USD)))
        return await self._run(_do)

    async def _guard(self, side: str, notional_usd: float,
                     symbol: str) -> None:
        lim = await self._limits()
        if not lim.get("enabled", True):
            raise RuntimeError("moomoo_disabled_by_operator")
        if side == "BUY":
            if notional_usd > float(lim["max_notional_usd"]):
                raise RuntimeError(
                    f"moomoo_notional_cap: ${notional_usd:.2f} > "
                    f"${lim['max_notional_usd']} (V1 tiny-notional)")
            if lim.get("one_position_at_a_time", True):
                pos = [p for p in await self.positions()
                       if float(p.get("qty") or 0) > 0]
                if pos:
                    raise RuntimeError(
                        "moomoo_one_position_limit: "
                        f"{_plain(str(pos[0].get('code')))} already open")
        if lim.get("rth_only", True):
            state = await self.md.market_state(symbol)
            ms = str(state.get("market_state") or "").upper()
            if "OPEN" not in ms or "PRE" in ms or "AFTER" in ms:
                raise RuntimeError(f"moomoo_rth_only: market_state={ms}")

    async def submit_market_order(self, symbol: str,
                                  qty: Optional[float] = None,
                                  notional: Optional[float] = None,
                                  side: str = "BUY",
                                  client_order_id: Optional[str] = None,
                                  mc_receipt: Optional[dict] = None) -> dict:
        """Router-compatible submit. Executes as a marketable LIMIT at
        the touch (price-disciplined, never blind market)."""
        from shared.broker_telemetry import record_submit  # noqa: WPS433
        side = (side or "BUY").upper()
        code = _us(symbol)
        t_trigger = time.monotonic()
        quote_t0 = time.monotonic()
        ba = await self.md.bid_ask(symbol)
        quote_age_ms = round((time.monotonic() - quote_t0) * 1000, 1)
        bid, ask = ba.get("bid"), ba.get("ask")
        if not bid or not ask:
            raise RuntimeError(f"moomoo_no_quote: {code}")
        px = float(ask) if side == "BUY" else float(bid)
        if qty is None:
            if not notional:
                raise ValueError("qty or notional required")
            qty = round(float(notional) / px, 6)
        notional_usd = qty * px
        await self._guard(side, notional_usd, symbol)
        t_submit = time.monotonic()

        def _do():
            from moomoo import (  # noqa: WPS433
                OrderType, TrdEnv, TrdSide,
            )
            env = TrdEnv.REAL if self.cfg["env"] == "REAL" else TrdEnv.SIMULATE
            with _Ctx(self.cfg, need_trade=True) as c:
                return _records(_ok(c.t.place_order(
                    price=px, qty=qty, code=code,
                    trd_side=TrdSide.BUY if side == "BUY" else TrdSide.SELL,
                    order_type=OrderType.NORMAL, trd_env=env,
                    acc_id=self.cfg["acc_id"],
                    remark=(client_order_id or "")[:60])))
        try:
            rows = await self._run(_do)
        except Exception as exc:
            await record_submit(
                broker="moomoo", symbol=_plain(code), side=side,
                quote_age_ms=quote_age_ms, bid=bid, ask=ask,
                spread=float(ask) - float(bid),
                trigger_ts=datetime.now(timezone.utc).isoformat(),
                submit_latency_ms=round(
                    (t_submit - t_trigger) * 1000, 1),
                ack_latency_ms=None, order_id=None,
                limit_price=px, rejection_reason=str(exc)[:200])
            raise
        ack_ms = round((time.monotonic() - t_submit) * 1000, 1)
        row = rows[0] if rows else {}
        order_id = str(row.get("order_id") or "")
        await record_submit(
            broker="moomoo", symbol=_plain(code), side=side,
            quote_age_ms=quote_age_ms, bid=bid, ask=ask,
            spread=float(ask) - float(bid),
            trigger_ts=datetime.now(timezone.utc).isoformat(),
            submit_latency_ms=round((t_submit - t_trigger) * 1000, 1),
            ack_latency_ms=ack_ms, order_id=order_id,
            limit_price=px, rejection_reason=None)
        return {"broker": "moomoo", "order_id": order_id,
                "status": str(row.get("order_status") or "submitted"),
                "symbol": _plain(code), "side": side,
                "qty": qty, "limit_price": px,
                "notional_usd": round(notional_usd, 2),
                "client_order_id": client_order_id,
                "trading_env": self.cfg["env"],
                "order_style": "marketable_limit",
                "raw": {k: str(v) for k, v in row.items()}}

    async def cancel_order(self, order_id: str) -> dict:
        def _do():
            from moomoo import ModifyOrderOp, TrdEnv  # noqa: WPS433
            env = TrdEnv.REAL if self.cfg["env"] == "REAL" else TrdEnv.SIMULATE
            with _Ctx(self.cfg, need_trade=True) as c:
                return _records(_ok(c.t.modify_order(
                    modify_order_op=ModifyOrderOp.CANCEL,
                    order_id=order_id, qty=0, price=0,
                    trd_env=env, acc_id=self.cfg["acc_id"])))
        rows = await self._run(_do)
        return {"ok": True, "order_id": order_id,
                "raw": rows[0] if rows else {}}

    async def get_order(self, order_id: str) -> dict:
        def _do():
            from moomoo import TrdEnv  # noqa: WPS433
            env = TrdEnv.REAL if self.cfg["env"] == "REAL" else TrdEnv.SIMULATE
            with _Ctx(self.cfg, need_trade=True) as c:
                rows = _records(_ok(c.t.order_list_query(
                    order_id=order_id, trd_env=env,
                    acc_id=self.cfg["acc_id"], refresh_cache=True)))
                return rows[0] if rows else {}
        row = await self._run(_do)
        status = str(row.get("order_status") or "").upper()
        mapped = ("FILLED" if "FILLED_ALL" in status
                  else "CANCELED" if "CANCEL" in status
                  else "FAILED" if status in ("FAILED", "DISABLED",
                                              "DELETED") else "WORKING")
        return {"order_id": order_id, "status": mapped,
                "filled_qty": row.get("dealt_qty"),
                "filled_avg_price": row.get("dealt_avg_price"),
                "raw": {k: str(v) for k, v in row.items()}}

    async def fills(self) -> list[dict]:
        def _do():
            from moomoo import TrdEnv, TrdMarket  # noqa: WPS433
            env = TrdEnv.REAL if self.cfg["env"] == "REAL" else TrdEnv.SIMULATE
            with _Ctx(self.cfg, need_trade=True) as c:
                return _records(_ok(c.t.deal_list_query(
                    deal_market=TrdMarket.US, trd_env=env,
                    acc_id=self.cfg["acc_id"], refresh_cache=True)))
        return await self._run(_do)

    async def submit_option_limit_order(self, **kwargs) -> dict:
        """Options are schema-ready but autonomous option orders stay
        DISABLED until the options execution policy exists (directive
        §8). Flip runtime_flags moomoo_limits.allow_autonomous_options
        only after that policy ships."""
        lim = await self._limits()
        if not lim.get("allow_autonomous_options"):
            raise RuntimeError(
                "moomoo_options_disabled: options execution policy not "
                "ready — schema is implemented, autonomy is not enabled")
        raise RuntimeError("moomoo_options_not_implemented_v1")


async def get_moomoo_adapter() -> Optional[MoomooBrokerAdapter]:
    """ADAPTER_LOADERS-compatible resolver. None = unconfigured →
    router fails CLOSED (BrokerRouteBlocked), never re-venues."""
    cfg = moomoo_config()
    if not cfg:
        return None
    return MoomooBrokerAdapter(cfg)
