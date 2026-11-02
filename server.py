"""Paradex crypto version of the trader (same page, same daily lock, risk = RISK_PCT % of account value).

Every number used to trade (entry, size, stop level) comes from Paradex's own prices.
Settings come from environment variables, which trader_app.py fills in from the setup wizard:
  ACCESS_TOKEN  PARADEX_MODE=l1 (L1_ADDRESS L1_PRIVATE_KEY)  or  subkey (L2_ADDRESS L2_PRIVATE_KEY)  [RISK_PCT=1] [RISK_USD=1 (fallback)] [MAX_NOTIONAL_USD=5000]
  [LOCK_TZ] [LOCK_RESET_HOUR] [LOCK_FILE] [STATS_START=2026-11-02]
Standalone:  uvicorn server:app --host 0.0.0.0 --port 8000 --env-file .env
"""
import os
import json
import time
import base64
import logging
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Optional
from decimal import Decimal, ROUND_DOWN, ROUND_CEILING, ROUND_FLOOR

import requests
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from paradex_py import Paradex, ParadexSubkey
from paradex_py.environment import PROD
from paradex_py.common.order import Order, OrderType, OrderSide

import daylock as lock

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("trader")

API = "https://api.prod.paradex.trade/v1"
SYMBOL = "BTC-USD-PERP"
RISK_PCT = Decimal(os.getenv("RISK_PCT", "1"))      # % of account value risked per trade
RISK_USD = Decimal(os.getenv("RISK_USD", "1"))      # fallback if the account value cannot be read
MAX_NOTIONAL = Decimal(os.getenv("MAX_NOTIONAL_USD", "5000"))
TICK, STEP = Decimal("0.1"), Decimal("0.00001")  # BTC-USD-PERP price tick / size step
TOKEN = os.environ["ACCESS_TOKEN"]

# ---- Daily lock: one trade per day, shared with the other brokers (daylock.py). ----
# The lock file also remembers WHY today's trade ended, so the lock screen can show it.
DEFAULT_REASON = "The position was closed on Paradex (your stop was hit, or it was closed or liquidated there)."


def set_lock_reason(reason):
    """Record WHY today's trade ended."""
    try:
        tmp = lock.FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"day": lock.today(), "at": int(time.time()), "reason": reason}, f)
        os.replace(tmp, lock.FILE)   # atomic write
        log.warning("Lock reason: %s", reason)
    except Exception as e:
        log.error("Could not save lock reason: %s", e)


def lock_reason():
    try:
        with open(lock.FILE, encoding="utf-8") as f:
            return json.load(f).get("reason") or DEFAULT_REASON
    except Exception:
        return "Lock file could not be read, so the app stays locked to be safe."


# Zero-fee RETAIL orders need an "interactive" login token. L1 mode logs in with your wallet (like the Paradex website
# does); subkey mode logs in with a trading-only L2 key. Either way we ask for the interactive token below.
MODE = os.getenv("PARADEX_MODE", "subkey")
if MODE == "l1":
    paradex = Paradex(env=PROD, l1_address=os.environ["L1_ADDRESS"], l1_private_key=os.environ["L1_PRIVATE_KEY"])
else:
    paradex = ParadexSubkey(
        env=PROD,
        l2_private_key=os.environ["L2_PRIVATE_KEY"],
        l2_address=os.environ["L2_ADDRESS"],
    )
# Request an interactive (retail, zero-fee) token, and keep it on refresh
paradex.api_client.auth_params = {"token_usage": "interactive"}
paradex.api_client.auth(params=paradex.api_client.auth_params)


def _log_token_claims():
    """Log what the login token says, and return it ({} if it cannot be read)."""
    try:
        tok = paradex.api_client.client.headers.get("Authorization", "").replace("Bearer ", "")
        part = tok.split(".")[1]
        part += "=" * (-len(part) % 4)
        claims = json.loads(base64.urlsafe_b64decode(part))
        log.info("Login token claims: %s", claims)
        return claims
    except Exception as e:
        log.warning("Could not read token claims: %s", e)
        return {}


# True only if the token itself says "interactive" (that is what makes orders Retail / 0% fee)
RETAIL = "interactive" in json.dumps(_log_token_claims()).lower()
if not RETAIL:
    log.warning("The login token is NOT marked interactive: orders may be charged Pro (API) fees. Mode=%s", MODE)

# ---- Trade history: every CLOSED trade since STATS_START, rebuilt from Paradex fills ----
try:
    STATS_START = datetime.fromisoformat(os.environ["STATS_START"]).replace(tzinfo=ZoneInfo("Asia/Manila"))
except Exception:
    STATS_START = datetime(2026, 11, 2, 0, 0, tzinfo=ZoneInfo("Asia/Manila"))   # 2 Nov 2026, 00:00 PHT
_stats_cache = {"at": 0.0, "data": None}


def _fills_page(params):
    try:
        return paradex.api_client.fetch_fills(params)
    except Exception as e:
        log.warning("api_client.fetch_fills failed (%s), trying the REST call", e)
    tok = paradex.api_client.client.headers.get("Authorization", "")
    r = requests.get(f"{API}/fills", params=params, headers={"Authorization": tok}, timeout=10)
    r.raise_for_status()
    return r.json()


def fetch_fills_since(start_ms):
    rows, seen, cursor = [], set(), None
    for _ in range(40):
        params = {"market": SYMBOL, "start_at": start_ms,
                  "end_at": int(time.time() * 1000) + 60000, "page_size": 100}
        if cursor:
            params["cursor"] = cursor
        res = _fills_page(params)
        page = res.get("results", []) if isinstance(res, dict) else (res or [])
        for f in page:
            fid = f.get("id")
            if fid is not None and fid in seen:
                continue
            seen.add(fid)
            rows.append(f)
        cursor = res.get("next") if isinstance(res, dict) else None
        if not cursor or not page:
            break
    rows.sort(key=lambda f: int(float(f.get("created_at") or 0)))
    return rows


def build_stats(fills):
    """A trade = fills from flat to flat. PnL = sell value - buy value (fees are 0 on the interactive token)."""
    trades, net, cash = [], Decimal("0"), Decimal("0")
    for f in fills:
        if f.get("market") not in (None, SYMBOL):
            continue
        size, price = Decimal(str(f["size"])), Decimal(str(f["price"]))
        sign = 1 if str(f.get("side", "")).upper() == "BUY" else -1
        net += sign * size
        cash -= sign * size * price
        if abs(net) < Decimal("0.000001"):      # back to flat: the trade is closed
            trades.append({"ts": int(float(f.get("created_at") or 0)) // 1000, "pnl": float(cash)})
            net, cash = Decimal("0"), Decimal("0")
    cum, out = 0.0, []
    for i, t in enumerate(trades, 1):
        cum += t["pnl"]
        out.append({"n": i, "ts": t["ts"], "pnl": round(t["pnl"], 4), "cum": round(cum, 4)})
    wins = [t["pnl"] for t in trades if t["pnl"] > 0]
    losses = [t["pnl"] for t in trades if t["pnl"] < 0]
    gross_loss = -sum(losses)
    return {
        "since": STATS_START.isoformat(), "trades": out, "count": len(trades),
        "total": round(cum, 4), "wins": len(wins), "losses": len(losses),
        "winrate": round(100.0 * len(wins) / len(trades), 1) if trades else None,
        "profit_factor": round(sum(wins) / gross_loss, 2) if gross_loss > 0 else None,
    }


def get_stats():
    if _stats_cache["data"] and time.time() - _stats_cache["at"] < 10:
        return _stats_cache["data"]
    data = build_stats(fetch_fills_since(int(STATS_START.timestamp() * 1000)))
    _stats_cache["data"], _stats_cache["at"] = data, time.time()
    return data


state = {"stop": None, "stop_id": None}
app = FastAPI()


@app.exception_handler(RequestValidationError)
async def validation_handler(request: Request, exc: RequestValidationError):
    """Return validation errors as plain text (so the page never shows [object Object])."""
    body = await request.body()
    log.error("422 on %s: errors=%s body=%s", request.url.path, exc.errors(), body[:500])
    msg = "; ".join(f"{'.'.join(str(x) for x in e['loc'])}: {e['msg']}" for e in exc.errors())
    return JSONResponse(status_code=422, content={"detail": msg})


@app.middleware("http")
async def no_cache(request: Request, call_next):
    resp = await call_next(request)
    if not request.url.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


class MoveReq(BaseModel):
    stop_price: float  # the stop line's price on the chart (Paradex mark price scale)


class TradeReq(BaseModel):
    side: str     # "long" | "short"
    dist: Optional[float] = None        # risk distance in USD (chart price minus dragged line)
    stop_price: Optional[float] = None  # older page versions sent these two instead; only their
    ref_price: Optional[float] = None   # difference is used, never as a price

    class Config:
        extra = "allow"   # so we can log whatever an unexpected page sends


def check(tok):
    if tok != TOKEN:
        raise HTTPException(401, "Wrong token")


_eq = {"v": None, "t": 0.0}


def _account_value():
    """Account value in USD from Paradex, or None if it cannot be read."""
    try:
        r = paradex.api_client.fetch_account_summary()
        v = r.get("account_value") if isinstance(r, dict) else getattr(r, "account_value", None)
        if v is not None:
            return Decimal(str(v))
    except Exception as e:
        log.warning("fetch_account_summary failed (%s), trying the REST call", e)
    try:
        tok = paradex.api_client.client.headers.get("Authorization", "")
        r = requests.get(f"{API}/account", headers={"Authorization": tok}, timeout=10)
        r.raise_for_status()
        v = r.json().get("account_value")
        if v is not None:
            return Decimal(str(v))
    except Exception as e:
        log.warning("Could not read the account value: %s", e)
    return None


def risk_amount():
    """RISK_PCT % of account value (cached 15 s); falls back to RISK_USD."""
    if time.time() - _eq["t"] > 15:
        v = _account_value()
        _eq["v"] = v if v is not None and v > 0 else None
        _eq["t"] = time.time()
    if _eq["v"]:
        return max((_eq["v"] * RISK_PCT / 100).quantize(Decimal("0.01")), Decimal("0.01"))
    return RISK_USD


def paradex_bbo():
    """Paradex best bid and ask for the market (public endpoint)."""
    r = requests.get(f"{API}/bbo/{SYMBOL}", timeout=5)
    r.raise_for_status()
    j = r.json()
    return Decimal(j["bid"]), Decimal(j["ask"])


def position():
    raw = paradex.api_client.fetch_positions()
    rows = raw.get("results", []) if isinstance(raw, dict) else (raw or [])
    for p in rows:
        size = float(p.get("size", 0) or 0)
        if p.get("market") == SYMBOL and size != 0:
            side = (p.get("side") or ("LONG" if size > 0 else "SHORT")).upper()
            state["raw"] = dict(p)   # kept so /api/status can log what Paradex really sent
            return {
                "side": side,
                "size": abs(size),
                # Paradex's UI shows the entry in USD ("average_entry_price_usd"). The plain
                # "average_entry_price" is in USDC, about 0.03% higher (~$20 at BTC 78k).
                "entry": float(p.get("average_entry_price_usd") or p.get("average_entry_price") or 0),
            }
    return None


def send(side, kind, size, instruction, **kw):
    order = Order(market=SYMBOL, order_side=side, order_type=kind, size=size,
                  limit_price=Decimal("0"), instruction=instruction, **kw)
    return paradex.api_client.submit_order(order)


def flatten():
    """Close any open position at market FIRST (reduce-only), then cancel the resting stop.

    Closing first means the position is never left unprotected while orders are cancelled.
    """
    p = position()
    if p:
        side = OrderSide.Sell if p["side"] == "LONG" else OrderSide.Buy
        send(side, OrderType.Market, Decimal(str(p["size"])).quantize(STEP), "IOC", reduce_only=True)
    try:
        paradex.api_client.cancel_all_orders({"market": SYMBOL})
    except Exception as e:
        log.warning("cancel_all_orders failed: %s", e)
    state["stop"] = None
    state["stop_id"] = None
    return p


def place_stop(close_side, size, stop):
    """Place a reduce-only stop-market order and return its id (or None if not reported)."""
    res = send(close_side, OrderType.StopMarket, size, "GTC", trigger_price=stop, reduce_only=True)
    return res.get("id") if isinstance(res, dict) else None


def _num(x):
    return float(x)


@app.get("/api/klines")
def klines(tf: str = "1m", n: int = 500, x_token: str = Header(None)):
    """Paradex candles for the chart (proxied so the page only talks to this server)."""
    check(x_token)
    res = {"1m": 1, "5m": 5}.get(tf)
    if not res:
        raise HTTPException(400, "tf must be 1m or 5m")
    n = max(1, min(int(n), 500))
    end = int(time.time() * 1000)
    start = end - n * res * 60 * 1000
    try:
        r = requests.get(f"{API}/markets/klines", timeout=8, params={
            "symbol": SYMBOL, "resolution": res, "start_at": start, "end_at": end, "price_kind": "mark"})
        r.raise_for_status()
        rows = r.json().get("results", [])
    except Exception as e:
        body = ""
        try:
            body = r.text[:200]
        except Exception:
            pass
        raise HTTPException(502, f"Could not load Paradex candles: {e} {body}")
    out = {}
    for x in rows:
        try:
            if isinstance(x, dict):
                ts = x.get("time") or x.get("timestamp") or x.get("t")
                o, h, l, c = x["open"], x["high"], x["low"], x["close"]
            else:
                ts, o, h, l, c = x[0], x[1], x[2], x[3], x[4]
            ts = int(ts)
            ts = ts // 1000 if ts > 10**11 else ts
            out[ts] = {"time": ts, "open": _num(o), "high": _num(h), "low": _num(l), "close": _num(c)}
        except Exception:
            continue
    if not out:
        raise HTTPException(502, "Paradex returned no candles.")
    times = sorted(out)
    step = res * 60
    if any(t % step for t in times):  # times not on the candle grid: return as they are
        return [out[t] for t in times]
    # Fill minutes with no candle with a flat candle at the previous close, so the chart has no gaps
    filled, prev, t = [], None, times[0]
    last_bucket = max(times[-1], int(time.time()) // step * step)
    while t <= last_bucket:
        if t in out:
            prev = out[t]
            filled.append(prev)
        elif prev:
            c = prev["close"]
            filled.append({"time": t, "open": c, "high": c, "low": c, "close": c})
        t += step
    return filled


@app.get("/api/price")
def price(x_token: str = Header(None)):
    """Paradex mark price (what the Paradex chart shows), plus bid and ask."""
    check(x_token)
    try:
        r = requests.get(f"{API}/markets/summary", params={"market": SYMBOL}, timeout=5)
        r.raise_for_status()
        m = r.json()["results"][0]
        return {"mark": float(m["mark_price"]), "bid": float(m["bid"]), "ask": float(m["ask"])}
    except Exception as e:
        raise HTTPException(502, f"Could not read the Paradex price: {e}")


@app.get("/api/status")
def status(x_token: str = Header(None)):
    check(x_token)
    p = position()
    if p:
        p["stop"] = state["stop"]
        if state.get("raw") != state.get("raw_logged"):
            log.info("RAW POSITION ROW FROM PARADEX: %s", state["raw"])
            state["raw_logged"] = state["raw"]
    else:
        state["stop"] = None
        state["stop_id"] = None
    try:
        bid, ask = paradex_bbo()
        para = float((bid + ask) / 2)
    except Exception:
        para = None
    # Locked once today's trade is finished (position closed by Stop button or by the stop order)
    locked = lock.traded_today() and p is None
    return {"position": p, "raw": state.get("raw") if p else None, "paradex_price": para,
            "venue": "Paradex" + (" RETAIL 0% FEE" if RETAIL else ""), "inst": SYMBOL,
            "risk_usd": float(risk_amount()), "risk_pct": float(RISK_PCT), "max_notional_usd": float(MAX_NOTIONAL),
            "pdec": 1, "qdec": 5, "unit": "BTC", "step": float(STEP), "min_dist": float(TICK),
            "conv": 1, "max_qty": None,
            "locked": locked, "unlock_at": lock.next_unlock_ts() if locked else None,
            "lock_reason": lock_reason() if locked else None}


@app.get("/api/stats")
def stats(x_token: str = Header(None)):
    check(x_token)
    try:
        return get_stats()
    except Exception as e:
        log.error("Stats failed: %s", e)
        raise HTTPException(502, f"Could not load trade history from Paradex: {e}")


@app.post("/api/trade")
def trade(req: TradeReq, x_token: str = Header(None)):
    check(x_token)
    if req.side not in ("long", "short"):
        raise HTTPException(400, "side must be long or short")
    if lock.traded_today():
        raise HTTPException(423, "Locked: today's trade is already used. Back tomorrow.")
    if position():
        raise HTTPException(409, "A position is already open. Press Stop first.")
    is_long = req.side == "long"

    # 1. Paradex's own price. The page sends only the side and the risk distance in USD;
    #    no Binance price is used for entry, size or stop.
    try:
        bid, ask = paradex_bbo()
    except Exception as e:
        raise HTTPException(502, f"Could not read the Paradex price: {e}")
    d = req.dist
    if d is None and req.stop_price is not None and req.ref_price is not None:
        d = abs(req.ref_price - req.stop_price)
    if d is None:
        log.error("Trade request without dist. Body received: %s", req.dict())
        raise HTTPException(400, "No distance received.")
    dist = Decimal(str(d))
    if not dist.is_finite() or dist < Decimal("0.1"):
        raise HTTPException(400, "Stop distance is too small.")

    # 2. Direction check against Paradex's price
    if is_long and (ask - dist) <= 0:
        raise HTTPException(400, "Stop distance is larger than the price.")

    # 3. Size from that distance, checked against the Paradex entry price
    est_entry = ask if is_long else bid
    qty = (risk_amount() / dist).quantize(STEP, rounding=ROUND_DOWN)
    if qty <= 0:
        raise HTTPException(400, "Line is too far away: size rounds to zero.")
    notional = qty * est_entry
    if notional > MAX_NOTIONAL:
        raise HTTPException(400, f"Line is too close: size ${notional:,.0f} exceeds the ${MAX_NOTIONAL:,.0f} cap.")

    # 4. Enter at market on Paradex
    open_side = OrderSide.Buy if is_long else OrderSide.Sell
    close_side = OrderSide.Sell if is_long else OrderSide.Buy
    try:
        send(open_side, OrderType.Market, qty, "IOC")
    except Exception as e:
        raise HTTPException(502, f"Entry failed: {e}")

    pos = None
    for _ in range(4):
        time.sleep(1.0)
        pos = position()
        if pos:
            break
    if not pos:
        raise HTTPException(502, "Entry order did not fill. No stop placed.")
    lock.mark_traded()   # the entry filled: today's one trade is used, even if it is closed right away

    # 5. Stop sits the same distance from the real Paradex fill price
    entry = Decimal(str(pos["entry"])) if pos["entry"] > 0 else est_entry
    if abs(entry - est_entry) > est_entry * Decimal("0.005"):   # >0.5% off the market: bad data
        log.warning("Position entry %s is far from market %s, using market", entry, est_entry)
        entry = est_entry
    if is_long:
        stop = ((entry - dist) / TICK).to_integral_value(rounding=ROUND_CEILING) * TICK
    else:
        stop = ((entry + dist) / TICK).to_integral_value(rounding=ROUND_FLOOR) * TICK

    try:  # if the market already ran past the stop, get out instead of placing it
        bid2, ask2 = paradex_bbo()
        log.info("Pre-stop check: side=%s entry=%s dist=%s stop=%s bid=%s ask=%s raw=%s",
                 req.side, entry, dist, stop, bid2, ask2, state.get("raw"))
        if (is_long and stop >= bid2) or (not is_long and stop <= ask2):
            flatten()
            why = (f"SAFETY CLOSE: right after entry, the stop level ({stop}) was already past the market "
                   f"(bid {bid2}, ask {ask2}, entry {entry}, distance {dist}), so the position was closed "
                   f"instead of placing a stop that would trigger instantly.")
            set_lock_reason(why)
            raise HTTPException(409, why)
    except HTTPException:
        raise
    except Exception as e:
        log.warning("Pre-stop price check skipped: %s", e)

    try:
        state["stop_id"] = place_stop(close_side, Decimal(str(pos["size"])).quantize(STEP), stop)
    except Exception as e:
        log.error("Stop failed, flattening: %s", e)
        flatten()
        why = f"SAFETY CLOSE: Paradex rejected the stop order (stop {stop}), so the position was closed. Reason: {e}"
        set_lock_reason(why)
        raise HTTPException(502, why)

    state["stop"] = float(stop)
    log.info("Opened %s %s entry %s stop %s (distance %s)", req.side, qty, entry, stop, dist)
    return {"ok": True, "side": req.side, "qty": float(qty), "entry": float(entry), "stop": float(stop)}


@app.post("/api/move_stop")
def move_stop(req: MoveReq, x_token: str = Header(None)):
    """Move the stop closer or further away. Uses Paradex prices for every check."""
    check(x_token)
    pos = position()
    if not pos:
        raise HTTPException(409, "No open position.")
    is_long = pos["side"] == "LONG"
    # The chart is Paradex's mark price, so the line's price is used as it is.
    new = (Decimal(str(req.stop_price)) / TICK).to_integral_value() * TICK
    try:
        r = requests.get(f"{API}/markets/summary", params={"market": SYMBOL}, timeout=5)
        r.raise_for_status()
        mark = Decimal(str(r.json()["results"][0]["mark_price"]))
    except Exception as e:
        raise HTTPException(502, f"Could not read the Paradex price: {e}")
    if is_long and new >= mark:
        raise HTTPException(409, f"For a long, the stop must be below the Paradex mark price ({mark}).")
    if not is_long and new <= mark:
        raise HTTPException(409, f"For a short, the stop must be above the Paradex mark price ({mark}).")
    if new <= 0:
        raise HTTPException(400, "Invalid stop price.")

    size = Decimal(str(pos["size"])).quantize(STEP)
    close_side = OrderSide.Sell if is_long else OrderSide.Buy
    old_id = state.get("stop_id")
    warning = None
    new_id, placed = None, False

    if old_id:  # safest: place the new stop first, then remove the old one
        try:
            new_id = place_stop(close_side, size, new)
            placed = True
            try:
                paradex.api_client.cancel_order(old_id)
            except Exception as e:
                log.warning("Could not cancel old stop %s: %s", old_id, e)
                warning = "New stop is set, but the old stop could not be cancelled."
        except Exception as e:
            log.warning("Place-first failed (%s), falling back to cancel then place", e)

    if not placed:
        try:
            paradex.api_client.cancel_all_orders({"market": SYMBOL})
        except Exception as e:
            log.warning("cancel_all_orders failed: %s", e)
        try:
            new_id = place_stop(close_side, size, new)
        except Exception as e:
            log.error("New stop failed, flattening: %s", e)
            flatten()
            why = f"SAFETY CLOSE: the new stop ({new}) could not be placed, so the position was closed. Reason: {e}"
            set_lock_reason(why)
            raise HTTPException(502, why)

    state["stop"] = float(new)
    state["stop_id"] = new_id
    log.info("Moved stop to %s (mark %s)", new, mark)
    return {"ok": True, "stop": float(new), "warning": warning}


@app.post("/api/stop")
def stop(x_token: str = Header(None)):
    check(x_token)
    closed = flatten()
    if closed:
        set_lock_reason("You pressed Stop and closed the position manually.")
    return {"ok": True, "closed": closed}


STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
log.info("Serving page from %s", STATIC_DIR)
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True))
