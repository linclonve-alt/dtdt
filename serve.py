"""Server mode: run Day Trade Risk Manager on your own server and use it from your phone's browser.

No window, no setup wizard. Everything comes from environment variables (or an .env file), see .env.example.
Keys are never written to disk by this program. Put it behind HTTPS (docker-compose.yml does that with Caddy).

  python serve.py                   start the server (reads .env in this folder if it exists)
  python serve.py --check           test the settings and the exchange login, then exit
  python serve.py --env-file X      use another env file
"""
import os
import sys
import time
import hmac
import json
import argparse
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

BROKERS = ("oanda", "binance", "bybit", "okx", "bitget", "gate", "kucoinfutures", "hyperliquid", "paradex")
FX_DEFAULT = "EUR_USD,GBP_USD,USD_JPY,AUD_USD,USD_CHF,USD_CAD"


def load_env_file(path):
    """Minimal .env reader (KEY=value, # comments, optional quotes). Real environment variables win."""
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:]
        k, v = line.split("=", 1)
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        else:
            v = v.split(" #", 1)[0].split("\t#", 1)[0].strip()     # inline comment after an unquoted value
        os.environ.setdefault(k.strip(), v)


def env(name, default=""):
    return os.environ.get(name, default).strip()


def truthy(v):
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def build_cfg():
    """Turn environment variables into the same settings dict the desktop wizard saves."""
    b = env("BROKER").lower()
    if b not in BROKERS:
        raise SystemExit(f"BROKER must be one of: {', '.join(BROKERS)}  (got '{b}')")
    token = env("ACCESS_TOKEN")
    if len(token) < 24:
        raise SystemExit("ACCESS_TOKEN is missing or too short (min 24 characters). Make one with:\n"
                         "  python -c \"import secrets; print(secrets.token_urlsafe(32))\"")
    try:
        risk, hour = float(env("RISK_PCT", "1")), int(env("LOCK_RESET_HOUR", "6"))
    except ValueError:
        raise SystemExit("RISK_PCT must be a number and LOCK_RESET_HOUR a whole number 0-23.")
    cfg = {"broker": b, "risk_pct": risk, "reset_hour": hour, "tz": env("LOCK_TZ", "Asia/Manila"), "token": token,
           "oanda": {"key": "", "account": "", "live": False, "pairs": []},
           "ex": {"key": "", "secret": "", "password": "", "wallet": "", "live": False, "pairs": []},
           "paradex": {"mode": "l1", "address": "", "key": ""}}
    if b == "oanda":
        cfg["oanda"] = {"key": env("OANDA_API_KEY"), "account": env("OANDA_ACCOUNT_ID"),
                        "live": truthy(env("OANDA_LIVE")),
                        "pairs": [p.strip() for p in env("OANDA_INSTRUMENTS", FX_DEFAULT).split(",") if p.strip()]}
    elif b == "paradex":
        mode = env("PARADEX_MODE", "l1").lower()
        if mode not in ("l1", "subkey"):
            raise SystemExit("PARADEX_MODE must be l1 or subkey.")
        addr, key = (env("L1_ADDRESS"), env("L1_PRIVATE_KEY")) if mode == "l1" else (env("L2_ADDRESS"), env("L2_PRIVATE_KEY"))
        cfg["paradex"] = {"mode": mode, "address": addr, "key": key}
    else:
        quote = "USDC" if b == "hyperliquid" else "USDT"
        pairs = env("EX_PAIRS") or ",".join(f"{c}_{quote}" for c in ("BTC", "ETH", "SOL"))
        cfg["ex"] = {"key": env("EX_KEY"), "secret": env("EX_SECRET"), "password": env("EX_PASSWORD"),
                     "wallet": env("EX_WALLET"), "live": truthy(env("EX_LIVE")),
                     "pairs": [p.strip() for p in pairs.split(",") if p.strip()]}
    return cfg


class Guard:
    """ASGI wrapper in front of the trader app.

    - /healthz answers 'ok' with no token (for Docker health checks), and nothing else.
    - Every /api/ call must carry the right X-Token (constant-time compare). A wrong token never reaches the exchange code.
    - Too many wrong tokens from one address in a row = that address is blocked for a while.
    """

    def __init__(self, app, token, max_fail=8, window=600, ban=900):
        self.app, self.token = app, token.encode()
        self.max_fail, self.window, self.ban = max_fail, window, ban
        self.fails = {}

    async def _reply(self, send, status, body, extra=()):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"application/json"), (b"cache-control", b"no-store"),
                                (b"content-length", str(len(data)).encode()), *extra]})
        await send({"type": "http.response.body", "body": data})

    def _fail(self, ip, now):
        if len(self.fails) > 2000:      # keep memory bounded
            self.fails = {k: v for k, v in self.fails.items() if v["ban"] > now or now - v["t0"] < self.window}
        rec = self.fails.get(ip)
        if not rec or (rec["ban"] <= now and now - rec["t0"] > self.window):
            rec = self.fails[ip] = {"n": 0, "t0": now, "ban": 0.0}
        rec["n"] += 1
        if rec["n"] >= self.max_fail:
            rec["ban"], rec["n"] = now + self.ban, 0

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope.get("path", "")
        if path == "/healthz":
            return await self._reply(send, 200, b'{"ok":true}')
        if path.startswith("/api/"):
            ip = (scope.get("client") or ("?", 0))[0]
            now = time.time()
            rec = self.fails.get(ip)
            if rec and rec["ban"] > now:
                wait = int(rec["ban"] - now) + 1
                return await self._reply(send, 429, {"detail": f"Too many wrong tokens. Try again in {wait} s."},
                                         [(b"retry-after", str(wait).encode())])
            given = dict(scope.get("headers") or []).get(b"x-token", b"")
            if not hmac.compare_digest(given, self.token):
                self._fail(ip, now)
                return await self._reply(send, 401, {"detail": "Wrong token"})
            self.fails.pop(ip, None)
        return await self.app(scope, receive, send)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--env-file", default=None)
    ap.add_argument("--check", action="store_true", help="test settings and exchange login, then exit")
    a = ap.parse_args()
    f = a.env_file or (HERE / ".env" if (HERE / ".env").exists() else None)
    if f:
        load_env_file(f)

    cfg = build_cfg()
    import trader_app   # heavy imports only after the settings are known to be present
    data = Path(env("DATA_DIR") or trader_app.DATA)
    data.mkdir(parents=True, exist_ok=True)
    trader_app.DATA = data      # the one-trade-a-day lock file lives here: keep this folder between restarts
    msg = trader_app.validate(cfg)
    if msg:
        raise SystemExit("Settings problem: " + msg)
    app = trader_app.load_app(cfg)      # logs in to the exchange; raises with the reason if it cannot
    if a.check:
        print(f"OK: {cfg['broker']} login works. Lock file: {data / 'lock.json'}")
        return

    import uvicorn
    trust = truthy(env("TRUST_PROXY"))      # only when a reverse proxy (Caddy) is the sole way in
    uvicorn.run(Guard(app, cfg["token"]), host=env("BIND_HOST", "127.0.0.1"), port=int(env("PORT", "8000")),
                access_log=False,           # access logs would record the ?t=TOKEN of the first page load
                proxy_headers=trust, forwarded_allow_ips="*" if trust else "127.0.0.1")


if __name__ == "__main__":
    main()
