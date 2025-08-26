import os, re, json, argparse, sys
from datetime import datetime, timezone
from dotenv import load_dotenv
import pandas as pd
from playwright.sync_api import sync_playwright

load_dotenv()

def parse_int(txt: str | None):
    if not txt:
        return None
    m = re.search(r"(\d[\d\s\u00A0]*)", txt)
    if not m:
        return None
    return int(m.group(1).replace(" ", "").replace("\u00A0",""))

def extract_data(page):
    """
    Ekstrakcja dostępności miejsc:
    1) próba policzenia per-sektor (aria-label/title/innerText)
    2) fallback: globalny licznik z tekstu strony („Dostępne: …”)
    Zwraca: (sectors:list[{'sector':str,'available':int}], total_available:int|None, notes:str)
    """
    notes = []

    # doczekaj do spokoju sieci
    page.wait_for_load_state("networkidle")

    # Cookie banner (jeśli jest)
    for label in ["Tylko niezbędne dane", "Zgadzam się", "Akceptuj", "Accept"]:
        try:
            page.get_by_text(label, exact=False).first.click(timeout=1500)
            notes.append(f"Clicked cookie: {label}")
            break
        except:
            pass

    # Czekamy aż pojawi się mapa stadionu / listy sektorów (czyste CSS — bez 'css=' / 'text=' miksu)
    page.wait_for_selector("svg, [data-testid*='sector'], [class*='sector']", timeout=20000)
    # Nieważne, ale jeśli jest „KUP BILET”, spróbujmy poczekać chwilę (nie blokujące)
    try:
        page.get_by_text("KUP BILET", exact=False).first.wait_for(timeout=2000)
    except:
        pass

    sectors = []
    try:
        locs = page.locator("[data-testid*='sector'], [class*='sector'], [aria-label*='Sektor'], [aria-label*='sektor']")
        count = locs.count()
    except:
        locs = None
        count = 0

    for i in range(count):
        el = locs.nth(i)
        aria = el.get_attribute("aria-label")
        title = el.get_attribute("title")
        txt = ""
        try:
            txt = el.inner_text().strip()
        except:
            pass

        name = None
        avail = None

        for cand in filter(None, [aria, title, txt]):
            # nazwa sektora (np. "Sektor B12")
            mname = re.search(r"(Sektor|Sector)\s*([A-Z]\d{0,2}|[A-Z]+)", cand, re.I)
            if mname and not name:
                name = mname.group(2)
            # liczba dostępnych miejsc
            mfree = re.search(r"(dostępnych|available|wolnych)[^\d]*(\d[\d\s\u00A0]*)", cand, re.I)
            if mfree and avail is None:
                avail = parse_int(mfree.group(2))

        if name and isinstance(avail, int):
            sectors.append({"sector": name, "available": avail})

    total_available = sum(s["available"] for s in sectors) if sectors else None

    # Globalny fallback, jeśli nie udało się policzyć z sektorów
    if total_available is None:
        body_text = page.locator("body").inner_text()
        m = re.search(r"(Dostępne|Available)\s*:\s*(\d[\d\s\u00A0]*)", body_text, re.I)
        if m:
            total_available = parse_int(m.group(2))
            notes.append("Used global 'Dostępne:' fallback")

    return sectors, total_available, "; ".join(notes)

def to_csv(path, row: dict):
    df = pd.DataFrame([row])
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

    if not EVENT_URL and not EVENT_ID:
        print("❌ Podaj EVENT_ID (parametr --event-id lub ENV EVENT_ID).", file=sys.stderr)
        sys.exit(1)
    if EVENT_ID and not EVENT_URL:
        EVENT_URL = f"https://bilety.wislakrakow.com/Stadium/Index?eventId={EVENT_ID}"

    ts = datetime.now(timezone.utc).isoformat()

    success = False
    notes = ""
    sectors = []
    total_available = None

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(user_agent="Mozilla/5.0 (compatible; WislaTicketWatcher/1.0)")
        page = ctx.new_page()
        page.goto(EVENT_URL, wait_until="domcontentloaded")
        try:
            sectors, total_available, notes = extract_data(page)
            success = True
        except Exception as e:
            notes = f"extract_error: {e}"
        finally:
            browser.close()

    row = {
        "timestamp_utc": ts,
        "event_id": EVENT_ID,
        "event_url": EVENT_URL,
        "total_available": total_available,
        "sectors_json": json.dumps(sectors, ensure_ascii=False),
        "success": success,
        "notes": notes
    }

    # CSV (repo)
    to_csv(OUTPUT_CSV, row)

    # Prosty alert do loga/stdout
    if ALERT_THRESHOLD and total_available is not None and total_available <= ALERT_THRESHOLD:
        print(f"[ALERT] Dostępne miejsca ≤ {ALERT_THRESHOLD}: {total_available}")

    # Krótki JSON do logów Actions
    print(json.dumps(row, ensure_ascii=False))

if __name__ == "__main__":
    main()
