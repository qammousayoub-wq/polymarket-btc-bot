import os
import time
import json
import math
from typing import Dict, Any, List, Optional, Tuple

import requests

GAMMA = "https://gamma-api.polymarket.com"
STATE_FILE = os.environ.get("STATE_FILE", "scanner_state.json")

# --- CONFIG (variables d'env possibles) ---
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "30"))  # 15–60
LIMIT = int(os.environ.get("LIMIT", "100"))               # events par page
MAX_PAGES = int(os.environ.get("MAX_PAGES", "3"))         # 3 pages => 300 events

# Mots-clés: "btc,bitcoin,eth,ethereum,sol,solana"
KEYWORDS = [k.strip().lower() for k in os.environ.get(
    "KEYWORDS",
    "btc,bitcoin,eth,ethereum,sol,solana"
).split(",") if k.strip()]

# Arbitrage alert si YES+NO <= (1 - EDGE_MIN)
EDGE_MIN = float(os.environ.get("EDGE_MIN", "0.03"))      # 0.03 = 3%
# Spike alert si prix bouge de >= SPIKE_DELTA (ex: 0.15 = 15 points)
SPIKE_DELTA = float(os.environ.get("SPIKE_DELTA", "0.15"))

# Telegram
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

# Anti-spam
MAX_ALERTS_PER_CYCLE = int(os.environ.get("MAX_ALERTS_PER_CYCLE", "8"))

# ------------------------------------------------------------

def tg_send(text: str) -> None:
    if not BOT_TOKEN or not CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": CHAT_ID, "text": text[:3900]}, timeout=20)
    except Exception:
        pass

def load_state() -> Dict[str, Any]:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {
            "seen_event_ids": [],
            "seen_market_ids": [],
            "prices": {},  # market_id -> {"yes": float, "no": float, "ts": int}
            "started_at": int(time.time())
        }

def save_state(state: Dict[str, Any]) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f)

def gamma_get(path: str, params: Dict[str, Any]) -> Any:
    r = requests.get(f"{GAMMA}{path}", params=params, timeout=25)
    r.raise_for_status()
    return r.json()

def fetch_active_events(limit: int, offset: int) -> List[Dict[str, Any]]:
    # Doc: /events?active=true&closed=false&limit=&offset=
    return gamma_get("/events", {
        "active": "true",
        "closed": "false",
        "limit": limit,
        "offset": offset
    })

def safe_lower_join(parts: List[str]) -> str:
    return " ".join([p for p in parts if isinstance(p, str)]).lower()

def match_keywords(text: str) -> List[str]:
    hits = []
    t = text.lower()
    for k in KEYWORDS:
        if k and k in t:
            hits.append(k)
    return hits

def event_url(slug: str) -> str:
    return f"https://polymarket.com/event/{slug}" if slug else ""

def market_url(slug: str) -> str:
    return f"https://polymarket.com/market/{slug}" if slug else ""

def parse_binary_prices(market: Dict[str, Any]) -> Optional[Tuple[float, float]]:
    """
    Essaie de récupérer un prix YES/NO depuis plusieurs champs possibles.
    Gamma renvoie souvent outcomePrices (strings) pour les outcomes.
    On fait du "best effort" : si on trouve 2 prix, on retourne (yes, no).
    """
    # 1) outcomePrices: ex ["0.62","0.38"]
    op = market.get("outcomePrices") or market.get("outcome_prices")
    if isinstance(op, list) and len(op) >= 2:
        try:
            yes = float(op[0])
            no = float(op[1])
            if 0 <= yes <= 1 and 0 <= no <= 1:
                return yes, no
        except Exception:
            pass

    # 2) outcomes + prices (parfois)
    outcomes = market.get("outcomes")
    prices = market.get("prices")
    if isinstance(outcomes, list) and isinstance(prices, list) and len(outcomes) >= 2 and len(prices) >= 2:
        try:
            # on assume order YES, NO si binary
            yes = float(prices[0])
            no = float(prices[1])
            if 0 <= yes <= 1 and 0 <= no <= 1:
                return yes, no
        except Exception:
            pass

    # 3) lastTradePrice / bestBid / bestAsk : si dispo en dict
    # (on ne peut pas reconstruire NO facilement si on a qu'un seul côté)
    return None

def is_probably_binary_market(market: Dict[str, Any]) -> bool:
    # best-effort: 2 outcomes ou outcomePrices length 2
    op = market.get("outcomePrices") or market.get("outcome_prices")
    if isinstance(op, list) and len(op) == 2:
        return True
    outs = market.get("outcomes")
    if isinstance(outs, list) and len(outs) == 2:
        return True
    return False

def summarize_market_text(event: Dict[str, Any], market: Dict[str, Any]) -> str:
    parts = []
    for k in ["title", "description", "slug"]:
        v = event.get(k)
        if isinstance(v, str):
            parts.append(v)
    for k in ["question", "title", "description", "slug"]:
        v = market.get(k)
        if isinstance(v, str):
            parts.append(v)
    return safe_lower_join(parts)

def main():
    if not BOT_TOKEN or not CHAT_ID:
        print("❌ Il manque TELEGRAM_BOT_TOKEN ou TELEGRAM_CHAT_ID dans les variables d'env.")
        return

    state = load_state()
    seen_events = set(state.get("seen_event_ids", []))
    seen_markets = set(state.get("seen_market_ids", []))
    prices_state: Dict[str, Any] = state.get("prices", {}) or {}

    tg_send("✅ Polymarket Mega Scanner ON (BTC/ETH/SOL + arbitrage + spikes).")

    while True:
        alerts_sent = 0
        try:
            for page in range(MAX_PAGES):
                offset = page * LIMIT
                events = fetch_active_events(LIMIT, offset)
                if not events:
                    break

                for e in events:
                    eid = str(e.get("id") or "")
                    eslug = e.get("slug") or ""
                    etitle = e.get("title") or "(no title)"

                    markets = e.get("markets") or []
                    if not isinstance(markets, list):
                        continue

                    # --- NEW EVENT / KEYWORD ALERT (si event ou un de ses markets match) ---
                    if eid and eid not in seen_events:
                        # scan keywords across event + markets
                        event_text_parts = []
                        for k in ["title", "description", "slug"]:
                            v = e.get(k)
                            if isinstance(v, str):
                                event_text_parts.append(v)

                        mk_text_parts = []
                        for m in markets[:10]:  # limite pour perf
                            for k in ["question", "title", "description", "slug"]:
                                v = m.get(k) if isinstance(m, dict) else None
                                if isinstance(v, str):
                                    mk_text_parts.append(v)

                        combined = safe_lower_join(event_text_parts + mk_text_parts)
                        hits = match_keywords(combined)

                        if hits and alerts_sent < MAX_ALERTS_PER_CYCLE:
                            hits_str = ", ".join(sorted(set(hits)))
                            msg = (
                                f"🟠 NEW CRYPTO EVENT ({hits_str})\n"
                                f"{etitle}\n"
                                f"{event_url(eslug)}\n"
                                f"Event ID: {eid}"
                            )
                            tg_send(msg)
                            alerts_sent += 1

                        seen_events.add(eid)

                    # --- MARKET LEVEL: spikes + arbitrage ---
                    for m in markets:
                        if not isinstance(m, dict):
                            continue

                        mid = str(m.get("id") or m.get("market_id") or "")
                        mslug = m.get("slug") or ""
                        q = m.get("question") or m.get("title") or "(no question)"

                        # keyword filter: only watch markets matching keywords (keeps noise low)
                        text = summarize_market_text(e, m)
                        hits = match_keywords(text)
                        if not hits:
                            # on peut commenter cette ligne si tu veux scanner TOUS les marchés
                            continue

                        # mark seen market
                        if mid:
                            seen_markets.add(mid)

                        # price tracking for binary markets only
                        if not is_probably_binary_market(m):
                            continue

                        parsed = parse_binary_prices(m)
                        if not parsed:
                            continue

                        yes, no = parsed
                        if not (0 <= yes <= 1 and 0 <= no <= 1):
                            continue

                        now = int(time.time())
                        prev = prices_state.get(mid)

                        # --- SPIKE ALERT ---
                        if isinstance(prev, dict):
                            py = float(prev.get("yes", yes))
                            pn = float(prev.get("no", no))
                            if (abs(yes - py) >= SPIKE_DELTA or abs(no - pn) >= SPIKE_DELTA) and alerts_sent < MAX_ALERTS_PER_CYCLE:
                                hits_str = ", ".join(sorted(set(hits)))
                                msg = (
                                    f"⚡ PRICE SPIKE ({hits_str})\n"
                                    f"{q}\n"
                                    f"YES: {py:.2f} → {yes:.2f} | NO: {pn:.2f} → {no:.2f}\n"
                                    f"{market_url(mslug)}\n"
                                    f"Market ID: {mid}"
                                )
                                tg_send(msg)
                                alerts_sent += 1

                        # --- ARBITRAGE ALERT (best-effort) ---
                        # If YES+NO < 1 - EDGE_MIN -> possible arbitrage (à vérifier liquidité/spread)
                        s = yes + no
                        if s <= (1.0 - EDGE_MIN) and alerts_sent < MAX_ALERTS_PER_CYCLE:
                            edge = 1.0 - s
                            hits_str = ", ".join(sorted(set(hits)))
                            msg = (
                                f"💰 POSSIBLE ARB ({hits_str})\n"
                                f"{q}\n"
                                f"YES: {yes:.2f} | NO: {no:.2f} | Sum: {s:.2f}\n"
                                f"Edge ~ {edge*100:.1f}% (verify spread/liquidity)\n"
                                f"{market_url(mslug)}\n"
                                f"Market ID: {mid}"
                            )
                            tg_send(msg)
                            alerts_sent += 1

                        # update price state
                        prices_state[mid] = {"yes": yes, "no": no, "ts": now}

            # persist state
            state["seen_event_ids"] = list(seen_events)[-5000:]
            state["seen_market_ids"] = list(seen_markets)[-10000:]
            state["prices"] = prices_state
            save_state(state)

        except Exception as ex:
            tg_send(f"⚠️ Scanner error: {ex}")

        time.sleep(POLL_SECONDS)

if __name__ == "__main__":
    main()
