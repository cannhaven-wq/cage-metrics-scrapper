"""
CageMetrics UFC Stats Scraper
Pulls fighter data from ufcstats.com and pushes to Supabase.
Rate-limited to be polite to ufcstats.com servers.
"""

import os
import re
import time
import requests
from bs4 import BeautifulSoup
from supabase import create_client, Client

# --- Config ---
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_SECRET_KEY = os.environ.get("SUPABASE_SECRET_KEY")
RATE_LIMIT_SECONDS = 1.5  # Time between requests
HEADERS = {
    "User-Agent": "CageMetrics/1.0 (Personal UFC stats project)"
}

# --- Supabase client ---
if not SUPABASE_URL or not SUPABASE_SECRET_KEY:
    raise SystemExit("Missing SUPABASE_URL or SUPABASE_SECRET_KEY environment variables")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_SECRET_KEY)

# --- Helpers ---
def get_soup(url):
    """Fetch a URL and return BeautifulSoup, with rate limiting."""
    time.sleep(RATE_LIMIT_SECONDS)
    try:
        r = requests.get(url, headers=HEADERS, timeout=20)
        r.raise_for_status()
        return BeautifulSoup(r.text, "html.parser")
    except Exception as e:
        print(f"  ! Error fetching {url}: {e}")
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

# --- Scraping ---
def get_fighter_urls():
    """Get every fighter URL by paginating through a-z."""
    fighter_urls = set()
    base = "http://www.ufcstats.com/statistics/fighters"
    for letter in "abcdefghijklmnopqrstuvwxyz":
        page = 1
        while True:
            url = f"{base}?char={letter}&page={page}"
            print(f"  Fetching list: {letter} page {page}")
            soup = get_soup(url)
            if not soup:
                break
            rows = soup.select("tr.b-statistics__table-row")
            page_urls = []
            for row in rows:
                a = row.find("a", class_="b-link b-link_style_black")
                if a and a.get("href"):
                    page_urls.append(a["href"])
            if not page_urls:
                break
            for u in page_urls:
                fighter_urls.add(u)
            # If we got fewer than expected results, stop paginating
            if len(page_urls) < 100:
                break
            page += 1
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
        # Get the value text (everything in li except the title)
        title.extract()
        value = li.get_text(strip=True)
        info[key] = value

    # Win method breakdown — scrape from career stats section
    ko_wins = sub_wins = dec_wins = 0
    # We won't have these by method easily — leave as 0 for now, can enhance later

    fighter = {
        "name": name,
        "nickname": nickname or None,
        "wins": wins,
        "losses": losses,
        "draws": draws,
        "height_in": parse_height(info.get("height")),
        "reach_in": parse_reach(info.get("reach")),
        "stance": info.get("stance") if info.get("stance") and info.get("stance") != "--" else None,
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
    """Insert or update a fighter in Supabase, keyed on ufc_url."""
    try:
        supabase.table("fighters").upsert(fighter, on_conflict="ufc_url").execute()
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
        # Optional: print progress every 50 fighters
        if i % 50 == 0:
            print(f"  Progress: {successes} ok, {failures} failed")

    print(f"\n=== Done. {successes} fighters saved, {failures} failures. ===")

if __name__ == "__main__":
    main()
