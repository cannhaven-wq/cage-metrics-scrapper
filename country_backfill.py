"""
Fill fighters.country / country_code from country of origin.

Primary source is the "Place of Birth" field on ufc.com athlete pages, which
covers ~95% of the active roster on a plain name->slug guess. Wikidata's
country-of-citizenship (P27) fills in the rest; it only covers ~60% on its own,
so it is a fallback rather than the source.

Deliberately does NOT infer anything from events.location. Where a fight is
held says nothing about where a fighter is from.

Usage:
    python country_backfill.py                 # active roster (fought since 2024)
    python country_backfill.py --all           # every fighter in the table
    python country_backfill.py --limit 50      # small test run
    python country_backfill.py --recheck       # re-look-up fighters we already missed
"""

import argparse
import csv
import html as htmllib
import io
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_SECRET_KEY = os.environ.get("SUPABASE_SECRET_KEY")
RATE_LIMIT_SECONDS = 0.8
MAX_ATTEMPTS = 3
ACTIVE_SINCE = "2024-01-01"

# ufc.com sits behind a bot check that 403s the default urllib User-Agent.
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

# Country name -> ISO 3166-1 alpha-2, keyed on the spellings ufc.com and
# Wikidata actually print (including the ones where they disagree).
COUNTRY_CODES = {
    "united states": "US", "usa": "US", "united states of america": "US",
    "brazil": "BR", "canada": "CA", "mexico": "MX", "russia": "RU",
    "russian federation": "RU", "united kingdom": "GB", "england": "GB",
    "scotland": "GB", "wales": "GB", "northern ireland": "GB",
    "great britain": "GB", "ireland": "IE", "australia": "AU",
    "new zealand": "NZ", "china": "CN", "people's republic of china": "CN",
    "japan": "JP", "south korea": "KR", "republic of korea": "KR",
    "north korea": "KP", "france": "FR", "germany": "DE", "spain": "ES",
    "italy": "IT", "netherlands": "NL", "the netherlands": "NL",
    "belgium": "BE", "sweden": "SE", "norway": "NO", "denmark": "DK",
    "finland": "FI", "iceland": "IS", "poland": "PL", "czech republic": "CZ",
    "czechia": "CZ", "slovakia": "SK", "austria": "AT", "switzerland": "CH",
    "portugal": "PT", "greece": "GR", "croatia": "HR", "serbia": "RS",
    "bosnia and herzegovina": "BA", "slovenia": "SI", "hungary": "HU",
    "romania": "RO", "bulgaria": "BG", "ukraine": "UA", "belarus": "BY",
    "moldova": "MD", "lithuania": "LT", "latvia": "LV", "estonia": "EE",
    "georgia": "GE", "armenia": "AM", "azerbaijan": "AZ", "kazakhstan": "KZ",
    "uzbekistan": "UZ", "kyrgyzstan": "KG", "kyrgyz republic": "KG",
    "tajikistan": "TJ", "turkmenistan": "TM", "turkey": "TR", "iran": "IR",
    "iraq": "IQ", "israel": "IL", "lebanon": "LB", "jordan": "JO",
    "syria": "SY", "saudi arabia": "SA", "united arab emirates": "AE",
    "bahrain": "BH", "kuwait": "KW", "qatar": "QA", "oman": "OM",
    "yemen": "YE", "afghanistan": "AF", "pakistan": "PK", "india": "IN",
    "nepal": "NP", "bangladesh": "BD", "sri lanka": "LK", "thailand": "TH",
    "vietnam": "VN", "philippines": "PH", "indonesia": "ID",
    "malaysia": "MY", "singapore": "SG", "myanmar": "MM", "cambodia": "KH",
    "laos": "LA", "mongolia": "MN",
    "argentina": "AR", "chile": "CL", "peru": "PE", "colombia": "CO",
    "venezuela": "VE", "ecuador": "EC", "bolivia": "BO", "uruguay": "UY",
    "paraguay": "PY", "panama": "PA", "costa rica": "CR", "guatemala": "GT",
    "honduras": "HN", "nicaragua": "NI", "el salvador": "SV", "cuba": "CU",
    "dominican republic": "DO", "puerto rico": "PR", "jamaica": "JM",
    "trinidad and tobago": "TT", "haiti": "HT", "bahamas": "BS",
    "suriname": "SR", "guyana": "GY", "belize": "BZ",
    "nigeria": "NG", "ghana": "GH", "cameroon": "CM", "south africa": "ZA",
    "kenya": "KE", "morocco": "MA", "algeria": "DZ", "tunisia": "TN",
    "egypt": "EG", "senegal": "SN", "angola": "AO", "congo": "CG",
    "democratic republic of the congo": "CD", "ivory coast": "CI",
    "mali": "ML", "sudan": "SD", "ethiopia": "ET", "tanzania": "TZ",
    "uganda": "UG", "zimbabwe": "ZW", "mozambique": "MZ",
    "guam": "GU", "samoa": "WS", "american samoa": "AS", "fiji": "FJ",
    "tonga": "TO", "papua new guinea": "PG", "cape verde": "CV",
    "dagestan": "RU", "chechnya": "RU",  # sometimes printed instead of Russia
    "canary islands": "ES", "balearic islands": "ES",  # ufc.com prints the region
    "cyprus": "CY", "hong kong": "HK", "macau": "MO", "taiwan": "TW",
    "turkiye": "TR", "north macedonia": "MK", "macedonia": "MK",
    "montenegro": "ME", "albania": "AL", "kosovo": "XK", "malta": "MT",
    "luxembourg": "LU", "monaco": "MC", "andorra": "AD",
    "anguilla": "AI", "solomon islands": "SB", "cabo verde": "CV",
    "korea": "KR",  # ufc.com prints this for South Korean fighters
}

# Wikidata prints a handful of these differently again.
COUNTRY_ALIASES = {
    "people's republic of china": "china",
    "kingdom of the netherlands": "netherlands",
    "united states of america": "united states",
    "republic of ireland": "ireland",
    "soviet union": "russia",
    "yugoslavia": "serbia",
    "czechoslovakia": "czech republic",
}

# Canonical spelling to store, so ufc.com and Wikidata rows agree in the table.
CANONICAL_NAME = {
    "US": "United States", "GB": "United Kingdom", "RU": "Russia",
    "CN": "China", "KR": "South Korea", "NL": "Netherlands",
    "CZ": "Czech Republic", "IE": "Ireland", "KG": "Kyrgyzstan",
    "CI": "Ivory Coast",
}


def log(msg):
    print(msg, flush=True)


def strip_accents(s):
    s = unicodedata.normalize("NFKD", s or "")
    return "".join(c for c in s if not unicodedata.combining(c))


def name_key(s):
    return " ".join(re.sub(r"[^a-z ]", " ", strip_accents(s).lower()).split())


def normalise_country(raw):
    """'Oakland, United States' -> ('United States', 'US')."""
    if not raw:
        return None, None
    tail = raw.split(",")[-1].strip()
    key = strip_accents(tail).lower().strip(" .")
    key = re.sub(r"\s*&\s*", " and ", key)  # "Bosnia & Herzegovina"
    key = COUNTRY_ALIASES.get(key, key)
    code = COUNTRY_CODES.get(key)
    if not code:
        return tail, None
    return CANONICAL_NAME.get(code, tail), code


def slug_variants(name):
    """ufc.com slugs are the name lowercased and hyphenated, mostly."""
    base = strip_accents(name).lower()
    v1 = re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", base)).strip("-")
    # "Casey O'Neill" -> casey-o-neill from v1, but the real page is casey-oneill.
    v2 = re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", base.replace("'", ""))).strip("-")
    return [v1] if v2 == v1 else [v1, v2]


def fetch(url, timeout=25):
    req = urllib.request.Request(url, headers={"User-Agent": BROWSER_UA})
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            return urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if attempt == MAX_ATTEMPTS:
                return None
            time.sleep(2 * attempt)
        except Exception:
            if attempt == MAX_ATTEMPTS:
                return None
            time.sleep(2 * attempt)
    return None


def ufc_birthplace(name):
    for slug in slug_variants(name):
        html = fetch("https://www.ufc.com/athlete/" + slug)
        time.sleep(RATE_LIMIT_SECONDS)
        if not html:
            continue
        m = re.search(r"Place of Birth</div>\s*<div[^>]*>(.*?)</div>", html, re.S | re.I)
        if m:
            val = re.sub(r"<[^>]+>", "", m.group(1))
            for _ in range(3):
                if "&" not in val:
                    break
                val = htmllib.unescape(val)
            val = re.sub(r"\s+", " ", val).strip()
            if val:
                return val
    return None


def load_wikidata():
    """One bulk query, used only for fighters ufc.com could not resolve."""
    q = """SELECT ?n ?countryLabel WHERE {
      { ?p wdt:P641 wd:Q114466 } UNION { ?p wdt:P106 wd:Q13474373 }
      ?p wdt:P27 ?country .
      { ?p rdfs:label ?n } UNION { ?p skos:altLabel ?n }
      FILTER(lang(?n)="en")
      SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }
    }"""
    url = "https://query.wikidata.org/sparql?" + urllib.parse.urlencode({"query": q})
    req = urllib.request.Request(url, headers={
        "Accept": "text/csv",
        "User-Agent": "CFL-fighter-country/1.0 (cannhaven@gmail.com)",
    })
    try:
        data = urllib.request.urlopen(req, timeout=180).read().decode("utf-8", "replace")
    except Exception as e:
        log("  wikidata fallback unavailable (%s) - continuing without it" % type(e).__name__)
        return {}
    table = {}
    for row in csv.DictReader(io.StringIO(data)):
        key = name_key(row["n"])
        if not key:
            continue
        table.setdefault(key, row["countryLabel"])
        parts = key.split()
        if len(parts) == 2:  # "Yan Xiaonan" vs "Xiaonan Yan"
            table.setdefault(" ".join(parts[::-1]), row["countryLabel"])
    return table


def sb(path, method="GET", body=None, extra_headers=None):
    headers = {
        "apikey": SUPABASE_SECRET_KEY,
        "Authorization": "Bearer " + SUPABASE_SECRET_KEY,
        "Content-Type": "application/json",
    }
    headers.update(extra_headers or {})
    req = urllib.request.Request(
        SUPABASE_URL + "/rest/v1/" + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers=headers)
    raw = urllib.request.urlopen(req, timeout=60).read().decode("utf-8")
    return json.loads(raw) if raw.strip() else []


def load_upcoming_fighter_ids():
    """Fighters booked on a card that has not happened yet."""
    today = datetime.now(timezone.utc).date().isoformat()
    events = sb("events?select=id&event_date=gte." + today + "&limit=40")
    if not events:
        return []
    ids = ",".join(str(e["id"]) for e in events)
    fights = sb("fights?select=fighter_a_id,fighter_b_id&event_id=in.(%s)&limit=1000" % ids)
    return sorted({x for f in fights for x in (f["fighter_a_id"], f["fighter_b_id"]) if x})


def load_fighters(all_fighters, recheck, limit, missing=False, upcoming=False):
    if upcoming:
        ids = load_upcoming_fighter_ids()
        if not ids:
            return []
        rows = sb("fighters?select=id,name,country,country_checked_at&id=in.(%s)&limit=1000"
                  % ",".join(map(str, ids)))
        if not recheck:
            rows = [r for r in rows if not r.get("country_checked_at")]
        return rows[:limit] if limit else rows

    rows, offset = [], 0
    flt = "" if all_fighters else "&last_fight_date=gte." + ACTIVE_SINCE
    if missing:
        flt += "&country_code=is.null"
    elif not recheck:
        flt += "&country_checked_at=is.null"
    while True:
        page = sb("fighters?select=id,name,country,country_checked_at"
                  "&order=last_fight_date.desc.nullslast&limit=1000&offset=%d%s"
                  % (offset, flt))
        if not page:
            break
        rows += page
        offset += 1000
        if limit and len(rows) >= limit:
            break
    return rows[:limit] if limit else rows


def remap():
    """Recompute country/country_code from birth_place already on record."""
    rows, offset, fixed, still = [], 0, 0, []
    while True:
        page = sb("fighters?select=id,name,birth_place&birth_place=not.is.null"
                  "&country_code=is.null&order=id&limit=1000&offset=%d" % offset)
        if not page:
            break
        rows += page
        offset += 1000
    log("%d fighters have a birthplace but no ISO code" % len(rows))
    for r in rows:
        country, code = normalise_country(r["birth_place"])
        if not code:
            still.append(r["birth_place"])
            continue
        sb("fighters?id=eq." + str(r["id"]), method="PATCH",
           body={"country": country, "country_code": code},
           extra_headers={"Prefer": "return=minimal"})
        fixed += 1
    log("remapped %d" % fixed)
    if still:
        log("still unmapped: %s" % ", ".join(sorted(set(still))))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="every fighter, not just the active roster")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--recheck", action="store_true", help="retry fighters already looked up")
    ap.add_argument("--missing", action="store_true",
                    help="only fighters with no country_code yet (re-run after adding ISO codes)")
    ap.add_argument("--remap", action="store_true",
                    help="re-derive country/country_code from the stored birth_place, "
                         "no network — use after adding entries to COUNTRY_CODES")
    ap.add_argument("--upcoming", action="store_true",
                    help="only fighters booked on an upcoming card (catches debutants, "
                         "who have no last_fight_date and so miss the active-roster filter)")
    args = ap.parse_args()

    if not SUPABASE_URL or not SUPABASE_SECRET_KEY:
        sys.exit("SUPABASE_URL / SUPABASE_SECRET_KEY not set.")

    if args.remap:
        remap()
        return

    fighters = load_fighters(args.all, args.recheck, args.limit, args.missing, args.upcoming)
    log("%d fighters to look up" % len(fighters))
    if not fighters:
        return

    wiki = load_wikidata()
    log("wikidata fallback: %d names" % len(wiki))

    stats = {"ufc.com": 0, "wikidata": 0, "miss": 0}
    no_code = []
    for i, f in enumerate(fighters, 1):
        raw, src = ufc_birthplace(f["name"]), "ufc.com"
        if not raw:
            hit = wiki.get(name_key(f["name"]))
            if hit:
                raw, src = hit, "wikidata"
        country, code = normalise_country(raw)
        sb("fighters?id=eq." + str(f["id"]), method="PATCH",
           body={
               "birth_place": raw,
               "country": country,
               "country_code": code,
               "country_src": src if raw else None,
               "country_checked_at": datetime.now(timezone.utc).isoformat(),
           },
           extra_headers={"Prefer": "return=minimal"})

        if raw and code:
            stats[src] += 1
        elif raw:
            no_code.append(raw)
        else:
            stats["miss"] += 1
        if i % 25 == 0 or i == len(fighters):
            log("  %d/%d - ufc %d, wikidata %d, missed %d"
                % (i, len(fighters), stats["ufc.com"], stats["wikidata"], stats["miss"]))

    log("\ndone: %d from ufc.com, %d from wikidata, %d unresolved"
        % (stats["ufc.com"], stats["wikidata"], stats["miss"]))
    if no_code:
        log("country with no ISO code (add to COUNTRY_CODES): %s"
            % ", ".join(sorted(set(no_code))))


if __name__ == "__main__":
    main()
