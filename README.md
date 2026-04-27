# CageMetrics UFC Stats Scraper

Scrapes fighter data from ufcstats.com and pushes to a Supabase database.

## Environment Variables Required

- `SUPABASE_URL` — your Supabase project URL
- `SUPABASE_SECRET_KEY` — your Supabase secret key (NOT the publishable one)

## How It Runs

- Paginates through every letter (a-z) on ufcstats.com fighter list
- Visits each fighter profile and extracts stats
- Upserts to the `fighters` table in Supabase (using `ufc_url` as the unique key)
- Rate-limited to 1.5 seconds between requests

## Local Run (optional)

```bash
pip install -r requirements.txt
export SUPABASE_URL="https://xxx.supabase.co"
export SUPABASE_SECRET_KEY="sb_secret_..."
python scraper.py
```

## Deployment

Deployed on Railway with a scheduled cron job to run nightly.
