"""
CageMetrics UFC Stats Scraper
Pulls fighter data from ufcstats.com and pushes to Supabase.
Rate-limited to be polite to ufcstats.com servers.
"""

import os
import re
import time
from datetime import datetime, date
from supabase import create_client, Client

import challenge

# --- Config ---
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_SECRET_KEY = os.environ.get("SUPABASE_SECRET_KEY")
RATE_LIMIT_SECONDS = 1.5  # Time between requests
MAX_ATTEMPTS = 3  # Retries per URL for transient errors

# UFC weight class event listing pages on ufcstats.com group fighters by division
# via the rankings/events, but the cleanest source is the fighter listing pages
# combined with each fighter's most recent fight weight class.
# Strategy: collect URLs alphabetically (covers everyone), then derive division
# from each fighter's most recent fight on their profile page.

DIVISION_MAP = {
    # Order matters: extract_division iterates this dict and substring-matches
    # against the fight-row text. More-specific names must come first so they
    # win over their substring-prefix peers.
    #   - Women's divisions before men's (e.g. "Women's Strawweight" must match
    #     before "Strawweight", or every woman becomes a man.)
    #   - "Light Heavyweight" before "Heavyweight" for the same reason.
    "Women's Strawweight": "Women's Strawweight",
    "Women's Flyweight": "Women's Flyweight",
    "Women's Bantamweight": "Women's Bantamweight",
    "Women's Featherweight": "Women's Featherweight",
    "Light Heavyweight": "Light Heavyweight",
    "Strawweight": "Strawweight",
    "Flyweight": "Flyweight",
    "Bantamweight": "Bantamweight",
    "Featherweight": "Featherweight",
    "Lightweight": "Lightweight",
    "Welterweight": "Welterweight",
    "Middleweight": "Middleweight",
    "Heavyweight": "Heavyweight",
}

# --- Supabase client ---
if not SUPABASE_URL or not SUPABASE_SECRET_KEY:
    raise SystemExit("Missing SUPABASE_URL or SUPABASE_SECRET_KEY environment variables")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_SECRET_KEY)

# One session per run: challenge.py's proof-of-work clearance cookie lives on
# the session, so every request after the first solve passes the interstitial.
session = challenge.make_session()

# --- Helpers ---
def get_soup(url):
    """Fetch a URL and return BeautifulSoup, with rate limiting.

    Solves the UFCStats proof-of-work interstitial (via challenge.py — a plain
    GET returns the challenge page with HTTP 200, which used to parse as an
    empty page) and retries transient errors with backoff."""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        time.sleep(RATE_LIMIT_SECONDS)
        try:
            soup = challenge.get_soup(session, url, timeout=20)
            if soup is not None:
                return soup
            print(f"  ! Challenge unsolved for {url} (attempt {attempt})")
        except Exception as e:
            print(f"  ! Error fetching {url} (attempt {attempt}): {e}")
        if attempt < MAX_ATTEMPTS:
            time.sleep(2 ** attempt)  # 2s, 4s
    return None

def parse_height(s):
    """'5' 11"' -> 71 inches"""
    if not s or s == "--":
        return None
    m = re.match(r"(\d+)' (\d+)\"", s.strip())
    if m:
        return int(m.group(1)) * 12 + int(m.group(2))
    return None

def parse_reach(s):
    """'72.0\"' -> 72.0"""
    if not s or s == "--":
        return None
    m = re.match(r"([\d.]+)", s.strip())
    return float(m.group(1)) if m else None

def parse_pct(s):
    """'56%' -> 56"""
    if not s or s == "--":
        return None
    m = re.match(r"(\d+)", s.strip())
    return int(m.group(1)) if m else None

def parse_num(s):
    """Parse a number, return None on failure."""
    if not s or s == "--":
        return None
    try:
        return float(s.strip())
    except ValueError:
        return None

def parse_dob(s):
    """'Mar 15, 1990' -> date(1990, 3, 15). Returns None on failure."""
    if not s or s == "--":
        return None
    try:
        return datetime.strptime(s.strip(), "%b %d, %Y").date()
    except ValueError:
        return None

def calc_age(dob):
    """Calculate age from a date of birth."""
    if not dob:
        return None
    today = date.today()
    return today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))

def extract_division(soup):
    """
    Find the fighter's division by looking at their most recent fight row.
    The fight history table has a 'Weight class' column we can read.
    """
    # The fight history table on a fighter profile
    rows = soup.select("tr.b-fight-details__table-row")
    for row in rows:
        # Skip header
        if row.get("class") and "b-fight-details__table-row_type_head" in row.get("class", []):
            continue
        cols = row.select("td.b-fight-details__table-col")
        if not cols:
            continue
        # The weight class column on ufcstats fighter pages is typically the 7th column (index 6)
        # Each cell contains a <p> with the value
        for col in cols:
            text = col.get_text(" ", strip=True)
            for wc, clean in DIVISION_MAP.items():
                if wc.lower() in text.lower():
                    return clean
        # Only check the first non-header row (most recent fight)
        break
    return None

def derive_sex(division):
    """Infer sex from division name. Women's divisions are explicitly labeled."""
    if not division:
        return None
    return "F" if division.startswith("Women's") else "M"

# --- Scraping ---
def get_fighter_urls():
    """Get every fighter URL by hitting each letter with page=all."""
    fighter_urls = set()
    base = "http://www.ufcstats.com/statistics/fighters"
    for letter in "abcdefghijklmnopqrstuvwxyz":
        url = f"{base}?char={letter}&page=all"
        print(f"  Fetching list: {letter}")
        soup = get_soup(url)
        if not soup:
            continue
        rows = soup.select("tr.b-statistics__table-row")
        added = 0
        for row in rows:
            a = row.find("a", class_="b-link b-link_style_black")
            if a and a.get("href"):
                fighter_urls.add(a["href"])
                added += 1
        print(f"    {added} fighters")
    return list(fighter_urls)

def parse_fighter(url):
    """Scrape one fighter's profile page."""
    soup = get_soup(url)
    if not soup:
        return None

    # Name + nickname
    name_el = soup.select_one("span.b-content__title-highlight")
    name = name_el.get_text(strip=True) if name_el else None
    nick_el = soup.select_one("p.b-content__Nickname")
    nickname = nick_el.get_text(strip=True) if nick_el else None
    if nickname:
        nickname = nickname.strip('"').strip()

    # Record
    record_el = soup.select_one("span.b-content__title-record")
    wins = losses = draws = 0
    if record_el:
        rtext = record_el.get_text(strip=True).replace("Record:", "").strip()
        m = re.match(r"(\d+)-(\d+)-(\d+)", rtext)
        if m:
            wins, losses, draws = int(m.group(1)), int(m.group(2)), int(m.group(3))

    # Stats from the info list
    info = {}
    for li in soup.select("li.b-list__box-list-item"):
        title = li.select_one("i.b-list__box-item-title")
        if not title:
            continue
        key = title.get_text(strip=True).rstrip(":").lower()
        title.extract()
        value = li.get_text(strip=True)
        info[key] = value

    # DOB and age
    dob = parse_dob(info.get("dob"))
    age = calc_age(dob)

    # Division — pulled from most recent fight in fight history
    division = extract_division(soup)
    sex = derive_sex(division)

    fighter = {
        "name": name,
        "nickname": nickname or None,
        "division": division,
        "sex": sex,
        "wins": wins,
        "losses": losses,
        "draws": draws,
        "height_in": parse_height(info.get("height")),
        "reach_in": parse_reach(info.get("reach")),
        "stance": info.get("stance") if info.get("stance") and info.get("stance") != "--" else None,
        "dob": dob.isoformat() if dob else None,
        "age": age,
        "slpm": parse_num(info.get("slpm")),
        "str_acc": parse_pct(info.get("str. acc.")),
        "sapm": parse_num(info.get("sapm")),
        "str_def": parse_pct(info.get("str. def")),
        "td_avg": parse_num(info.get("td avg.")),
        "td_acc": parse_pct(info.get("td acc.")),
        "td_def": parse_pct(info.get("td def.")),
        "sub_avg": parse_num(info.get("sub. avg.")),
        "ufc_url": url,
    }
    return fighter

def upsert_fighter(fighter):
    """Insert or update a fighter in Supabase, keyed on ufc_url.

    Strips keys with None values before sending — a failed extraction
    (e.g. division=None when the ufcstats page omits a weight-class column)
    must NOT overwrite a previously-correct value. Without this filter,
    a scraper re-run will null out every field the current page can't
    re-derive."""
    payload = {k: v for k, v in fighter.items() if v is not None}
    if not payload.get("ufc_url"):
        return False
    try:
        supabase.table("fighters").upsert(payload, on_conflict="ufc_url").execute()
        return True
    except Exception as e:
        print(f"  ! Supabase error for {fighter.get('name')}: {e}")
        return False

# --- Main ---
def main():
    print("=== CageMetrics Scraper ===")
    print("Step 1: Collecting fighter URLs...")
    urls = get_fighter_urls()
    print(f"Found {len(urls)} fighters.")
    if not urls:
        # The roster is never actually empty — zero URLs means the scrape is
        # broken (blocked, markup change, ...). Exit non-zero so the scheduled
        # run shows as failed instead of quietly doing nothing.
        raise SystemExit("No fighter URLs found — scrape is broken, refusing to exit green")

    print("Step 2: Scraping fighter profiles...")
    successes = failures = 0
    for i, url in enumerate(urls, 1):
        print(f"[{i}/{len(urls)}] {url}")
        fighter = parse_fighter(url)
        if fighter and fighter.get("name"):
            if upsert_fighter(fighter):
                successes += 1
            else:
                failures += 1
        else:
            failures += 1
        if i % 50 == 0:
            print(f"  Progress: {successes} ok, {failures} failed")

    print(f"\n=== Done. {successes} fighters saved, {failures} failures. ===")
    if successes == 0:
        raise SystemExit("0 fighters saved — run failed, refusing to exit green")

if __name__ == "__main__":
    main()
