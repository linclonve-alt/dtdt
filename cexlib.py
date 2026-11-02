"""Crypto exchange list and helpers, shared by the setup wizard (trader_app.py) and server_ccxt.py.

Binance, Bybit, OKX, Bitget, Gate, KuCoin Futures and Hyperliquid all go through the CCXT library.
MEXC is deliberately missing: MEXC does not let API users place futures orders.
"""

EXCHANGES = {
    # id (a CCXT class name): label, needs an API passphrase, quote currency of its perpetuals
    "binance":       {"label": "Binance",     "kind": "CEX", "password": False, "quote": "USDT"},
    "bybit":         {"label": "Bybit",       "kind": "CEX", "password": False, "quote": "USDT"},
    "okx":           {"label": "OKX",         "kind": "CEX", "password": True,  "quote": "USDT"},
    "bitget":        {"label": "Bitget",      "kind": "CEX", "password": True,  "quote": "USDT"},
    "gate":          {"label": "Gate",        "kind": "CEX", "password": False, "quote": "USDT"},
    "kucoinfutures": {"label": "KuCoin",      "kind": "CEX", "password": True,  "quote": "USDT"},
    "hyperliquid":   {"label": "Hyperliquid", "kind": "DEX", "password": False, "quote": "USDC"},
}


def symbol_for(pair):
    """'BTC_USDT' -> CCXT perpetual symbol 'BTC/USDT:USDT'."""
    base, quote = pair.split("_")
    return f"{base}/{quote}:{quote}"


def build_exchange(eid, key="", secret="", password="", wallet="", live=False):
    """A CCXT exchange object set up for USDT/USDC perpetual futures. live=False uses the exchange's testnet."""
    import ccxt
    cfg = {"enableRateLimit": True, "timeout": 20000,
           "options": {"defaultType": "future" if eid == "binance" else "swap", "adjustForTimeDifference": True}}
    if eid == "hyperliquid":
        cfg["walletAddress"] = wallet    # your main account address
        cfg["privateKey"] = key          # the API (agent) wallet key: it can trade but not withdraw
    else:
        cfg["apiKey"], cfg["secret"] = key, secret
        if password:
            cfg["password"] = password
    ex = getattr(ccxt, eid)(cfg)
    if not live:
        ex.set_sandbox_mode(True)
    return ex


def check_login(ex):
    """Raises if the keys do not work."""
    ex.fetch_balance()


def explain(e, label):
    """Turn a CCXT error into one plain sentence for the setup page."""
    import ccxt
    if isinstance(e, ccxt.AuthenticationError):
        msg = (f"{label} rejected these keys. Check the key and secret (and passphrase), that it has trading permission, "
               "any IP whitelist, and that Testnet/Live matches where you created the key.")
    elif isinstance(e, ccxt.NotSupported):
        msg = f"{label}: {e}"
    elif isinstance(e, ccxt.NetworkError):
        msg = f"Could not reach {label}: {e}"
    else:
        msg = f"{label} error: {e}"
    return msg[:400]
