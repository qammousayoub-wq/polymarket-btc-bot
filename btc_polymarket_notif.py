import os
import re
import time
import json
from typing import Dict, Any, List, Optional, Tuple

import requests

GAMMA = "https://gamma-api.polymarket.com"
STATE_FILE = os.environ.get("STATE_FILE", "scanner_state.json")

# ---- CONFIG ----
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "10"))  # target: scan every 10s
MAX_PAGES = int(os.environ.get("MAX_PAGES", "3"))         # 3 * 100 events/page

KEYWORDS_RAW = [
    k.strip().lower()
    for k in os.environ.get(
        "KEYWORDS", "btc,bitcoin,eth,ethereum,sol,solana"
    ).split(",")
    if k.strip()
]

# Short tickers (btc/eth/sol) must match as full "words"
KEYWORD_TOKENS = [k for k in KEYWORDS_RAW if len(k) <= 3]
# Longer names (bitcoin/ethereum/solana) can be substring-matched
KEYWORD_SUBSTRINGS = [k for k in KEYWORDS_RAW if len(k) > 3]

EDGE_MIN = float(os.environ.get("EDGE_MIN", "0.03"))          # 3% edge
SPIKE_DELTA = float(os.environ.get("SPIKE_DELTA", "0.12"))    # 12 points (0.12)
MAX_ALERTS_PER_CYCLE = int(os.environ.get("MAX_ALERTS_PER_CYCLE", "8"))
MAX_SEEN_EVENTS = int(os.environ.get("MAX_SEEN_EVENTS", "5000"))

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

session = requests.Session()
session.headers.update({"User-Agent": "polymarket-scanner/1.0"})


def tg_send(text: str) -> None:
    if not BOT_TOKEN or not CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    try:
        session.post(
            url,
            json={"chat_id": CHAT_ID, "text": text[:3900]},
            timeout=20,
        )
    except Exception:
        # Best-effort notifications; scanner keeps running even if Telegram fails.
        pass


def load_state() -> Dict[str, Any]:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"seen_event_ids": [], "prices": {}}


def save_state(state: Dict[str, Any]) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f)


def fetch_events_page(page: int) -> List[Dict[str, Any]]:
    r = session.get(
        f"{GAMMA}/events",
        params={
            "active": "true",
            "closed": "false",
            "limit": 100,
            "offset": page * 100,
        },
        timeout=25,
    )
    r.raise_for_status()
    data = r.json()
    return data or []


def event_url(slug: str) -> str:
    return f"https://polymarket.com/event/{slug}" if slug else ""


def market_url(slug: str) -> str:
    return f"https://polymarket.com/market/{slug}" if slug else ""


def hits(text: str) -> List[str]:
    """
    Return crypto keywords present in the text.
    - Short tickers (btc/eth/sol) must appear as standalone tokens
      to avoid matching 'eth' in 'health', etc.
    - Longer names (bitcoin/ethereum/solana) can match as substrings.
    """
    t = (text or "").lower()
    # tokenise to avoid 'eth' inside 'health'
    tokens = set(re.findall(r"[a-z0-9$]+", t))

    matched: List[str] = []
    for k in KEYWORD_TOKENS:
        if k in tokens:
            matched.append(k)
    for k in KEYWORD_SUBSTRINGS:
        if k in t:
            matched.append(k)

    # de-duplicate while preserving order
    seen: set = set()
    result: List[str] = []
    for k in matched:
        if k not in seen:
            seen.add(k)
            result.append(k)
    return result


def parse_yes_no_prices(market: Dict[str, Any]) -> Optional[Tuple[float, float]]:
    # Gamma often includes outcomePrices like ["0.62","0.38"] for binary markets
    op = market.get("outcomePrices") or market.get("outcome_prices")
    if isinstance(op, list) and len(op) >= 2:
        try:
            yes = float(op[0])
            no = float(op[1])
            if 0 <= yes <= 1 and 0 <= no <= 1:
                return yes, no
        except Exception:
            return None
    return None


def process_event(
    e: Dict[str, Any],
    seen_events: set,
    prices_state: Dict[str, Any],
    alerts: int,
) -> int:
    eid = str(e.get("id") or "")
    eslug = e.get("slug") or ""
    title = e.get("title") or "(no title)"
    markets = e.get("markets") or []

    # NEW MARKET/EVENT ALERT (only once per event)
    if eid and eid not in seen_events:
        text = " ".join(
            [
                str(e.get("title") or ""),
                str(e.get("description") or ""),
                " ".join(
                    [
                        (m.get("question") or m.get("title") or "")
                        for m in markets
                        if isinstance(m, dict)
                    ][:10]
                ),
            ]
        )
        h = hits(text)
        if h and alerts < MAX_ALERTS_PER_CYCLE:
            tg_send(
                f"🟠 NEW CRYPTO EVENT ({', '.join(sorted(set(h)))})\n"
                f"{title}\n{event_url(eslug)}\nEventID: {eid}"
            )
            alerts += 1
        seen_events.add(eid)

    # MARKET ANOMALIES
    now_ts = int(time.time())
    for m in markets:
        if not isinstance(m, dict):
            continue

        q = m.get("question") or m.get("title") or "(no question)"
        mslug = m.get("slug") or ""
        mid = str(m.get("id") or m.get("market_id") or "")

        text = f"{title} {q} {m.get('description') or ''} {mslug}"
        h = hits(text)
        if not h:
            continue

        yn = parse_yes_no_prices(m)
        if not yn:
            continue
        yes, no = yn

        prev = prices_state.get(mid) if isinstance(prices_state, dict) else None
        prev_yes = float(prev.get("yes", yes)) if isinstance(prev, dict) else yes
        prev_no = float(prev.get("no", no)) if isinstance(prev, dict) else no

        # Spike check: big move since last seen price
        if isinstance(prev, dict):
            if (
                abs(yes - prev_yes) >= SPIKE_DELTA
                or abs(no - prev_no) >= SPIKE_DELTA
            ) and alerts < MAX_ALERTS_PER_CYCLE:
                tg_send(
                    f"⚡ PRICE SPIKE ({', '.join(sorted(set(h)))})\n"
                    f"{q}\nYES {prev_yes:.2f}→{yes:.2f} | NO {prev_no:.2f}→{no:.2f}\n"
                    f"{market_url(mslug)}\nMarketID: {mid}"
                )
                alerts += 1

        # Arb heuristic: trigger only on transition into arb state
        s = yes + no
        prev_sum = prev_yes + prev_no
        arb_was_active = bool(prev.get("arb_active", False)) if isinstance(prev, dict) else False
        arb_is_active = s <= (1.0 - EDGE_MIN)

        if (
            arb_is_active
            and not arb_was_active
            and alerts < MAX_ALERTS_PER_CYCLE
        ):
            edge = 1.0 - s
            tg_send(
                f"💰 POSSIBLE ARB ({', '.join(sorted(set(h)))})\n"
                f"{q}\nYES {yes:.2f} | NO {no:.2f} | Sum {s:.2f}\n"
                f"Edge ~ {edge*100:.1f}% (verify liquidity/spread)\n"
                f"{market_url(mslug)}\nMarketID: {mid}"
            )
            alerts += 1

        prices_state[mid] = {
            "yes": yes,
            "no": no,
            "ts": now_ts,
            "arb_active": arb_is_active,
        }

    return alerts


def run_loop() -> None:
    if not BOT_TOKEN or not CHAT_ID:
        print("❌ Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID.")
        return

    state = load_state()
    seen_events = set(state.get("seen_event_ids", []))
    prices_state = state.get("prices", {}) or {}

    tg_send("✅ Pro Scanner ON (BTC/ETH/SOL + anomalies).")

    while True:
        alerts = 0
        try:
            for page in range(MAX_PAGES):
                events = fetch_events_page(page)
                if not events:
                    break
                for e in events:
                    alerts = process_event(e, seen_events, prices_state, alerts)
                    if alerts >= MAX_ALERTS_PER_CYCLE:
                        break
                if alerts >= MAX_ALERTS_PER_CYCLE:
                    break

            state["seen_event_ids"] = list(seen_events)[-MAX_SEEN_EVENTS:]
            state["prices"] = prices_state
            save_state(state)
        except Exception as ex:
            tg_send(f"⚠️ Scanner error: {ex}")

        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    run_loop()
