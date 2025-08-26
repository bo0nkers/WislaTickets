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
    1) Najpierw łapiemy dokładnie 'Sprzedane bilety: <liczba>'
    2) (opcjonalnie) zbieramy dostępność per-sektor (fallback / dodatkowa metryka)
    Zwraca: dict z polami: sold_tickets, total_available (opcjonalnie), sectors, notes
    """
    notes = []
    page.wait_for_load_state("networkidle")

    # Cookie banner (jeśli jest)
    for label in ["Tylko niezbędne dane", "Zgadzam się", "Akceptuj", "Accept"]:
        try:
            page.get_by_text(label, exact=False).first.click(timeout=1500)
            notes.append(f"Clicked cookie: {label}")
            break
        except:
            pass

    # Upewnijmy się, że treść strony jest wczytana
    page.wait_for_selector("body", timeout=20000)

    body_text = page.locator("body").inner_text()

    # --- KLUCZ: 'Sprzedane bilety: <liczba>' ---
    sold = None
    # kilka wariantów na wszelki wypadek
    for pattern in [
        r"Sprzedane\s+bilety\s*:\s*(\d[\d\s\u00A0]*)",
        r"Sprzedanych\s+biletów\s*:\s*(\d[\d\s\u00A0]*)",
    ]:
        m = re.search(pattern, body_text, re.I)
        if m:
            sold = parse_int(m.group(1))
            break
    if sold is None:
        notes.append("Nie znaleziono wzorca 'Sprzedane bilety:'")

    # --- Dodatkowo: spróbujmy policzyć dostępne per sektor (może być puste – to tylko dodatek) ---
    sectors = []
    total_available = None
    try:
        # czekamy na elementy sektorów, ale nie blokujemy jeśli ich nie ma
        page.wait_for_selector("svg, [data-testid*='sector'], [class*='sector']", timeout=2000)
        locs = page.locator("[data-testid*='sector'], [class*='sector'], [aria-label*='Sektor'], [aria-label*='sektor']")
        count = locs.count()
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
                mname = re.search(r"(Sektor|Sector)\s*([A-Z]\d{0,2}|[A-Z]+)", cand, re.I)
                if mname and not name:
                    name = mname.group(2)
                mfree = re.search(r"(dostępnych|available|wolnych)[^\d]*(\d[\d\s\u00A0]*)", cand, re.I)
                if mfree and avail is None:
                    avail = parse_int(mfree.group(2))
            if name and isinstance(avail, int):
                sectors.append({"sector": name, "available": avail})
        if sectors:
            total_available = sum(s["available"] for s in sectors)
    except:
        pass

    # Globalny fallback na 'Dostępne: X' (jeśli kiedyś pojawi się taka etykieta)
    if total_available is None:
        m = re.search(r"(Dostępne|Available)\s*:\s*(\d[\d\s\u00A0]*)", body_text, re.I)
        if m:
            total_available = parse_int(m.group(2))
            notes.append("Used global 'Dostępne:' fallback")

    return {
        "sold_tickets": sold,
        "total_available": total_available,
        "sectors": sectors,
        "notes": "; ".join(notes)
    }

def to_csv(path, row: dict):
    df = pd.DataFrame([row])
    # jeśli istnieje stary CSV z innymi kolumnami – usuń go w repo, żeby uniknąć mieszania nagłówków
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
    # (opcjonalnie) podaj pojemność stadionu, wtedy policzymy dostępne = capacity - sold
    CAPACITY = os.getenv("TOTAL_CAPACITY")  # np. 33000
    CAPACITY = int(CAPACITY) if CAPACITY and CAPACITY.isdigit() else None

    if not EVENT_URL and not EVENT_ID:
        print("❌ Podaj EVENT_ID (parametr --event-id lub ENV EVENT_ID).", file=sys.stderr)
        sys.exit(1)
    if EVENT_ID and not EVENT_URL:
        EVENT_URL = f"https://bilety.wislakrakow.com/Stadium/Index?eventId={EVENT_ID}"

    ts = datetime.now(timezone.utc).isoformat()

    success = False
    notes = ""
    result = {
        "sold_tickets": None,
        "total_available": None,
        "sectors": [],
        "notes": ""
    }

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(user_agent="Mozilla/5.0 (compatible; WislaTicketWatcher/1.0)")
        page = ctx.new_page()
        page.goto(EVENT_URL, wait_until="domcontentloaded")
        try:
            result = extract_data(page)
            success = True
        except Exception as e:
            result["notes"] = f"extract_error: {e}"
        finally:
            browser.close()

    # jeśli mamy pojemność i liczbę sprzedanych – policz dostępne
    computed_available = None
    if CAPACITY and isinstance(result.get("sold_tickets"), int):
        computed_available = max(CAPACITY - result["sold_tickets"], 0)

    row = {
        "timestamp_utc": ts,
        "event_id": EVENT_ID,
        "event_url": EVENT_URL,
        "sold_tickets": result.get("sold_tickets"),
        # preferuj licznik z sektorów, a jeśli brak – użyj z pojemności (jeśli podana)
        "total_available": result.get("total_available") if result.get("total_available") is not None else computed_available,
        "sectors_json": json.dumps(result.get("sectors") or [], ensure_ascii=False),
        "success": success,
        "notes": result.get("notes","")
    }

    # CSV (repo)
    to_csv(OUTPUT_CSV, row)

    # Prosty alert gdy zbliża się komplet – wykorzystaj próg do wczesnego powiadomienia
    if ALERT_THRESHOLD and isinstance(row["total_available"], int) and row["total_available"] <= ALERT_THRESHOLD:
        print(f"[ALERT] Dostępne miejsca ≤ {ALERT_THRESHOLD}: {row['total_available']}")

    # Log JSON (ułatwia diagnostykę w Actions)
    print(json.dumps(row, ensure_ascii=False))

if __name__ == "__main__":
    main()
