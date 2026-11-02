# Run it on your own server, use it from your phone

The same trader as the desktop app, but it runs on a small server and you open it on your phone like an app
(Android and iPhone). No terminal needed on the phone, only once on the server.

## What you need
- A small Linux server (any VPS, 1 GB RAM is enough) with Docker installed.
- A domain or subdomain (for example `trade.yourname.com`) with an **A record pointing at the server's IP**.
  HTTPS is required: the phone only offers "install app" over HTTPS, and your access token must not travel in the clear.

## Setup (once)
1. Copy this folder to the server.
2. `cp .env.example .env` then edit it:
   - `DOMAIN`, `BROKER`, your keys, `LOCK_TZ`, `LOCK_RESET_HOUR`.
   - `ACCESS_TOKEN`: make one with `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`.
   - Keep `EX_LIVE=0` (testnet/demo) until you have tried a few trades.
3. `chmod 600 .env`
4. Test the login first: `docker compose run --rm app python serve.py --check`
   It prints `OK` if the exchange accepted your keys, or the reason if not.
5. Start it: `docker compose up -d` (Caddy fetches the HTTPS certificate by itself, give it a minute).

## On your phone
Open `https://YOUR-DOMAIN/?t=YOUR_TOKEN` once. The page remembers the token and removes it from the address bar.
- **Android (Chrome):** menu, then **Install app** / **Add to Home screen**.
- **iPhone (Safari):** Share, then **Add to Home Screen**. The home-screen app keeps its own storage on iPhone,
  so the first time you open it, it asks for the token: paste it once.

## Keeping your money safe
- **Whitelist the server's IP** in the exchange's API settings. A server has a fixed IP, so a stolen key is useless elsewhere.
- API keys: **Trade permission only, withdrawals OFF.**
- Hyperliquid: use an API wallet, not your main wallet key.
- Paradex L1 mode needs your wallet's private key on the server, and that key can move **all** funds in the wallet.
  Use a wallet that holds only your trading money, or use `PARADEX_MODE=subkey` (safer, may be charged Pro fees).
- Wrong tokens are blocked after 8 tries in a row (15 minutes per address). Use the long random token, not a password you picked.
- Anyone with the token can place a trade, and anyone with server access can read `.env`. Treat the server like your wallet.

## Good to know
- **One broker per server instance.** To switch exchange, edit `BROKER` and the keys in `.env`, then `docker compose up -d`.
  The one-trade-a-day lock is kept in the `appdata` Docker volume and is shared by all brokers, so switching does not give you a second trade.
  Do not delete that volume.
- Logs: `docker compose logs -f app`. Update: copy the new files, then `docker compose up -d --build`.
- Without Docker: `pip install -r requirements.txt`, then `python serve.py` (it listens on 127.0.0.1 only by default).
  For private access with no domain, Tailscale with `tailscale serve` gives you HTTPS inside your own network.
- The desktop installer still works as before. Server mode and desktop mode are independent.
