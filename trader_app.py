"""Day Trade Risk Manager desktop app: opens its own window, shows a setup wizard on first run, then the trader.

Run:  python trader_app.py        (customers use the installer built by GitHub Actions)
Reset settings: run with --setup
Settings and the daily-lock file live in  ~/.daytrade_risk_manager/
"""
import os, sys, json, stat, time, socket, secrets, threading, webbrowser, importlib
from pathlib import Path

import requests
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

DATA = Path.home() / ".daytrade_risk_manager"
CFG = DATA / "config.json"

SETUP_HTML = r"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Day Trade Risk Manager setup</title><style>
*{box-sizing:border-box;border-radius:0}body{background:#000;color:#fff;font:14px/1.5 "IBM Plex Mono","Courier New",monospace;margin:0;padding:20px}
.w{max-width:520px;margin:0 auto;border:3px double #fff;padding:22px}h1{margin:0 0 4px;letter-spacing:3px}
label{display:block;margin:14px 0 4px;color:#bdbdbd;font-size:12px;text-transform:uppercase;letter-spacing:1px}
input,select{width:100%;background:#000;color:#fff;border:2px solid #fff;font:inherit;padding:9px}
.row{display:flex;gap:14px;flex-wrap:wrap}.row label{display:flex;gap:6px;align-items:center;margin:6px 0;color:#fff;text-transform:none;font-size:14px}
.row input{width:auto}button{margin-top:22px;width:100%;background:#fff;color:#000;border:2px solid #fff;font:700 16px inherit;padding:14px;letter-spacing:2px;box-shadow:3px 3px 0 #8a8a8a}
#err{background:#fff;color:#000;padding:8px 10px;margin:12px 0;display:none;font-weight:600}.h{color:#8a8a8a;font-size:12px}
.note{border:1px solid #8a8a8a;padding:8px 10px;margin:12px 0 0;font-size:12px;color:#bdbdbd}.warn{border-color:#fff;color:#fff}
</style></head><body><form class="w" id="setupform"><h1>RISK MANAGER SETUP</h1><div class="h">Your keys are saved only on this computer.</div>
<div id="err">__ERR__</div>
<label>Broker / exchange</label><select id="broker">
<optgroup label="FOREX BROKER"><option value="oanda">OANDA (forex)</option></optgroup>
<optgroup label="CEX - CENTRALIZED EXCHANGES (API key)">
<option value="binance">Binance Futures</option><option value="bybit">Bybit</option><option value="okx">OKX</option><option value="bitget">Bitget</option><option value="gate">Gate</option><option value="kucoinfutures">KuCoin Futures</option>
<option disabled>MEXC (not available: no futures API)</option></optgroup>
<optgroup label="DEX - DECENTRALIZED (wallet)"><option value="hyperliquid">Hyperliquid</option><option value="paradex">Paradex</option></optgroup>
</select>
<div class="note" id="hint"></div>

<div id="oanda"><label>OANDA API token</label><input id="okey" type="password" autocomplete="off">
<label>OANDA account ID (like 101-001-1234567-001)</label><input id="oacc">
<label>Account type</label><select id="live"><option value="0">Demo / practice (recommended first)</option><option value="1">LIVE (real money)</option></select>
<label>Pairs in the switcher</label><div class="row" id="fxpairs"></div></div>

<div id="cex" style="display:none"><label>API key</label><input id="ckey" autocomplete="off">
<label>API secret</label><input id="csec" type="password" autocomplete="off">
<div id="passrow"><label>API passphrase</label><input id="cpass" type="password" autocomplete="off"></div>
<label>Account type</label><select id="clive"><option value="0">Testnet / demo (needs separate testnet keys)</option><option value="1">LIVE (real money)</option></select>
<label>Pairs in the switcher</label><div class="row" id="cexpairs"></div></div>

<div id="hl" style="display:none"><label>Main wallet address (0x...)</label><input id="hladdr" autocomplete="off">
<label>API wallet private key (0x...)</label><input id="hlkey" type="password" autocomplete="off">
<label>Account type</label><select id="hllive"><option value="0">Testnet (recommended first)</option><option value="1">LIVE (real money)</option></select>
<label>Pairs in the switcher</label><div class="row" id="hlpairs"></div></div>

<div id="paradex" style="display:none"><label>Login type</label><select id="pmode">
<option value="l1">L1 wallet login - zero-fee RETAIL orders</option><option value="subkey">Subkey (L2 only) - safer, may be charged Pro fees</option></select>
<div id="pl1"><label>Ethereum (L1) wallet address</label><input id="pl1addr" autocomplete="off">
<label>Ethereum (L1) wallet private key</label><input id="pl1key" type="password" autocomplete="off">
<div class="note warn">This key can move ALL the funds in that wallet, not only trade. Use a wallet that holds only your trading money, and never share this PC's config folder.</div></div>
<div id="psub" style="display:none"><label>Paradex L2 address</label><input id="padd" autocomplete="off">
<label>Paradex subkey private key</label><input id="pkey" type="password" autocomplete="off"></div></div>

<label>Risk per trade (% of account)</label><input id="risk" type="number" step="0.1" min="0.1" max="5" value="1">
<label>Daily lock opens at (hour, 0-23)</label><input id="hour" type="number" min="0" max="23" value="6">
<label>Your timezone</label><input id="tz">
<button type="submit" id="go">SAVE &amp; START</button></form>
<script>
const $=i=>document.getElementById(i);
const FX=["EUR_USD","GBP_USD","USD_JPY","AUD_USD","USD_CHF","USD_CAD"], COINS=["BTC","ETH","SOL","XRP","DOGE"];
const CEX=["binance","bybit","okx","bitget","gate","kucoinfutures"], PASS=["okx","bitget","kucoinfutures"];
const HINT={
 oanda:"Needs a USD-denominated OANDA account.",
 binance:"Create an API key with Futures + Trading permission and turn OFF withdrawals. Set Binance Futures to One-way position mode and set your leverage there first.",
 bybit:"Create an API key with Trade permission only (no withdrawals). Use One-way position mode and set your leverage on Bybit first.",
 okx:"Create an API key with Trade permission only (no withdrawals) and note the passphrase you chose. Set the account to Net (one-way) position mode and set your leverage on OKX first. For Demo, create the key while in OKX Demo Trading mode.",
 gate:"Create a Futures API key with Trade permission only (no withdrawals). Use One-way mode and set your leverage on Gate first. Testnet needs separate Gate testnet keys.",
 bitget:"Create an API key with Trade permission only (no withdrawals). Use One-way position mode and set your leverage on Bitget first.",
 kucoinfutures:"Create a KuCoin FUTURES API key with Trade permission only (no withdrawals). Use One-way mode. This app sends 10x leverage with each order.",
 hyperliquid:"Use an API wallet: on app.hyperliquid.xyz open More > API, create one and paste its private key here. It can trade but cannot withdraw. The main wallet address is the account that holds your funds.",
 paradex:"Zero-fee retail orders need the interactive login token. The app shows RETAIL 0% FEE in the header when Paradex confirmed it."};
function boxes(id,list,on){$(id).innerHTML=list.map(p=>`<label><input type="checkbox" value="${p}" ${on.includes(p)?"checked":""}>${p.replace("_","/")}</label>`).join("")}
boxes("fxpairs",FX,FX);boxes("cexpairs",COINS.map(c=>c+"_USDT"),["BTC_USDT","ETH_USDT","SOL_USDT"]);boxes("hlpairs",COINS.map(c=>c+"_USDC"),["BTC_USDC","ETH_USDC","SOL_USDC"]);
$("tz").value=Intl.DateTimeFormat().resolvedOptions().timeZone||"UTC";
if($("err").textContent.trim())$("err").style.display="block";
function view(){const b=$("broker").value;
 $("oanda").style.display=b=="oanda"?"block":"none";$("cex").style.display=CEX.includes(b)?"block":"none";
 $("hl").style.display=b=="hyperliquid"?"block":"none";$("paradex").style.display=b=="paradex"?"block":"none";
 $("passrow").style.display=PASS.includes(b)?"block":"none";$("hint").textContent=HINT[b]||"";$("hint").style.display=HINT[b]?"block":"none"}
function pview(){const l1=$("pmode").value=="l1";$("pl1").style.display=l1?"block":"none";$("psub").style.display=l1?"none":"block"}
$("broker").onchange=view;$("pmode").onchange=pview;view();pview();
const sel=id=>[...document.querySelectorAll("#"+id+" input:checked")].map(x=>x.value);
$("setupform").onsubmit=async e=>{e.preventDefault();$("err").style.display="none";$("go").textContent="CHECKING...";
 const b=$("broker").value,hl=b=="hyperliquid",l1=$("pmode").value=="l1";
 const ex=hl?{key:$("hlkey").value.trim(),secret:"",password:"",wallet:$("hladdr").value.trim(),live:$("hllive").value=="1",pairs:sel("hlpairs")}
  :{key:$("ckey").value.trim(),secret:$("csec").value.trim(),password:$("cpass").value.trim(),wallet:"",live:$("clive").value=="1",pairs:sel("cexpairs")};
 const body={broker:b,risk_pct:+$("risk").value,reset_hour:+$("hour").value,tz:$("tz").value.trim(),
  oanda:{key:$("okey").value.trim(),account:$("oacc").value.trim(),live:$("live").value=="1",pairs:sel("fxpairs")},ex:ex,
  paradex:{mode:$("pmode").value,address:(l1?$("pl1addr"):$("padd")).value.trim(),key:(l1?$("pl1key"):$("pkey")).value.trim()}};
 const r=await fetch("/save",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)}).then(r=>r.json()).catch(()=>({error:"Could not reach the setup program."}));
 if(r.error){$("err").textContent=r.error;$("err").style.display="block";$("go").textContent="SAVE & START";return}
 $("go").textContent="STARTING...";
 for(let i=0;i<90;i++){await new Promise(s=>setTimeout(s,1000));
  try{const t=await fetch("/").then(x=>x.text());if(!t.includes('id="setupform"')){location.href="/?t="+r.token;return}}catch(_){}}
 $("err").textContent="Start failed. Close this window and run the program again.";$("err").style.display="block";$("go").textContent="SAVE & START"};
</script></body></html>"""


def free_port(start=8000):
    for p in range(start, start + 50):
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", p)) != 0:
                return p
    return start


def _hex(v, n):
    """True if v looks like 0x + n hex characters."""
    v = v[2:] if v[:2].lower() == "0x" else v
    return len(v) == n and all(c in "0123456789abcdefABCDEF" for c in v)


def validate(d):
    """Return an error string, or '' if the settings look usable."""
    if not (0 < d.get("risk_pct", 0) <= 5):
        return "Risk must be between 0.1 and 5 percent."
    if not (0 <= d.get("reset_hour", -1) <= 23):
        return "Lock hour must be 0 to 23."
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo(d.get("tz", ""))
    except Exception:
        return "Timezone not recognised. Use a name like Asia/Manila or Europe/London."
    b = d.get("broker")
    if b == "oanda":
        o = d["oanda"]
        if not o["key"] or not o["account"] or not o["pairs"]:
            return "Fill in the OANDA token, account ID and pick at least one pair."
        base = "https://api-fxtrade.oanda.com" if o["live"] else "https://api-fxpractice.oanda.com"
        try:
            r = requests.get(f"{base}/v3/accounts/{o['account']}/summary", headers={"Authorization": f"Bearer {o['key']}"}, timeout=10)
        except Exception as e:
            return f"Could not reach OANDA: {e}"
        if r.status_code != 200:
            return "OANDA rejected these details. Check the token, the account ID, and that Demo/Live matches the account."
        if r.json()["account"].get("currency") != "USD":
            return "This version needs a USD-denominated OANDA account."
        return ""
    if b == "paradex":
        p = d["paradex"]
        l1 = p.get("mode") == "l1"
        if not p["address"] or not p["key"]:
            return "Fill in the Paradex " + ("Ethereum wallet address and private key." if l1 else "L2 address and subkey private key.")
        if l1 and not (_hex(p["address"], 40) and _hex(p["key"], 64)):
            return "The Ethereum address should be 0x + 40 characters and the private key 0x + 64 characters."
        return ""
    import cexlib
    if b in cexlib.EXCHANGES:
        info, e = cexlib.EXCHANGES[b], d["ex"]
        if not e["pairs"]:
            return "Pick at least one pair."
        if b == "hyperliquid":
            if not (_hex(e["wallet"], 40) and _hex(e["key"], 64)):
                return "Hyperliquid needs your main wallet address (0x + 40 characters) and the API wallet private key (0x + 64 characters)."
        else:
            if not e["key"] or not e["secret"]:
                return "Fill in the API key and the API secret."
            if info["password"] and not e["password"]:
                return f"{info['label']} also needs the API passphrase."
        try:
            cexlib.check_login(cexlib.build_exchange(b, e["key"], e["secret"], e["password"], e["wallet"], e["live"]))
        except Exception as ex:
            return cexlib.explain(ex, info["label"])
        return ""
    return "Choose an exchange."


def run_setup(port, err=""):
    done, out = threading.Event(), {}
    app = FastAPI()

    @app.get("/")
    def page():
        return HTMLResponse(SETUP_HTML.replace("__ERR__", err))

    @app.post("/save")
    async def save(request: Request):
        d = await request.json()
        msg = validate(d)
        if msg:
            return JSONResponse({"error": msg})
        d["token"] = secrets.token_urlsafe(16)
        d["port"] = port
        DATA.mkdir(exist_ok=True)
        CFG.write_text(json.dumps(d, indent=2))
        try:
            os.chmod(CFG, stat.S_IRUSR | stat.S_IWUSR)   # keys readable by this user only
        except Exception:
            pass
        out["cfg"] = d
        threading.Timer(0.5, done.set).start()
        return {"ok": True, "token": d["token"]}

    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_config=None))
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    done.wait()
    srv.should_exit = True
    t.join(5)
    return out["cfg"]


def load_app(c):
    import cexlib
    e = os.environ
    e.update(ACCESS_TOKEN=c["token"], RISK_PCT=str(c["risk_pct"]), LOCK_TZ=c["tz"],
             LOCK_RESET_HOUR=str(c["reset_hour"]), LOCK_FILE=str(DATA / "lock.json"))
    b = c["broker"]
    if b == "oanda":
        o = c["oanda"]
        e.update(OANDA_API_KEY=o["key"], OANDA_ACCOUNT_ID=o["account"], OANDA_LIVE="1" if o["live"] else "0",
                 OANDA_INSTRUMENTS=",".join(o["pairs"]))
        name = "server_oanda"
    elif b in cexlib.EXCHANGES:
        x = c["ex"]
        e.update(EXCHANGE_ID=b, EX_KEY=x["key"], EX_SECRET=x["secret"], EX_PASSWORD=x["password"], EX_WALLET=x["wallet"],
                 EX_LIVE="1" if x["live"] else "0", EX_PAIRS=",".join(x["pairs"]))
        name = "server_ccxt"
    else:
        p = c["paradex"]
        if p.get("mode") == "l1":
            e.update(PARADEX_MODE="l1", L1_ADDRESS=p["address"], L1_PRIVATE_KEY=p["key"])
        else:   # configs saved by older versions have no mode: they were subkey logins
            e.update(PARADEX_MODE="subkey", L2_ADDRESS=p["address"], L2_PRIVATE_KEY=p["key"])
        name = "server"
    sys.modules.pop(name, None)
    sys.modules.pop("daylock", None)   # re-read timezone / reset hour if setup was retried
    return importlib.import_module(name).app


def backend(port):
    """Runs in the background: setup wizard if needed, then the trader server."""
    cfg = None
    if CFG.exists() and "--setup" not in sys.argv:
        try:
            cfg = json.loads(CFG.read_text())
        except Exception:
            cfg = None
    err = ""
    while True:
        if cfg is None:
            cfg = run_setup(port, err)
        try:
            app = load_app(cfg)
            break
        except BaseException as ex:   # wrong keys etc.: show the setup page again with the reason
            err, cfg = f"Could not start with those settings: {ex}", None
    uvicorn.run(app, host="127.0.0.1", port=port, log_config=None)


def show_window(url):
    """Own app window (Edge WebView2). Falls back to the normal browser if that is unavailable."""
    t0 = time.time()
    try:
        import webview
        webview.create_window("Day Trade Risk Manager", url, width=440, height=880, min_size=(360, 600), background_color="#000000")
        webview.start()
        if time.time() - t0 > 3:   # the window was really shown and then closed by the user
            os._exit(0)
    except Exception:
        pass
    webbrowser.open(url)
    threading.Event().wait()


def main():
    DATA.mkdir(exist_ok=True)
    if sys.stdout is None or sys.stderr is None:   # windowed build has no console: log to a file instead
        sys.stdout = sys.stderr = open(DATA / "app.log", "a", buffering=1, encoding="utf-8")
    port = free_port()
    token = None
    if CFG.exists() and "--setup" not in sys.argv:
        try:
            token = json.loads(CFG.read_text()).get("token")
        except Exception:
            token = None
    threading.Thread(target=backend, args=(port,), daemon=True).start()
    for _ in range(180):   # wait (up to 90 s) until the setup page or the trader is answering
        with socket.socket() as sk:
            if sk.connect_ex(("127.0.0.1", port)) == 0:
                break
        time.sleep(0.5)
    show_window(f"http://127.0.0.1:{port}/" + (f"?t={token}" if token else ""))
    os._exit(0)


if __name__ == "__main__":
    main()
