"""One-trade-per-day lock shared by every broker server. Resets daily at LOCK_RESET_HOUR in LOCK_TZ (default 6 AM PHT)."""
import os, json, time, logging
from datetime import datetime, timedelta, time as dtime
from zoneinfo import ZoneInfo

log = logging.getLogger("trader")
TZ = ZoneInfo(os.getenv("LOCK_TZ", "Asia/Manila"))
RESET = int(os.getenv("LOCK_RESET_HOUR", "6"))
FILE = os.getenv("LOCK_FILE", os.path.join(os.path.dirname(os.path.abspath(__file__)), "lock.json"))


def today():
    return (datetime.now(TZ) - timedelta(hours=RESET)).date().isoformat()


def next_unlock_ts():
    now = datetime.now(TZ)
    nxt = datetime.combine(now.date(), dtime(RESET), tzinfo=TZ)
    if now >= nxt:
        nxt += timedelta(days=1)
    return int(nxt.timestamp())


def traded_today():
    try:
        with open(FILE, encoding="utf-8") as f:
            return json.load(f).get("day") == today()
    except FileNotFoundError:
        return False
    except Exception as e:
        log.error("Lock file unreadable, treating as locked: %s", e)
        return True


def mark_traded():
    tmp = FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"day": today(), "at": int(time.time())}, f)
    os.replace(tmp, FILE)


DEFAULT_REASON = "The position was closed on the exchange (your stop was hit, or it was closed or liquidated there)."


def set_reason(reason):
    """Remember WHY today's trade ended (shown on the lock screen). Keeps today's lock."""
    try:
        tmp = FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"day": today(), "at": int(time.time()), "reason": reason}, f)
        os.replace(tmp, FILE)
        log.warning("Lock reason: %s", reason)
    except Exception as e:
        log.error("Could not save lock reason: %s", e)


def reason():
    try:
        with open(FILE, encoding="utf-8") as f:
            return json.load(f).get("reason") or DEFAULT_REASON
    except Exception:
        return "Lock file could not be read, so the app stays locked to be safe."
