import os, re, json, argparse, sys, time
from datetime import datetime, timezone
from dotenv import load_dotenv
import pandas as pd
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

load_dotenv()

def parse_int(txt: str | None):
    if not txt:
        return None
    m = re.search(r"(\d[\d\s\u00A0]*)", txt)
    if not m:
        return None
    return int(m.group(1).replace(" ", "").replace("\u00A0",""))

SOLD_PATTERNS = [
    r"Sprzedane\s*bilety\s*[:\-]?\s*(\d[\d\s\u00A0]*)",
    r"Sprzedanych\s*biletów\s*[:\-]?\s*(\d[\d\s\u00A0]*)",
    r"Sprzedano\s*bilet[yów]*\s*[:\-]?\s*(\d[\d\s\u00A0]*)",
]

def find_sold_in_text(text: str):
    if not text:
        return None
    for pat in SOLD_PATTERNS:
        m = re.search(pat, text, re.I)
        if m:
            return parse_int(m.group(1))
    return None

def try_read_sold_on(page, url, notes, wait_text_hint=None):
    """
    Otwiera URL, czeka aż strona się uspokoi i szuka 'Sprzedane bilety: X' w inner_text body.
    Opcjonalnie może krótko poczekać na tekst-hint (np. 'Sprzedane bilety').
    """
    page.goto(url, wait_until="domcontentloaded")
    # cookies / zgody
    for label in ["Tylko niezbędne dane", "Zgadzam się", "Akceptuj", "Accept"]:
        try:
            page.get_by_text(label, exact=False).first.click(timeout=1500)
            notes.append(f"Clicked cookie: {label}")
            break
        except:
            pass

    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except PWTimeout:
        notes.append("networkidle timeout")

    # czasem tekst doskakuje chwilę później (JS)
    if wait_text_hint:
        try:
            page.get_by_text(wait_text_hint, exact=False).first.wait_for(timeout=3000)
        except:
            pass
    else:
        try:
            page.wait_for_selector("body", timeout=10000)
        except:
            pass

    # dajmy JS jeszcze ułamek sekundy
    time.sleep(0.5)

    try:
        body_text = page.locator("body").inner_text()
    except:
        body_text = ""

    sold = find_sold_in_text(body_text)
    return sold, body_text

def save_row_csv(path, row: dict):
    df = pd.DataFrame([row])
    # zawsze zapisujemy (nawet gdy sold_tickets None) – żeby commit poszedł
    if os.path.exists(path):
        df.to_csv(path, mode="a", header=False, index=False)
    else:
        df.to_csv(path, index=False)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--event-id", type=int, help="ID meczu (nadpisuje ENV EVENT_ID)")
    args = parser.parse_args()

    EVENT_ID = args.event_id or int(os.getenv("EVENT_ID", "0") or "0")
    EVENT_URL = os.getenv("EVENT_URL") or (f"https://bilety.wislakrakow.com/Stadium/Index?eventId={EVENT_ID}" if EVENT_ID else "")
    OUTPUT_CSV = os.getenv("OUTPUT_CSV", "ticket_snapshots.csv")
    ALERT_THRESHOLD = int(os.getenv("ALERT_THRESHOLD", "0"))
    CAPACITY = os.getenv("TOTAL_CAPACITY")
    CAPACITY = int(CAPACITY) if CAPACITY and CAPACITY.isdigit() else None

    if not EVENT_URL and not EVENT_ID:
        print("❌ Podaj EVENT_ID (parametr --event-id lub ENV EVENT_ID).", file=sys.stderr)
        sys.exit(1)
    if EVENT_ID and not EVENT_URL:
        EVENT_URL = f"https://bilety.wislakrakow.com/Stadium/Index?eventId={EVENT_ID}"

    ts = datetime.now(timezone.utc).isoformat()
    notes = []
    sold_tickets = None
    total_available = None

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(user_agent="Mozilla/5.0 (compatible; WislaTicketWatcher/1.0)")
        page = ctx.new_page()

        # 1) Spróbuj NA STRONIE GŁÓWNEJ – tu zwykle jest 'Sprzedane bilety: X'
        try:
            sold_tickets, body_home = try_read_sold_on(
                page, "https://bilety.wislakrakow.com/", notes, wait_text_hint="Sprzedane"
            )
            if sold_tickets is not None:
                notes.append("sold from homepage")
        except Exception as e:
            notes.append(f"home_error: {e}")

        # 2) Jeśli nie znaleziono – spróbuj na stronie eventu
        if sold_tickets is None and EVENT_URL:
            try:
                sold_tickets, body_event = try_read_sold_on(
                    page, EVENT_URL, notes, wait_text_hint="Sprzedane"
                )
                if sold_tickets is not None:
                    notes.append("sold from event page")
            except Exception as e:
                notes.append(f"event_error: {e}")

        # 3) Jeśli mamy pojemność – policz 'available'
        if CAPACITY and isinstance(sold_tickets, int):
            total_available = max(CAPACITY - sold_tickets, 0)

        browser.close()

    row = {
        "timestamp_utc": ts,
        "event_id": EVENT_ID,
        "event_url": EVENT_URL,
        "sold_tickets": sold_tickets,
        "total_available": total_available,
        "sectors_json": "[]",  # zostawiamy pole dla zgodności
        "success": True,
        "notes": "; ".join(notes) if notes else ""
    }

    save_row_csv(OUTPUT_CSV, row)

    if ALERT_THRESHOLD and isinstance(total_available, int) and total_available <= ALERT_THRESHOLD:
        print(f"[ALERT] Dostępne miejsca ≤ {ALERT_THRESHOLD}: {total_available}")

    print(json.dumps(row, ensure_ascii=False))

if __name__ == "__main__":
    main()
