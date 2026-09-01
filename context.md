# fastSloth — News Monitoring & Auto-Posting Pipeline

Serverless, free-tier pipeline: scrapes news sites hourly, extracts structured
story data with Gemini, renders branded PNG cards, posts them to
Facebook/Instagram/X, and logs telemetry a Next.js dashboard reads.

## Architecture

- **Trigger**: GitHub Actions cron, `17 * * * *` (hourly, off the top of the
  hour to dodge GitHub's queue spikes). `.github/workflows/pipeline.yml`.
- **Pipeline**: `main_pipeline.py` — fetch provider pages → dedupe against
  `news_items.url` → fetch full article text → Gemini 1.5 Flash structured
  extraction → Playwright renders 4 PNG cards (1080x1080) → publish to
  Meta/X → insert row → always write one `cron_logs` row per run.
- **DB**: Postgres (Supabase/Neon), pooled connection on port 6543,
  `sslmode=require`. Schema in `schema.sql`. Run it once against the DB.
- **Dashboard**: Next.js App Router on Vercel. `app/api/telemetry/route.js`
  queries `cron_logs` + `news_items`; `app/page.js` renders it.

## Known ceilings (deliberate, not oversights)

- **Image hosting**: Instagram's Graph API needs a public `image_url`, not
  raw bytes, so generated cards get committed into `public/cards/` via the
  GitHub Contents API (`GITHUB_TOKEN`, auto-provided in Actions) and served
  from `raw.githubusercontent.com`. Free and simple, but the repo grows
  ~1-2 images per new story forever. If story volume grows, swap this for a
  Supabase Storage bucket (also free tier) instead of committing to git.
- **X auth**: no `tweepy`/`requests-oauthlib` — OAuth 1.0a is hand-signed
  with stdlib `hmac`/`hashlib` in `_oauth1_header()` to keep
  `requirements.txt` to exactly the libraries specified. Needs *user-context*
  keys (`X_ACCESS_TOKEN`/`X_ACCESS_SECRET` from an app with read+write
  permission), not just an app-only bearer token.
- **1 new story per provider per run**: the scrape loop `break`s after the
  first unprocessed link per channel, so a backlog drains one story/provider
  per hour rather than all at once.
- Social publishing functions no-op (with a log line) when their platform's
  secrets aren't set, so the pipeline stays useful with only
  `DATABASE_URL`/`GEMINI_API_KEY` configured.

## Setup

### 1. Database
Run `schema.sql` against a free Supabase or Neon Postgres project (their SQL
editor works). Copy the **pooled** connection string (port 6543) — percent-encode
any special characters in the password (`[`→`%5B`, `]`→`%5D`, `@`→`%40`,
`#`→`%23`, `/`→`%2F`) or the URL won't parse.

### 2. GitHub repo secrets
Settings → Secrets and variables → Actions → New repository secret:

| Secret | Required | Notes |
|---|---|---|
| `DATABASE_URL` | yes | pooled, `sslmode=require` |
| `GEMINI_API_KEY` | yes | Google AI Studio, free tier |
| `META_ACCESS_TOKEN` | optional | long-lived Page token |
| `META_PAGE_ID` | optional | Facebook Page id |
| `META_IG_USER_ID` | optional | linked IG business account id |
| `X_CONSUMER_KEY` / `X_CONSUMER_SECRET` | optional | X app keys |
| `X_ACCESS_TOKEN` / `X_ACCESS_SECRET` | optional | user-context token, read+write |

`GITHUB_TOKEN` is injected automatically by Actions — nothing to add, but the
workflow needs `permissions: contents: write` (already set) so it can commit
card images.

Workflow runs on the hourly cron automatically, or trigger it manually from
the Actions tab (`workflow_dispatch` is enabled).

### 3. Dashboard on Vercel
- Import this repo into Vercel.
- Add env var `DATABASE_URL` (same pooled connection string) in the Vercel
  project settings.
- Deploy — Next.js/Tailwind build via `package.json` needs no extra config.

## Files

- `main_pipeline.py` — the pipeline
- `requirements.txt` — requests, beautifulsoup4, google-generativeai, psycopg2-binary, playwright
- `schema.sql` — the two tables
- `.github/workflows/pipeline.yml` — cron + secrets wiring
- `app/api/telemetry/route.js`, `app/page.js`, `app/layout.js` — dashboard
