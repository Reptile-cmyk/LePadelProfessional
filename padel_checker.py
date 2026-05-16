#!/usr/bin/env python3
"""
padel_checker.py  v4  — Copenhagen Padel Availability Checker
═══════════════════════════════════════════════════════════════════════════════
Filter: start 17:30–18:30 Copenhagen time | min 60 min | prefers 90 min

Platform map:
  PadelMates  (open REST API, no auth needed)
      Racket Club Taastrup      club_id: 54EX259qJIe1WWtlkJ4WEH25aRg1  ✓
      Match Padel Kløvermarken  club_id: run --discover-klover to find it

  MATCHi  (semi-public REST API, no auth needed)
      Padel Yard Jernbanebyen   facility_id: see setup
      Padel Yard Reffen         facility_id: see setup

  WannaSport  (Playwright)   → Match Padel Ballerup
  Bookli      (Playwright)   → Cupra Arena

Install:
  pip install requests beautifulsoup4 playwright
  playwright install chromium       <- only for Ballerup, Cupra, and --discover

Usage:
  python padel_checker.py                     # check today
  python padel_checker.py --date 2026-05-22
  python padel_checker.py --days 7
  python padel_checker.py --no-browser        # skip Playwright centres
  python padel_checker.py --discover-klover   # auto-find Kløvermarken club_id

═══════════════════════════════════════════════════════════════════════════════
  SETUP — fill in the two MATCHi IDs  (2 min)
═══════════════════════════════════════════════════════════════════════════════
  1. Open https://www.matchi.se/facilities/padelyardjernbanebyen in Chrome
  2. DevTools (F12) → Network → filter "Fetch/XHR"
  3. Click a date in the calendar
  4. Find a request with "facilityId=XXXX" in the URL → copy the integer
  Repeat for https://www.matchi.se/facilities/padelyard  (Reffen)
═══════════════════════════════════════════════════════════════════════════════
"""

import argparse
import asyncio
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

TZ       = ZoneInfo("Europe/Copenhagen")
EARLIEST = "17:30"
LATEST   = "18:30"
MIN_DUR  = 60    # minimum duration to show (minutes)
MAX_DUR  = 90    # maximum duration to show — filters out 120 min blocks
PREF_DUR = 90    # duration that earns a ★

# ══════════════════════════════════════════════════════════════════════════════
#  CONFIGURATION
# ══════════════════════════════════════════════════════════════════════════════

PADELMATES_CLUBS: dict[str, Optional[str]] = {
    "Racket Club Taastrup":      "54EX259qJIe1WWtlkJ4WEH25aRg1",  # ✓ confirmed
    "Match Padel Kløvermarken":  "PDXpw2Hh4ZaSI6sTxslHS7tpelV2",  # ✓ confirmed
}

MATCHI_FACILITIES: dict[str, Optional[int]] = {
    "Padel Yard Jernbanebyen": 2834,
    "Padel Yard Reffen":       917,
}

# PadelMates booking page — used to auto-discover unknown club_ids
RACKET_CLUB_PAGES = {
    "Match Padel Kløvermarken": "https://racketclub.dk/centre/klover",
}

# ══════════════════════════════════════════════════════════════════════════════

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "application/json, text/html, */*",
    "Accept-Language": "da-DK,da;q=0.9,en;q=0.8",
})


# ── Data model ────────────────────────────────────────────────────────────────
@dataclass
class Slot:
    centre:   str
    court:    str
    start:    str           # Copenhagen local "HH:MM"
    end:      str
    duration: int           # minutes
    price:    Optional[str] = None
    book_url: str = ""

    @property
    def preferred(self) -> bool:
        return self.duration >= PREF_DUR

    def __str__(self) -> str:
        star  = "★" if self.preferred else "○"
        price = f"  [{self.price}]" if self.price else ""
        return f"    {star}  {self.start}–{self.end}  ({self.duration} min)  {self.court}{price}"


# ── Helpers ───────────────────────────────────────────────────────────────────
def to_mins(hhmm: str) -> int:
    h, m = map(int, hhmm.split(":"))
    return h * 60 + m

def from_mins(m: int) -> str:
    return f"{m // 60:02d}:{m % 60:02d}"

def qualifies(start: str, duration: int) -> bool:
    return EARLIEST <= start <= LATEST and MIN_DUR <= duration <= MAX_DUR

def is_double_indoor(name: str) -> bool:
    """Exclude single courts and outdoor courts."""
    low = name.lower()
    # Exclude singles
    if "single" in low or "enkelt" in low:
        return False
    # Exclude outdoor indicators
    if any(x in low for x in ("outdoor", "ude", "outside", "utomhus", " out")):
        return False
    return True

def day_ts_ms(d: date) -> tuple[int, int]:
    """Midnight–23:59 Copenhagen time as Unix milliseconds."""
    s = datetime(d.year, d.month, d.day,  0,  0, tzinfo=TZ)
    e = datetime(d.year, d.month, d.day, 23, 59, tzinfo=TZ)
    return int(s.timestamp() * 1000), int(e.timestamp() * 1000)


# ══════════════════════════════════════════════════════════════════════════════
#  PLATFORM 1 — PadelMates  (open REST API, no auth)
# ══════════════════════════════════════════════════════════════════════════════
#
#  GET https://fastapi-production-fargate.padelmates.io/player/
#          player_booking/all_courts_slot_prices_v2
#          ?club_id=<str>&start_datetime=<ms>&end_datetime=<ms>
#
#  allSlots[]:
#    courtName           str    e.g. "CC│STATE"
#    duration            int    minutes
#    price               float  DKK
#    startTimestamp      int    Unix ms UTC  ← convert to Copenhagen for filter
#    endTimestamp        int    Unix ms UTC
#    reservedIntersection bool  true = taken, skip

PM_API = (
    "https://fastapi-production-fargate.padelmates.io"
    "/player/player_booking/all_courts_slot_prices_v2"
)

def scrape_padelmates(centre: str, club_id: Optional[str], check_date: date) -> list[Slot]:
    if not club_id:
        print(f"  [SKIP] {centre}: club_id not set  →  run --discover-klover")
        return []

    s_ms, e_ms = day_ts_ms(check_date)
    try:
        r = SESSION.get(
            PM_API,
            params={"club_id": club_id, "start_datetime": s_ms, "end_datetime": e_ms},
            timeout=10,
        )
        r.raise_for_status()
    except Exception as exc:
        print(f"  [ERR] {centre}: {exc}")
        return []

    slots: list[Slot] = []
    for item in r.json().get("allSlots", []):
        if item.get("reservedIntersection", True):
            continue

        court    = item.get("courtName", "?")
        duration = item.get("duration", 0)
        price    = item.get("price", None)

        # Convert UTC timestamps → Copenhagen local time
        start_dt = datetime.fromtimestamp(item["startTimestamp"] / 1000, tz=TZ)
        end_dt   = datetime.fromtimestamp(item["endTimestamp"]   / 1000, tz=TZ)
        start    = start_dt.strftime("%H:%M")
        end      = end_dt.strftime("%H:%M")

        if not is_double_indoor(court) or not qualifies(start, duration):
            continue

        slots.append(Slot(
            centre, court, start, end, duration,
            f"{price:.0f} DKK" if price else None,
            f"https://www.padelmates.com/booking?clubId={club_id}&slotId={item.get('slotId','')}",
        ))
    return slots


# ── club_id auto-discovery via Playwright ────────────────────────────────────
async def discover_club_id(centre: str, page_url: str) -> Optional[str]:
    """
    Opens the Racket Club booking page in a headless browser, waits for the
    PadelMates API call to fire, and extracts the club_id from the URL.
    Prints the result so you can paste it into PADELMATES_CLUBS above.
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print("Playwright not installed. Run: pip install playwright && playwright install chromium")
        return None

    found: list[str] = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page    = await browser.new_page()

        def on_request(req):
            if "all_courts_slot_prices" in req.url and "club_id=" in req.url:
                m = re.search(r"club_id=([^&]+)", req.url)
                if m and not found:
                    found.append(m.group(1))

        page.on("request", on_request)

        print(f"  Opening {page_url} …")
        try:
            await page.goto(page_url, wait_until="networkidle", timeout=25_000)
            # Trigger the calendar by clicking the next available date button
            btn = page.locator("button[aria-label*='day'], .date-button, .calendar-day")
            if await btn.count():
                await btn.first.click()
            await page.wait_for_timeout(4_000)
        except Exception as exc:
            print(f"  [WARN] {exc}")
        finally:
            await browser.close()

    if found:
        print(f"\n  ✓ Found club_id for {centre}:")
        print(f"    {found[0]}")
        print(f"\n  Paste this into PADELMATES_CLUBS in the script:\n")
        print(f'    "{centre}":  "{found[0]}",')
        return found[0]
    else:
        print(f"  [FAIL] Could not capture club_id for {centre}.")
        print("  Try opening the page manually and checking DevTools → Network")
        print(f"  for requests to fastapi-production-fargate.padelmates.io")
        return None


# ══════════════════════════════════════════════════════════════════════════════
#  PLATFORM 2 — MATCHi  (semi-public REST API, no auth)
# ══════════════════════════════════════════════════════════════════════════════
#
#  GET https://www.matchi.se/book/getSchedule
#          ?facilityId=<int>&date=YYYY-MM-DD&sport=PADEL
#  Returns HTML. Available slots: <a data-start data-end data-court-name …>

# ══════════════════════════════════════════════════════════════════════════════
#  PLATFORM 2 — MATCHi
#  Padel Yard Jernbanebyen → /book/schedule  (facilityId=2834)
#  Padel Yard Reffen       → /book/listSlots (facility=917)
#
#  Two different endpoints — both discovered via DevTools Network tab.
#
#  listSlots HTML structure:
#    <h6>Available times <strong>Friday, 29 May</strong> <strong>1730</strong></h6>
#    <table>
#      <tr>
#        <td>01 | Horten (Indoor)</td>
#        <td>60min</td>
#        <td>Padel  Artificial grass</td>
#        <td><a href="/login/auth?returnUrl=...start=MS&end=MS...">Book</a></td>
#      </tr>
#    </table>
#
#  schedule HTML structure: similar but uses <a class="courtLinks"> anchors.
# ══════════════════════════════════════════════════════════════════════════════

# Per-facility config: endpoint path and ID parameter name
MATCHI_CONFIG: dict[str, dict] = {
    "Padel Yard Jernbanebyen": {
        "endpoint":  "schedule",
        "id_param":  "facilityId",
        "facility_id": None,   # filled from MATCHI_FACILITIES below
    },
    "Padel Yard Reffen": {
        "endpoint":  "listSlots",
        "id_param":  "facility",
        "facility_id": None,
    },
}

MATCHI_BASE = "https://www.matchi.se/book/"


def _parse_listslots(html: str, centre: str, check_date: date,
                     facility_id: int) -> list[Slot]:
    """
    Parse the /book/listSlots HTML response.
    Times are grouped under h6 headers containing the start time as HHMM.
    Book links contain UTC millisecond timestamps for start/end.
    """
    soup  = BeautifulSoup(html, "html.parser")
    slots: list[Slot] = []

    current_start: Optional[str] = None

    for tag in soup.find_all(["h6", "tr"]):
        # ── Time header ──────────────────────────────────────────────────────
        if tag.name == "h6":
            text   = tag.get_text(strip=True)
            # Time is the last bold/strong word, e.g. "1730"
            bolds  = tag.find_all("strong")
            hhmm_raw = bolds[-1].get_text(strip=True) if bolds else ""
            m = re.match(r"^(\d{2})(\d{2})$", hhmm_raw)
            if m:
                current_start = f"{m.group(1)}:{m.group(2)}"
            else:
                current_start = None
            continue

        # ── Slot row ─────────────────────────────────────────────────────────
        if current_start is None:
            continue

        cells = tag.find_all("td")
        if len(cells) < 2:
            continue

        court_raw = cells[0].get_text(strip=True)
        # Strip leading number: "01 | Horten (Indoor)" → "Horten (Indoor)"
        court = re.sub(r"^\d+\s*[|│]\s*", "", court_raw).strip()
        if not court:
            court = court_raw

        if not is_double_indoor(court):
            continue

        # Duration from cell[1]: "60min" or "90min"
        dur_text = cells[1].get_text(strip=True)
        dur_m    = re.search(r"(\d+)\s*min", dur_text, re.I)
        duration = int(dur_m.group(1)) if dur_m else 60

        # Derive start from the header; validate it's in our window
        start = current_start
        if not qualifies(start, duration):
            continue

        # Get book URL and derive end time from UTC timestamps in the href
        book_link = tag.find("a")
        book_url  = ""
        end       = from_mins(to_mins(start) + duration)   # default

        if book_link:
            href = book_link.get("href", "")
            book_url = f"https://www.matchi.se{href}" if href.startswith("/") else href

            # Extract start/end UTC ms from the URL for accurate local end time
            ts_start = re.search(r"start=(\d+)", href)
            ts_end   = re.search(r"end=(\d+)",   href)
            if ts_start and ts_end:
                s_dt = datetime.fromtimestamp(int(ts_start.group(1)) / 1000, tz=TZ)
                e_dt = datetime.fromtimestamp(int(ts_end.group(1))   / 1000, tz=TZ)
                start    = s_dt.strftime("%H:%M")
                end      = e_dt.strftime("%H:%M")
                duration = to_mins(end) - to_mins(start)
                if not qualifies(start, duration):
                    continue

        slots.append(Slot(centre, court, start, end, duration, None, book_url))

    return slots


def _parse_schedule(html: str, centre: str, facility_id: int) -> list[Slot]:
    """
    Parse the /book/schedule HTML response.
    Available slots are <a class="courtLinks"> with time/court in child elements.
    """
    soup  = BeautifulSoup(html, "html.parser")
    slots: list[Slot] = []

    book_base = "https://www.matchi.se/facilities/padelyardjernbanebyen"

    for el in soup.select("a.courtLinks"):
        text  = el.get_text(separator=" ", strip=True)
        times = re.findall(r"\b(\d{2}:\d{2})\b", text)
        if len(times) < 2:
            continue
        start, end = times[0], times[1]

        court_el = el.select_one(".court, .resource, [class*='court']")
        court    = court_el.get_text(strip=True) if court_el else text

        if not is_double_indoor(court):
            continue

        duration = to_mins(end) - to_mins(start)
        if not qualifies(start, duration):
            continue

        href     = el.get("href", "")
        book_url = f"https://www.matchi.se{href}" if href.startswith("/") else book_base
        price_m  = re.search(r"(\d[\d\s,.]+)\s*(?:DKK|kr\.?)", text, re.I)

        slots.append(Slot(
            centre, court, start, end, duration,
            price_m.group(0) if price_m else None,
            book_url,
        ))

    return slots


def scrape_matchi(centre: str, facility_id: Optional[int], check_date: date) -> list[Slot]:
    if not facility_id:
        print(f"  [SKIP] {centre}: facility_id not set")
        return []

    import time as _time
    cfg      = MATCHI_CONFIG.get(centre, {"endpoint": "listSlots", "id_param": "facility"})
    endpoint = cfg["endpoint"]
    id_param = cfg["id_param"]
    url      = MATCHI_BASE + endpoint

    params = {
        "wl":      "",
        id_param:  facility_id,
        "sport":   5,
        "week":    "",
        "year":    "",
        "indoor":  "true",
        "_":       int(_time.time() * 1000),
        "date":    check_date.isoformat(),
    }
    if endpoint == "schedule":
        params["s"] = 1   # required by schedule endpoint

    try:
        r = SESSION.get(url, params=params, timeout=10)
        r.raise_for_status()
    except Exception as exc:
        print(f"  [ERR] {centre}: {exc}")
        return []

    if endpoint == "listSlots":
        return _parse_listslots(r.text, centre, check_date, facility_id)
    else:
        return _parse_schedule(r.text, centre, facility_id)


# ══════════════════════════════════════════════════════════════════════════════
#  PLATFORM 3 — WannaSport  (Playwright)  →  Match Padel Ballerup
# ══════════════════════════════════════════════════════════════════════════════

# ══════════════════════════════════════════════════════════════════════════════
#  PLATFORM 3 — WannaSport  (open REST API, no auth)
#  Match Padel Ballerup
# ══════════════════════════════════════════════════════════════════════════════
#
#  GET https://wsapi.wannasport.com/api/public/Product/product-section-by-slug
#       ?facilitySlug=match-padel-ballerup
#       &productSlug=padeltennis-inde      ← indoor double courts
#       &duration=60&duration=90
#
#  Response: product.timetable[] with date + timeSpans[].time (local ISO)
#  Times are already in Copenhagen local time — no UTC conversion needed.
#  Returns ~14 days of availability in one call.

# ══════════════════════════════════════════════════════════════════════════════
#  PLATFORM 3 — WannaSport  (open REST API, no auth)
#  Match Padel Ballerup + Cupra Arena
# ══════════════════════════════════════════════════════════════════════════════
#
#  GET https://wsapi.wannasport.com/api/public/Product/product-section-by-slug
#       ?facilitySlug=<slug>&productSlug=<slug>&duration=60&duration=90
#
#  Times in product.timetable[].timeSpans[].time are Copenhagen local ISO —
#  no UTC conversion needed. One call returns ~14 days of availability.

WANNASPORT_API = "https://wsapi.wannasport.com/api/public/Product/product-section-by-slug"

WANNASPORT_VENUES = {
    "Match Padel Ballerup": {
        "facilitySlug": "match-padel-ballerup",
        "productSlug":  "padeltennis-inde",
        "bookUrl":      "https://www.wannasport.com/dnk/da/ballerup/padel/padeltennis-inde",
    },
    "Cupra Arena": {
        "facilitySlug": "padelpadel-cph-cupra-arena",
        "productSlug":  "padelbane-inde-double",
        "bookUrl":      "https://www.wannasport.com/dnk/da/cupra-arena/padel/padelbane-inde-double",
    },
}

def scrape_wannasport(centre: str, check_date: date) -> list[Slot]:
    cfg = WANNASPORT_VENUES.get(centre, {})
    if not cfg:
        print(f"  [SKIP] {centre}: not configured")
        return []

    try:
        r = SESSION.get(
            WANNASPORT_API,
            params=[
                ("facilitySlug", cfg["facilitySlug"]),
                ("productSlug",  cfg["productSlug"]),
                ("duration",     "60"),
                ("duration",     "90"),
            ],
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
    except Exception as exc:
        print(f"  [ERR] {centre}: {exc}")
        return []

    target = check_date.isoformat()[:10]
    slots: list[Slot] = []

    for day in data.get("product", {}).get("timetable", []):
        if not day.get("date", "").startswith(target):
            continue
        for span in day.get("timeSpans", []):
            start = datetime.fromisoformat(span["time"]).strftime("%H:%M")
            if not (EARLIEST <= start <= LATEST):
                continue
            for dur in [60, 90]:
                if not qualifies(start, dur):
                    continue
                slots.append(Slot(
                    centre, "Double (Indoor)",
                    start, from_mins(to_mins(start) + dur), dur,
                    None, cfg["bookUrl"],
                ))

    return slots



# ══════════════════════════════════════════════════════════════════════════════
#  ORCHESTRATOR
# ══════════════════════════════════════════════════════════════════════════════

async def check_one_day(d: date) -> list[Slot]:
    print(f"\n📅  {d.strftime('%A %#d %B %Y')}")
    all_slots: list[Slot] = []

    # PadelMates — pure REST, parallelised
    pm_results = await asyncio.gather(*[
        asyncio.to_thread(scrape_padelmates, name, cid, d)
        for name, cid in PADELMATES_CLUBS.items()
    ])
    for name, result in zip(PADELMATES_CLUBS, pm_results):
        print(f"  {'✓' if result else '·'} {name}: {len(result)} slot(s)")
        all_slots.extend(result)

    # MATCHi — direct HTTP (sync, fast)
    for name, fid in MATCHI_FACILITIES.items():
        result = scrape_matchi(name, fid, d)
        print(f"  {'✓' if result else '·'} {name}: {len(result)} slot(s)")
        all_slots.extend(result)

    # Playwright centres
    # WannaSport — pure REST for both Ballerup and Cupra Arena
    for ws_centre in WANNASPORT_VENUES:
        result = scrape_wannasport(ws_centre, d)
        print(f"  {'✓' if result else '·'} {ws_centre}: {len(result)} slot(s)")
        all_slots.extend(result)

    return all_slots


def print_results(slots: list[Slot], d: date) -> None:
    print(f"\n{'═'*60}")
    print(f"  {d.strftime('%A %#d %B %Y')} — 17:30–18:30 · ≥60 min · double")
    print(f"{'═'*60}")

    if not slots:
        print("  No matching slots found.\n")
        return

    by_centre: dict[str, list[Slot]] = {}
    for s in slots:
        by_centre.setdefault(s.centre, []).append(s)

    for centre in sorted(by_centre):
        cslots = sorted(by_centre[centre], key=lambda s: (-s.duration, s.start))
        print(f"\n  📍 {centre}")
        for s in cslots:
            print(s)
        if cslots[0].book_url:
            print(f"     → {cslots[0].book_url}")

    pref = [s for s in slots if s.preferred]
    print(f"\n{'─'*60}")
    print(f"  Total: {len(slots)}  |  ★ 90 min: {len(pref)}")
    print(f"{'─'*60}\n")


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════════════════

async def main() -> None:
    p = argparse.ArgumentParser(description="Copenhagen padel availability checker")
    p.add_argument("--date",  "-d", default=None, help="YYYY-MM-DD (default: today)")
    p.add_argument("--days",  "-n", type=int, default=1, help="Days to check")
    p.add_argument("--discover-klover", action="store_true",
                   help="Auto-find club_id for Match Padel Kløvermarken and exit")
    args = p.parse_args()

    # ── Discovery mode ────────────────────────────────────────────────────────
    if args.discover_klover:
        print("\n🔍  Discovering club_id for Match Padel Kløvermarken …")
        for centre, url in RACKET_CLUB_PAGES.items():
            await discover_club_id(centre, url)
        return

    # ── Normal mode ───────────────────────────────────────────────────────────
    start = (
        date.fromisoformat(args.date)
        if args.date
        else datetime.now(TZ).date()
    )

    grand: list[Slot] = []
    for i in range(args.days):
        d     = start + timedelta(days=i)
        slots = await check_one_day(d)
        print_results(slots, d)
        grand.extend(slots)

    if args.days > 1:
        pref = sum(1 for s in grand if s.preferred)
        print(f"══ {args.days}-day total: {len(grand)} slot(s) | ★ {pref} preferred ══\n")


if __name__ == "__main__":
    asyncio.run(main())
