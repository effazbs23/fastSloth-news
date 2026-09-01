# fastSloth — News Monitoring & Auto-Posting Pipeline

Serverless, free-tier pipeline: scrapes news sites daily, extracts structured
story data with Gemini, renders branded PNG cards, posts them to
Facebook/Instagram/X, and logs telemetry a Next.js dashboard reads.

## Architecture

- **Trigger**: GitHub Actions cron, `17 3 * * *` (once daily at 03:17 UTC),
  plus `workflow_dispatch` — fired both manually from the Actions tab and by
  the dashboard's **Refresh** button. `.github/workflows/pipeline.yml`.
- **Providers**: Star News BD, The Daily Star, Ittefaq, bdnews24 (via its
  Bangla subdomain, see below), Daily Campus (`NEWS_CHANNELS` in
  `main_pipeline.py`). Excluded, deliberately:
  - **Jamuna TV, Kalerkantho** — sit behind Cloudflare (JS challenge or WAF
    block on every path tried, including `/feed`/`/sitemap.xml`) that a plain
    `requests` scraper can't pass; would need a real challenge-solving
    browser, out of scope for this stack.
  - **Amardesh** — not itself a news publisher: its homepage is a link
    directory to *other* outlets' homepages (Kalerkantho, Bangladesh
    Protidin, ...) and static reference pages (bank lists, flight
    schedules), not original articles. Nothing there to extract.
  - **bdnews24's main domain** (`bdnews24.com`) is also Cloudflare-blocked,
    but its Bangla edition (`bangla.bdnews24.com`) isn't and carries the same
    stories, so that subdomain is what's actually fetched.
- **Pipeline**: `main_pipeline.py` — insert a `RUNNING` `cron_logs` row to get
  a run id → for each provider, scan the whole homepage listing (not just the
  first link) for unprocessed article URLs, up to `MAX_STORIES_PER_PROVIDER`
  → fetch article text → Gemini structured extraction → Playwright renders 4
  PNG cards (1080x1080) → publish to Meta/X for up to `MAX_SOCIAL_POSTS_PER_RUN`
  stories → insert each story tagged with the run's `cron_log_id` → update
  the `cron_logs` row with final status/counts. Runs once daily, so scanning
  the full listing (not stopping at the first match) is what covers "the last
  24 hours" — no per-article publish-date parsing needed since dedup on
  `news_items.url` handles re-runs.
- **DB**: Postgres (Supabase/Neon), pooled connection on port 6543,
  `sslmode=require`. Schema in `schema.sql`, safe to re-run (uses
  `IF NOT EXISTS`/idempotent `ALTER`) — re-run it against your existing DB
  after pulling the `cron_log_id` column addition.
- **Dashboard**: Next.js App Router on Vercel.
  - `app/api/telemetry/route.js` — `cron_logs` + `news_items`, plus
    `latestBatch` (the most recent non-`RUNNING` run's stories, joined on
    `cron_log_id`). Marked `force-dynamic` so it's never statically cached.
  - `app/api/refresh/route.js` — POSTs a `workflow_dispatch` to the GitHub
    Actions API to run the pipeline on demand.
  - `app/page.js` — two tabs: **Telemetry** (existing status/history view)
    and **Last Fetched News** (Kanban board, one column per provider, cards
    show title/location/context/accused_victim/issues — sourced from
    `latestBatch`, so it always reflects exactly one run's output). The
    **Refresh** button calls `/api/refresh`, then polls `/api/telemetry`
    every 5s (up to 3 min) until a new completed run shows up.

## Incidents

- **2026-09-01 — zero stories extracted, both providers.** Root causes:
  1. `google-generativeai` (sunset 2025-08-31) + `gemini-1.5-flash` (fully
     shut down 2025-09-24, 404s on every call) — every extraction silently
     failed inside the per-provider `try/except`, so nothing ever reached the
     DB even though the workflow exited green. Migrated to the `google-genai`
     SDK (`from google import genai`, `genai.Client(...).models.generate_content(...)`)
     on `gemini-3.5-flash`. **`gemini-2.5-flash` is scheduled to shut down
     2026-10-16** (and has been intermittently 404ing even earlier) — if
     `gemini-3.5-flash` itself starts erroring, bump `GEMINI_MODEL` in
     `main_pipeline.py` to whatever Google's current stable Flash model is.
  2. Daily Star's article links are *relative* (`/sports/football/news/<slug>-<id>`),
     but the old filter only matched absolute URLs containing `/news/` right
     after the domain — 0 candidates, always. Fixed by resolving every href
     with `urljoin()` and matching on a numeric article-id path segment
     instead (also tightened on Star News BD to stop matching `/tag/...`
     pages as if they were articles).

- **2026-09-01 — zero stories extracted, all providers, after the above fix
  too.** The `getaddrinfo` IPv4 monkeypatch at the top of `main_pipeline.py`
  (there since the original scaffold, to work around a GitHub Actions
  IPv6-vs-Postgres issue that turned out to not even apply to psycopg2's
  connection path) crashed every `requests.get()` call:
  `getaddrinfo() got multiple values for argument 'family'`. It did
  `kwargs['family'] = socket.AF_INET` on top of `*args`, but on this runner's
  Python/urllib3 stack `family` arrives positionally, so it collided with the
  injected kwarg — caught by the per-provider `try/except`, so every provider
  silently failed at the very first fetch, before scraping even started.
  Fixed by giving the wrapper `family`'s real positional slot and just
  hardcoding `AF_INET` in the call to the original function instead of
  forwarding whatever was passed in. Verified against a live `requests.get()`
  call, not just a compile check, since this exact class of bug (looks fine,
  breaks on the actual call convention) is what caused it in the first place.

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
- **Story/social caps per run**: `MAX_STORIES_PER_PROVIDER = 20` (flat safety
  cap on Gemini calls + GitHub commits per provider, not real rate-limiting —
  revisit if a homepage listing ever runs deeper than that in 24h) and
  `MAX_SOCIAL_POSTS_PER_RUN = 3` (stories beyond that still get extracted and
  stored/shown on the dashboard, just not posted to FB/IG/X, so a busy day
  doesn't dump a dozen posts on your socials at once).
- Social publishing functions no-op (with a log line) when their platform's
  secrets aren't set, so the pipeline stays useful with only
  `DATABASE_URL`/`GEMINI_API_KEY` configured.
- **Refresh button has no synchronous result**: it dispatches the GitHub
  Actions run and returns immediately; the actual scrape/extract/render work
  happens in Actions (Playwright and the social APIs don't run on Vercel),
  typically finishing in 1-3 minutes. The dashboard polls and updates itself
  when it's done rather than blocking the request.

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

Workflow runs on the daily cron automatically, or trigger it manually from
the Actions tab (`workflow_dispatch` is enabled) — or from the dashboard's
Refresh button, see below.

### 3. Dashboard on Vercel
- Import this repo into Vercel.
- Add env vars in the Vercel project settings:

  | Env var | Required | Notes |
  |---|---|---|
  | `DATABASE_URL` | yes | same pooled connection string as the GitHub secret |
  | `GITHUB_REPO` | for Refresh button | `"owner/repo"`, e.g. `effazbs23/fastSloth-news` |
  | `GITHUB_PAT` | for Refresh button | classic PAT with `repo` scope (or fine-grained: Actions read/write on this repo) — [github.com/settings/tokens](https://github.com/settings/tokens) |
  | `GITHUB_REF` | optional | branch to run, defaults to `master` |

  Without `GITHUB_REPO`/`GITHUB_PAT` the dashboard still works, just the
  Refresh button errors instead of triggering a run.
- Deploy — Next.js/Tailwind build via `package.json` needs no extra config.

## Files

- `main_pipeline.py` — the pipeline
- `requirements.txt` — requests, beautifulsoup4, google-genai, psycopg2-binary, playwright
- `schema.sql` — the two tables, plus `news_items.cron_log_id`
- `.github/workflows/pipeline.yml` — cron + secrets wiring
- `app/api/telemetry/route.js` — status + latest-batch data
- `app/api/refresh/route.js` — triggers a pipeline run on demand
- `app/page.js`, `app/layout.js` — dashboard (Telemetry tab + Kanban tab)
