import os, time, json
import requests

GAMMA = "https://gamma-api.polymarket.com"
KEYWORDS = ["btc", "bitcoin"]
POLL_SECONDS = 30
STATE_FILE = "btc_seen.json"

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

def tg_send(text: str):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    requests.post(url, json={"chat_id": CHAT_ID, "text": text[:3900]}, timeout=20)

def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"seen_event_ids": [], "started_at": int(time.time())}

def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f)

def fetch_active_events(limit=200, offset=0):
    # Events actifs + non clos (c’est ce que tu veux)
    params = {"active": "true", "closed": "false", "limit": limit, "offset": offset}
    r = requests.get(f"{GAMMA}/events", params=params, timeout=20)
    r.raise_for_status()
    return r.json()

def event_url(slug: str) -> str:
    if slug:
        return f"https://polymarket.com/event/{slug}"
    return ""

def market_url(slug: str) -> str:
    if slug:
        return f"https://polymarket.com/market/{slug}"
    return ""

def contains_btc(event: dict) -> bool:
    # Cherche "btc/bitcoin" dans titre/description et dans les questions des markets de l’event
    text_parts = []
    for k in ["title", "description", "slug"]:
        v = event.get(k)
        if isinstance(v, str):
            text_parts.append(v)

    for m in (event.get("markets") or []):
        for k in ["question", "title", "description", "slug"]:
            v = m.get(k)
            if isinstance(v, str):
                text_parts.append(v)

    text = " ".join(text_parts).lower()
    return any(k in text for k in KEYWORDS)

def pick_first_btc_market(event: dict):
    for m in (event.get("markets") or []):
        parts = []
        for k in ["question", "title", "description", "slug"]:
            v = m.get(k)
            if isinstance(v, str):
                parts.append(v)
        if any(k in (" ".join(parts).lower()) for k in KEYWORDS):
            return m
    return (event.get("markets") or [None])[0]

def main():
    if not BOT_TOKEN or not CHAT_ID:
        print("❌ Il manque TELEGRAM_BOT_TOKEN ou TELEGRAM_CHAT_ID dans les variables d'env.")
        print("PowerShell:")
        print('$env:TELEGRAM_BOT_TOKEN="..."')
        print('$env:TELEGRAM_CHAT_ID="..."')
        return

    state = load_state()
    seen = set(state.get("seen_event_ids", []))
    started_at = state.get("started_at", int(time.time()))

    tg_send("✅ Bot BTC ON : je t’envoie une notif UNIQUEMENT quand un NOUVEAU pari actif contient BTC/Bitcoin.")

    while True:
        try:
            events = fetch_active_events(limit=200, offset=0)

            for e in events:
                eid = str(e.get("id") or "")
                if not eid or eid in seen:
                    continue

                # “Nouveau depuis lancement” : si l’API donne une date, on s’en sert; sinon on garde juste seen.
                # (Certaines réponses ont createdAt; on gère les deux cas)
                created = e.get("createdAt") or e.get("created_at")
                if isinstance(created, (int, float)) and created < started_at:
                    # Event plus vieux que le lancement => on ignore
                    seen.add(eid)
                    continue

                if contains_btc(e):
                    title = e.get("title") or "(sans titre)"
                    eslug = e.get("slug") or ""
                    m = pick_first_btc_market(e) or {}
                    mslug = (m.get("slug") if isinstance(m, dict) else "") or ""
                    q = (m.get("question") if isinstance(m, dict) else None) or ""

                    msg = "🟠 Nouveau pari BTC détecté:\n"
                    msg += f"{title}\n"
                    if q:
                        msg += f"Market: {q}\n"
                    if eslug:
                        msg += f"Event: {event_url(eslug)}\n"
                    if mslug:
                        msg += f"Market: {market_url(mslug)}\n"
                    msg += f"ID: {eid}"
                    tg_send(msg)

                # marque comme vu dans tous les cas
                seen.add(eid)

            state["seen_event_ids"] = list(seen)[-2000:]  # limite mémoire
            save_state(state)

        except Exception as ex:
            tg_send(f"⚠️ Bot erreur: {ex}")

        time.sleep(POLL_SECONDS)

if __name__ == "__main__":
    main()
