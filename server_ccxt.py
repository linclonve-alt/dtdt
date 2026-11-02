"""Crypto futures version of the trader through CCXT: Binance, Bybit, OKX, Bitget, Gate, KuCoin Futures, Hyperliquid.

Same page, same one-trade-a-day lock, risk = RISK_PCT % of the account value. Needs the exchange in ONE-WAY position mode.
Flow (same as the Paradex server): market entry, then a reduce-only stop-market order the same distance from the real
fill. If the stop cannot be placed the position is closed again. If even that fails you are told to close it by hand.

Settings come from environment variables, which trader_app.py fills in from the setup wizard:
  ACCESS_TOKEN  EXCHANGE_ID  EX_KEY  [EX_SECRET] [EX_PASSWORD] [EX_WALLET (Hyperliquid)] [EX_LIVE=0] [EX_PAIRS=BTC_USDT,...]
  [RISK_PCT=1] [RISK_USD=10 (fallback)] [EX_MAX_LEVERAGE=10] [EX_MAX_NOTIONAL_USD=5000 (fallback)] [EX_LEVERAGE=10 (KuCoin)]
"""
import os
import time
import logging
from decimal import Decimal, ROUND_DOWN
from typing import Optional

import ccxt
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import cexlib
import daylock as lock

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("trader")

TOKEN = os.environ["ACCESS_TOKEN"]
EID = os.environ["EXCHANGE_ID"]
INFO = cexlib.EXCHANGES[EID]
LABEL, QUOTE = INFO["label"], INFO["quote"]
LIVE = os.getenv("EX_LIVE", "0") == "1"
RISK_PCT = Decimal(os.getenv("RISK_PCT", "1"))
RISK_FALLBACK = Decimal(os.getenv("RISK_USD", "10"))
MAX_LEV = Decimal(os.getenv("EX_MAX_LEVERAGE", "10"))                 # biggest position = account value x this
MAX_NOTIONAL_FALLBACK = Decimal(os.getenv("EX_MAX_NOTIONAL_USD", "5000"))   # used while the account value is unknown
LEVERAGE = int(os.getenv("EX_LEVERAGE", "10"))                        # only KuCoin needs this on every order
TFS = ("1m", "5m", "15m", "30m", "1h")

ex = cexlib.build_exchange(EID, key=os.environ.get("EX_KEY", ""), secret=os.environ.get("EX_SECRET", ""),
                           password=os.environ.get("EX_PASSWORD", ""), wallet=os.environ.get("EX_WALLET", ""), live=LIVE)
ex.load_markets()


def _step(v):
    """Smallest price/amount increment from a CCXT 'precision' value, or None if it cannot be told."""
    if v is None:
        return None
    d = Decimal(str(v))
    if ex.precisionMode == ccxt.TICK_SIZE:
        return d
    if ex.precisionMode == ccxt.DECIMAL_PLACES:
        return Decimal(1).scaleb(-int(d))
    return None


def _probe(fn, sym):
    """Fallback: how many decimals does the exchange accept? Ask it to round a long number."""
    s = str(fn(sym, 1.23456789))
    return Decimal(1).scaleb(-len(s.split(".")[1])) if "." in s else Decimal(1)


META, SYM, PAIR_OF = {}, {}, {}
for _pair in [x.strip() for x in os.getenv("EX_PAIRS", f"BTC_{QUOTE}").split(",") if x.strip()]:
    _sym = cexlib.symbol_for(_pair)
    _m = ex.markets.get(_sym)
    if not _m:
        log.warning("%s has no market %s, skipping %s", LABEL, _sym, _pair)
        continue
    _pr = _m.get("precision") or {}
    _tick = _step(_pr.get("price")) or _probe(ex.price_to_precision, _sym)
    _astep = _step(_pr.get("amount")) or _probe(ex.amount_to_precision, _sym)      # in contracts
    _cs = Decimal(str(_m.get("contractSize") or 1))                                # base currency per contract
    _min = ((_m.get("limits") or {}).get("amount") or {}).get("min")
    _step_base = _astep * _cs                                                      # base currency per lot
    META[_pair] = {"sym": _sym, "tick": _tick, "pdec": max(0, -_tick.as_tuple().exponent),
                   "astep": _astep, "cs": _cs, "step": _step_base, "qdec": max(0, -_step_base.as_tuple().exponent),
                   "min": Decimal(str(_min)) if _min else None, "base": _m.get("base") or _pair.split("_")[0]}
    SYM[_pair], PAIR_OF[_sym] = _sym, _pair
PAIRS = [p for p in SYM]
if not PAIRS:
    raise RuntimeError(f"None of the chosen pairs exist on {LABEL}.")
DEFAULT = PAIRS[0]
log.info("%s %s, pairs %s", LABEL, "LIVE" if LIVE else "TESTNET", PAIRS)

app = FastAPI()
state = {"stop": None, "stop_id": None, "sym": None, "contracts": None}
_eq = {"v": None, "t": 0.0}


@app.exception_handler(RequestValidationError)
async def validation_handler(request: Request, exc: RequestValidationError):
    msg = "; ".join(f"{'.'.join(str(x) for x in e['loc'])}: {e['msg']}" for e in exc.errors())
    return JSONResponse(status_code=422, content={"detail": msg})


@app.middleware("http")
async def no_cache(request: Request, call_next):
    resp = await call_next(request)
    if not request.url.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


class MoveReq(BaseModel):
    stop_price: float


class TradeReq(BaseModel):
    side: str
    dist: Optional[float] = None
    inst: Optional[str] = None

    class Config:
        extra = "allow"


def check(tok):
    if tok != TOKEN:
        raise HTTPException(401, "Wrong token")


def M(inst):
    inst = inst or DEFAULT
    if inst not in META:
        raise HTTPException(400, f"Unknown pair {inst}")
    return inst, META[inst]


def ex_call(what, fn, *a, **kw):
    """Run a CCXT call; turn any exchange error into a readable 502 for the page."""
    try:
        return fn(*a, **kw)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"{LABEL}: {what} failed: {str(e)[:300]}")


def _account_value():
    try:
        b = ex.fetch_balance()
        v = (b.get("total") or {}).get(QUOTE)
        if v is None:
            v = (b.get(QUOTE) or {}).get("total")
        return Decimal(str(v)) if v is not None else None
    except Exception as e:
        log.warning("Could not read the account value: %s", e)
        return None


def risk_amount():
    """RISK_PCT % of the account value (cached 15 s); falls back to RISK_USD."""
    if time.time() - _eq["t"] > 15:
        v = _account_value()
        _eq["v"] = v if v is not None and v > 0 else None
        _eq["t"] = time.time()
    if _eq["v"]:
        return max((_eq["v"] * RISK_PCT / 100).quantize(Decimal("0.01")), Decimal("0.01"))
    return RISK_FALLBACK


def max_notional():
    return _eq["v"] * MAX_LEV if _eq["v"] else MAX_NOTIONAL_FALLBACK


def bidask(sym):
    t = ex_call("price", ex.fetch_ticker, sym)
    last = t.get("last") or t.get("close")
    bid, ask = t.get("bid") or last, t.get("ask") or last
    if not bid or not ask:
        raise HTTPException(502, f"{LABEL} returned no price for {sym}.")
    return Decimal(str(bid)), Decimal(str(ask))


def position():
    """The open position on any of the chosen pairs (one trade a day, so at most one)."""
    rows = ex_call("reading positions", ex.fetch_positions, list(SYM.values()))
    for r in rows or []:
        pair = PAIR_OF.get(r.get("symbol"))
        c = Decimal(str(r.get("contracts") or 0))
        side = str(r.get("side") or "").lower()
        if not pair or c == 0 or side not in ("long", "short"):
            continue
        state["sym"], state["contracts"] = r["symbol"], c
        return {"inst": pair, "side": side.upper(), "size": float(c * META[pair]["cs"]),
                "entry": float(r.get("entryPrice") or 0), "stop": state["stop"]}
    state["stop"] = state["stop_id"] = state["sym"] = state["contracts"] = None
    return None


def _order(sym, is_buy, contracts, params, ref_price=None):
    """Market order. Hyperliquid has no true market orders, so it is given a reference price to work from."""
    amt = float(ex.amount_to_precision(sym, contracts))
    px = float(ref_price) if (EID == "hyperliquid" and ref_price is not None) else None
    if EID == "kucoinfutures":
        params = {**params, "leverage": LEVERAGE}
    return ex.create_order(sym, "market", "buy" if is_buy else "sell", amt, px, params)


def place_stop(sym, close_is_buy, contracts, stop):
    o = _order(sym, close_is_buy, contracts,
               {"stopLossPrice": float(ex.price_to_precision(sym, stop)), "reduceOnly": True}, ref_price=stop)
    return o.get("id") if isinstance(o, dict) else None


def cancel_stop(oid, sym):
    """Cancel one stop order. Exchanges want different flags for conditional orders, so try each."""
    for params in ({"trigger": True}, {"stop": True}, {}):
        try:
            ex.cancel_order(oid, sym, params)
            return True
        except Exception as e:
            err = e
    log.warning("Could not cancel stop %s: %s", oid, err)
    return False


def cancel_all_stops(sym):
    for params in ({"trigger": True}, {"stop": True}):
        try:
            ex.cancel_all_orders(sym, params)
            return True
        except Exception as e:
            err = e
    log.warning("Could not cancel stops on %s: %s", sym, err)
    return False


def flatten():
    """Close any open position at market FIRST (reduce-only), then cancel the resting stop."""
    p = position()
    if p:
        sym, c = state["sym"], state["contracts"]
        bid, ask = bidask(sym)
        is_buy = p["side"] == "SHORT"          # closing a short means buying
        ex_call("closing the position", _order, sym, is_buy, c, {"reduceOnly": True}, ask if is_buy else bid)
        cancel_all_stops(sym)
    state["stop"] = state["stop_id"] = None
    return p


def end_trade(why):
    lock.set_reason(why)
    log.error(why)


@app.get("/api/klines")
def klines(tf: str = "1m", n: int = 500, inst: str = "", x_token: str = Header(None)):
    check(x_token)
    inst, m = M(inst)
    if tf not in TFS:
        raise HTTPException(400, "tf must be 1m, 5m, 15m, 30m or 1h")
    n, rows, err = max(1, min(int(n), 500)), None, None
    for lim in dict.fromkeys((n, 300, 200, 100)):      # some exchanges cap how many candles one call returns
        try:
            rows = ex.fetch_ohlcv(m["sym"], tf, limit=min(lim, n))
            if rows:
                break
        except Exception as e:
            err = e
    if not rows:
        raise HTTPException(502, f"{LABEL} returned no candles{': ' + str(err)[:200] if err else ''}.")
    out = {}
    for r in rows:
        try:
            out[int(r[0]) // 1000] = {"time": int(r[0]) // 1000, "open": float(r[1]), "high": float(r[2]),
                                      "low": float(r[3]), "close": float(r[4])}
        except Exception:
            continue
    return [out[t] for t in sorted(out)]


@app.get("/api/price")
def price(inst: str = "", x_token: str = Header(None)):
    check(x_token)
    _, m = M(inst)
    bid, ask = bidask(m["sym"])
    return {"mark": float((bid + ask) / 2), "bid": float(bid), "ask": float(ask)}


@app.get("/api/status")
def status(inst: str = "", x_token: str = Header(None)):
    check(x_token)
    p = position()
    inst, m = M(p["inst"] if p else inst)   # while a trade is open the page follows that pair
    locked = lock.traded_today() and p is None
    risk = risk_amount()
    return {"position": p, "raw": None, "venue": LABEL + ("" if LIVE else " TESTNET"),
            "pairs": PAIRS, "inst": inst,
            "risk_usd": float(risk), "risk_pct": float(RISK_PCT), "max_notional_usd": float(max_notional()),
            "pdec": m["pdec"], "qdec": m["qdec"], "unit": m["base"], "step": float(m["step"]),
            "min_dist": float(m["tick"]), "conv": 1, "max_qty": None,
            "locked": locked, "unlock_at": lock.next_unlock_ts() if locked else None,
            "lock_reason": lock.reason() if locked else None}


@app.post("/api/trade")
def trade(req: TradeReq, x_token: str = Header(None)):
    check(x_token)
    if req.side not in ("long", "short"):
        raise HTTPException(400, "side must be long or short")
    if lock.traded_today():
        raise HTTPException(423, "Locked: today's trade is already used. Back tomorrow.")
    if position():
        raise HTTPException(409, "A position is already open. Press Stop first.")
    if req.dist is None:
        raise HTTPException(400, "No distance received.")
    inst, m = M(req.inst)
    sym, is_long = m["sym"], req.side == "long"
    dist = Decimal(str(req.dist))
    if not dist.is_finite() or dist < m["tick"]:
        raise HTTPException(400, "Stop distance is too small.")

    # 1. Size from the risk distance, in whole exchange lots
    bid, ask = bidask(sym)
    est = ask if is_long else bid
    if is_long and est - dist <= 0:
        raise HTTPException(400, "Stop distance is larger than the price.")
    base_qty = risk_amount() / dist
    contracts = (base_qty / m["cs"] / m["astep"]).to_integral_value(rounding=ROUND_DOWN) * m["astep"]
    if contracts <= 0:
        raise HTTPException(400, "Line is too far away: size rounds to zero.")
    if m["min"] and contracts < m["min"]:
        raise HTTPException(400, f"Size {contracts} is below {LABEL}'s minimum of {m['min']}. Raise the risk % or move the line closer.")
    notional = contracts * m["cs"] * est
    if notional > max_notional():
        raise HTTPException(400, f"Line is too close: size ${notional:,.0f} exceeds the ${max_notional():,.0f} cap.")

    # 2. Enter at market
    try:
        _order(sym, is_long, contracts, {}, est)
    except Exception as e:
        raise HTTPException(502, f"Entry failed: {str(e)[:300]}. Nothing was opened.")
    pos = None
    for _ in range(5):
        time.sleep(1.0)
        try:
            pos = position()
        except HTTPException:
            pos = None
        if pos:
            break
    if not pos:
        raise HTTPException(502, f"Could not confirm the fill on {LABEL}. CHECK YOUR POSITIONS THERE NOW. No stop was placed.")
    lock.mark_traded()      # the entry filled: today's one trade is used, even if it is closed right away

    # 3. Stop the same distance from the real fill
    entry = Decimal(str(pos["entry"])) if pos["entry"] > 0 else est
    if abs(entry - est) > est * Decimal("0.01"):
        log.warning("Reported entry %s is far from market %s, using market", entry, est)
        entry = est
    stop = entry - dist if is_long else entry + dist
    stop = Decimal(str(ex.price_to_precision(sym, stop)))
    c = state["contracts"]
    try:
        bid2, ask2 = bidask(sym)
        if (is_long and stop >= bid2) or (not is_long and stop <= ask2):
            flatten()
            why = (f"SAFETY CLOSE: right after entry the stop level ({stop}) was already past the market "
                   f"(bid {bid2}, ask {ask2}), so the position was closed instead of placing a stop that fires instantly.")
            end_trade(why)
            raise HTTPException(409, why)
    except HTTPException as e:
        if e.status_code == 409:
            raise
        log.warning("Pre-stop price check skipped: %s", e.detail)
    try:
        state["stop_id"] = place_stop(sym, not is_long, c, stop)
    except Exception as e:
        log.error("Stop failed, closing: %s", e)
        try:
            flatten()
        except HTTPException as e2:
            why = (f"DANGER: the position is OPEN WITHOUT A STOP on {LABEL} and it could not be closed automatically. "
                   f"CLOSE IT ON {LABEL.upper()} NOW. (stop: {str(e)[:150]} / close: {e2.detail})")
            end_trade(why)
            raise HTTPException(500, why)
        why = f"SAFETY CLOSE: {LABEL} rejected the stop order (stop {stop}), so the position was closed. Reason: {str(e)[:250]}"
        end_trade(why)
        raise HTTPException(502, why)
    state["stop"] = float(stop)
    log.info("Opened %s %s contracts %s entry %s stop %s", req.side, contracts, sym, entry, stop)
    return {"ok": True, "side": req.side, "qty": float(contracts * m["cs"]), "entry": float(entry), "stop": float(stop)}


@app.post("/api/move_stop")
def move_stop(req: MoveReq, x_token: str = Header(None)):
    check(x_token)
    pos = position()
    if not pos:
        raise HTTPException(409, "No open position.")
    inst, m = M(pos["inst"])
    sym, c, is_long = m["sym"], state["contracts"], pos["side"] == "LONG"
    new = Decimal(str(ex.price_to_precision(sym, req.stop_price)))
    bid, ask = bidask(sym)
    if is_long and new >= bid:
        raise HTTPException(409, f"For a long, the stop must be below the bid ({bid}).")
    if not is_long and new <= ask:
        raise HTTPException(409, f"For a short, the stop must be above the ask ({ask}).")
    old_id, warning = state.get("stop_id"), None
    if old_id:      # safest: place the new stop first, then remove the old one
        new_id = ex_call("placing the new stop", place_stop, sym, not is_long, c, new)
        if not cancel_stop(old_id, sym):
            warning = "New stop is set, but the old stop could not be cancelled. Check your open orders."
    else:           # unknown old stop (app was restarted): clear stops, then place
        cancel_all_stops(sym)
        try:
            new_id = place_stop(sym, not is_long, c, new)
        except Exception as e:
            try:
                flatten()
            except HTTPException:
                pass
            why = f"SAFETY CLOSE: the new stop ({new}) could not be placed, so the position was closed. Reason: {str(e)[:250]}"
            end_trade(why)
            raise HTTPException(502, why)
    state["stop"], state["stop_id"] = float(new), new_id
    return {"ok": True, "stop": float(new), "warning": warning}


@app.post("/api/stop")
def stop(x_token: str = Header(None)):
    check(x_token)
    closed = flatten()
    if closed:
        lock.set_reason("You pressed Stop and closed the position manually.")
    return {"ok": True, "closed": closed}


STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True))
