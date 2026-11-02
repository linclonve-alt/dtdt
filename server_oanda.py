"""OANDA forex version of the trader (same page, same daily lock, risk = RISK_PCT % of equity).

Needs a USD-denominated OANDA account. Starts on the PRACTICE (demo) server; set OANDA_LIVE=1 for real money.
.env:  ACCESS_TOKEN  OANDA_API_KEY  OANDA_ACCOUNT_ID  [OANDA_INSTRUMENTS=EUR_USD,GBP_USD,... (the pairs in the switcher)] [OANDA_INSTRUMENT=default pair] [OANDA_LIVE=0]
       [RISK_PCT=1] [RISK_USD=10 (fallback)] [MAX_UNITS=200000] [LOCK_TZ] [LOCK_RESET_HOUR]
Run:   uvicorn server_oanda:app --host 0.0.0.0 --port 8000 --env-file .env
"""
import os
import time
import logging
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from typing import Optional

import requests
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import daylock as lock

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("trader")

TOKEN = os.environ["ACCESS_TOKEN"]
KEY, ACC = os.environ["OANDA_API_KEY"], os.environ["OANDA_ACCOUNT_ID"]
LIVE = os.getenv("OANDA_LIVE", "0") == "1"
BASE = "https://api-fxtrade.oanda.com" if LIVE else "https://api-fxpractice.oanda.com"
INSTS = [x.strip() for x in os.getenv(
    "OANDA_INSTRUMENTS", "EUR_USD,GBP_USD,USD_JPY,AUD_USD,USD_CHF,USD_CAD").split(",") if x.strip()]
DEFAULT = os.getenv("OANDA_INSTRUMENT", INSTS[0])
if DEFAULT not in INSTS:
    INSTS.insert(0, DEFAULT)
RISK_PCT = Decimal(os.getenv("RISK_PCT", "1"))
RISK_FALLBACK = Decimal(os.getenv("RISK_USD", "10"))
MAX_UNITS = int(os.getenv("MAX_UNITS", "200000"))
GRAN = {"1m": "M1", "5m": "M5", "15m": "M15", "30m": "M30", "1h": "H1"}

S = requests.Session()
S.headers.update({"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"})
state = {"tids": []}
_eq = {"v": None, "t": 0.0}


def oa(method, path, **kw):
    try:
        r = S.request(method, BASE + path, timeout=10, **kw)
    except Exception as e:
        raise HTTPException(502, f"OANDA unreachable: {e}")
    if r.status_code >= 400:
        raise HTTPException(502, f"OANDA {r.status_code}: {r.text[:300]}")
    return r.json()


META = {}
for _m in oa("GET", f"/v3/accounts/{ACC}/instruments", params={"instruments": ",".join(INSTS)})["instruments"]:
    _p = int(_m["displayPrecision"])
    META[_m["name"]] = {"pdec": _p, "pip": Decimal(10) ** int(_m["pipLocation"]),
                        "tick": Decimal(1).scaleb(-_p), "quote": _m["name"].split("_")[1]}
INSTS = [i for i in INSTS if i in META]
if DEFAULT not in META:
    DEFAULT = INSTS[0]
log.info("OANDA %s account %s, pairs %s", "LIVE" if LIVE else "PRACTICE", ACC, INSTS)


def M(inst):
    inst = inst or DEFAULT
    if inst not in META:
        raise HTTPException(400, f"Unknown pair {inst}")
    return inst, META[inst]


app = FastAPI()


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


def risk_amount():
    """RISK_PCT % of account NAV (cached 15 s); falls back to RISK_USD."""
    if time.time() - _eq["t"] > 15:
        try:
            v = Decimal(oa("GET", f"/v3/accounts/{ACC}/summary")["account"]["NAV"])
            _eq["v"] = v if v > 0 else None
        except Exception as e:
            log.warning("Could not read equity, using RISK_USD: %s", e)
            _eq["v"] = None
        _eq["t"] = time.time()
    return max((_eq["v"] * RISK_PCT / 100).quantize(Decimal("0.01")), Decimal("0.01")) if _eq["v"] else RISK_FALLBACK


def pricing(inst):
    """bid, ask, and the factor that converts 1 unit of quote currency into account currency."""
    j = oa("GET", f"/v3/accounts/{ACC}/pricing", params={"instruments": inst, "includeHomeConversions": "true"})
    p = j["prices"][0]
    conv = Decimal(1)
    for h in j.get("homeConversions", []):
        if h.get("currency") == META[inst]["quote"]:
            conv = Decimal(h["accountLoss"])
    return Decimal(p["bids"][0]["price"]), Decimal(p["asks"][0]["price"]), conv


def position():
    """The open position on any of the allowed pairs (only one trade a day, so at most one pair)."""
    trades = [t for t in oa("GET", f"/v3/accounts/{ACC}/openTrades").get("trades", []) if t["instrument"] in META]
    if trades:
        trades = [t for t in trades if t["instrument"] == trades[0]["instrument"]]
    units = sum(int(t["currentUnits"]) for t in trades)
    if not trades or units == 0:
        state["tids"] = []
        return None
    state["tids"] = [t["id"] for t in trades]
    tot = sum(abs(int(t["currentUnits"])) for t in trades)
    entry = sum(float(t["price"]) * abs(int(t["currentUnits"])) for t in trades) / tot
    stop = next((float(t["stopLossOrder"]["price"]) for t in trades if t.get("stopLossOrder")), None)
    return {"inst": trades[0]["instrument"], "side": "LONG" if units > 0 else "SHORT",
            "size": abs(units), "entry": entry, "stop": stop}


def flatten():
    p = position()
    if p:   # closing the trades also cancels their attached stop-loss
        oa("PUT", f"/v3/accounts/{ACC}/positions/{p['inst']}/close",
           json={"longUnits": "ALL"} if p["side"] == "LONG" else {"shortUnits": "ALL"})
    return p


@app.get("/api/klines")
def klines(tf: str = "1m", n: int = 500, inst: str = "", x_token: str = Header(None)):
    check(x_token)
    inst, _ = M(inst)
    if tf not in GRAN:
        raise HTTPException(400, "tf must be 1m, 5m, 15m, 30m or 1h")
    j = oa("GET", f"/v3/instruments/{inst}/candles",
           params={"granularity": GRAN[tf], "count": max(1, min(int(n), 500)), "price": "M"})
    out = []
    for c in j.get("candles", []):
        m = c.get("mid")
        if not m:
            continue
        ts = int(datetime.fromisoformat(c["time"][:19]).replace(tzinfo=timezone.utc).timestamp())
        out.append({"time": ts, "open": float(m["o"]), "high": float(m["h"]), "low": float(m["l"]), "close": float(m["c"])})
    if not out:
        raise HTTPException(502, "OANDA returned no candles (market closed?).")
    return out


@app.get("/api/price")
def price(inst: str = "", x_token: str = Header(None)):
    check(x_token)
    inst, _ = M(inst)
    bid, ask, _ = pricing(inst)
    return {"mark": float((bid + ask) / 2), "bid": float(bid), "ask": float(ask)}


@app.get("/api/status")
def status(inst: str = "", x_token: str = Header(None)):
    check(x_token)
    p = position()
    inst, m = M(p["inst"] if p else inst)   # while a trade is open the page follows that pair
    locked = lock.traded_today() and p is None
    return {"position": p, "raw": None, "venue": "OANDA" + ("" if LIVE else " DEMO"),
            "pairs": INSTS, "inst": inst,
            "risk_usd": float(risk_amount()), "risk_pct": float(RISK_PCT), "max_notional_usd": 0,
            "pdec": m["pdec"], "qdec": 0, "unit": "units", "step": 1, "min_dist": float(m["pip"]),
            "conv": float(pricing(inst)[2]), "max_qty": MAX_UNITS,
            "locked": locked, "unlock_at": lock.next_unlock_ts() if locked else None}


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
    dist = Decimal(str(req.dist)).quantize(m["tick"])
    if dist < m["pip"]:
        raise HTTPException(400, "Stop distance is too small.")
    _, _, conv = pricing(inst)
    units = int((risk_amount() / (dist * conv)).to_integral_value(rounding=ROUND_DOWN))
    if units < 1:
        raise HTTPException(400, "Line is too far away: size rounds to zero.")
    if units > MAX_UNITS:
        raise HTTPException(400, f"Line is too close: {units} units exceeds the {MAX_UNITS} cap.")
    is_long = req.side == "long"
    # The stop is attached to the order and sits `dist` from the actual fill, so it is never unprotected.
    j = oa("POST", f"/v3/accounts/{ACC}/orders", json={"order": {
        "type": "MARKET", "instrument": inst, "units": str(units if is_long else -units),
        "timeInForce": "FOK", "positionFill": "DEFAULT",
        "stopLossOnFill": {"distance": str(dist), "timeInForce": "GTC"}}})
    fill = j.get("orderFillTransaction")
    if not fill:
        why = (j.get("orderCancelTransaction") or {}).get("reason") or j.get("errorMessage") or "unknown"
        raise HTTPException(502, f"Order not filled: {why}. Nothing was opened.")
    lock.mark_traded()
    entry = Decimal(fill["price"])
    stop = entry - dist if is_long else entry + dist
    log.info("Opened %s %s units %s entry %s stop %s", req.side, units, inst, entry, stop)
    return {"ok": True, "side": req.side, "qty": units, "entry": float(entry), "stop": float(stop)}


@app.post("/api/move_stop")
def move_stop(req: MoveReq, x_token: str = Header(None)):
    check(x_token)
    pos = position()
    if not pos:
        raise HTTPException(409, "No open position.")
    inst, m = M(pos["inst"])
    new = Decimal(str(req.stop_price)).quantize(m["tick"])
    bid, ask, _ = pricing(inst)
    if pos["side"] == "LONG" and new >= bid:
        raise HTTPException(409, f"For a long, the stop must be below the bid ({bid}).")
    if pos["side"] == "SHORT" and new <= ask:
        raise HTTPException(409, f"For a short, the stop must be above the ask ({ask}).")
    for tid in state["tids"]:
        oa("PUT", f"/v3/accounts/{ACC}/trades/{tid}/orders", json={"stopLoss": {"price": str(new), "timeInForce": "GTC"}})
    return {"ok": True, "stop": float(new), "warning": None}


@app.post("/api/stop")
def stop(x_token: str = Header(None)):
    check(x_token)
    return {"ok": True, "closed": flatten()}


STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True))
