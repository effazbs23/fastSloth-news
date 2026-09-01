# fastSloth — News Monitoring & Auto-Posting Pipeline

Serverless, free-tier pipeline: scrapes news sites daily, extracts structured
story data with Groq (`openai/gpt-oss-120b`), renders branded PNG cards,
posts them to Facebook/Instagram/X, and logs telemetry a Next.js dashboard
reads.

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
  → fetch article text → Groq structured extraction → Playwright renders 4
  branded PNG cards (1080x1080, logo/date/location on every card - see
  Brand template below) → archive every card to Supabase Storage → publish to
  Meta/X for up to `MAX_SOCIAL_POSTS_PER_RUN` stories → insert each story
  tagged with the run's `cron_log_id` → update the `cron_logs` row with final
  status/counts. Runs once daily, so scanning the full listing (not stopping
  at the first match) is what covers "the last 24 hours" — no per-article
  publish-date parsing needed since dedup on `news_items.url` handles re-runs.
  Processing is inherently serial (one story fully finishes - extract, render,
  archive, publish, insert - before the next starts), which is what "queued"
  photocard generation meant in practice; no separate queue infra was added.
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

## Brand template

`render_image_cards()` in `main_pipeline.py` renders **one** card per story
(not a 4-slide carousel - that was the original design, changed
2026-09-01): a logo badge top-left, the news (`context`) centered in bold
type over a subtle gradient tint, a small red accent rule above the
headline, and a `📍 location` pill in the bottom-left corner. No date, no
source/vendor attribution anywhere on the card or in the social caption -
removed deliberately, not an oversight.

- `BRAND_LOGO_PATH` = `assets/logo.png` (the real fastSloth News logo, resized
  from the 1536x1024/1MB original to 600x400/139KB - it renders at 110px
  tall, no reason to base64-embed the full-res file into every card). Sits
  inside its own white rounded "badge" with a soft shadow rather than placed
  directly on the gradient background: the logo's PNG has a white
  background itself, and `mix-blend-mode: multiply` (the usual fix for
  this) turned out not to reach through the content wrapper's `z-index`
  stacking context, so it stayed a visibly mismatched box until wrapped in
  a deliberate badge instead - simpler and confirmed working, where the
  blend-mode approach was confirmed *not* working, both by actually
  rendering and looking at the PNG rather than reasoning about the CSS.
  Falls back to a plain text label if the logo file is ever missing.
- `BRAND_BG_COLOR` (`#ffffff`), `BRAND_ACCENT_COLOR` (`#f03018`),
  `BRAND_TEXT_COLOR` (`#101018`) are sampled straight from `assets/logo.png`
  (clustered the most common non-background pixel colors), not eyeballed.
- `BRAND_FONT_FAMILY` = `'Inter','Hind Siliguri',sans-serif`, loaded from
  Google Fonts at render time. Inter for crisp modern Latin type; Hind
  Siliguri so Bengali headlines (most of what this pipeline actually
  extracts) render as real glyphs instead of tofu boxes or a generic
  fallback - Chromium picks whichever font in the stack has each character.
  Confirmed by rendering a card with real Bengali `context` text, not
  assumed.
- Card size is 1080x1080 (`viewport` in `render_image_cards()`) - change if a
  different aspect ratio is needed (e.g. 1080x1350 portrait, 1080x1920 Story).
- **Background art**: `generate_background_image()` optionally calls a
  deployed `saurav-z/free-image-generation-api` Cloudflare Worker (or
  anything with the same `{"prompt": ...} -> image bytes` shape) for a
  low-opacity (0.16) backdrop image behind the text, built from a prompt
  derived from `context`. This is the one place that AI image generator
  actually fits: since it only ever sits at low opacity behind the precise
  HTML/CSS text layer, its imprecision on exact colors/logo/text doesn't
  matter here the way it would for the rest of the card (see Incidents).
  Needs `IMAGE_GEN_API_URL` (the deployed Worker's URL) and
  `IMAGE_GEN_API_KEY` - without them, cards just render on the plain brand
  background, no error.

I evaluated `saurav-z/free-image-generation-api` (a Cloudflare Workers /
Stable Diffusion XL text-to-image API) for this and deliberately did not use
it: it has no way to guarantee exact logo placement, exact brand colors, or
legible text in the output - especially not the Bengali headlines this
pipeline actually extracts. HTML/CSS→PNG via Playwright is deterministic and
was already in place, so it's what got extended instead.

## Incidents

- **2026-09-01 — consolidated to one card per story; fixed a bug this
  introduced.** Instagram's Graph API requires 2+ children for a `CAROUSEL`
  media post; `publish_to_meta()` always built one regardless of image
  count, which was fine when every story rendered 4 cards but would 400 now
  that it's always exactly 1. Fixed by posting a single image directly
  (`image_url` + `caption`, no `media_type`/`is_carousel_item`) when there's
  only one URL, keeping the carousel path for if this ever goes back to
  multi-image. Caught by reading the actual Graph API behavior, not
  assumed - this is the kind of thing that would've only surfaced as a
  confusing 400 in production otherwise.

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
  Fixed independently (identical patch) by effazrayhan in `617fbd2` around
  the same time this was diagnosed here.

- **2026-09-01 — Gemini `403 PERMISSION_DENIED: Your project has been denied
  access`.** Not a code bug - this is Google restricting the API key's
  project, a currently-widespread free-tier issue per
  [Google's own developer forum](https://discuss.ai.google.dev/t/403-permission-denied-project-denied-access/177102)
  (AI Studio shows the project as "Restricted" / "Billing Tier: Unavailable"
  even though `models.list` still returns 200). Options, in order of least
  to most effort: check the project's status at
  [aistudio.google.com](https://aistudio.google.com) → API keys; generate a
  fresh key under a **different/new** Google Cloud project (the restriction
  is project-scoped, and this is the most commonly reported fix on the
  forum); or enable billing on the project (Gemini's free quota still
  applies on a billing-enabled project, and those get flagged less often).
  While investigating this, also found and fixed a separate robustness gap:
  the per-provider `try/except` wrapped the *entire* scrape loop, so one
  story's extraction failure (like this one) silently skipped every other
  candidate for that provider too, not just the failing story. Moved to a
  per-story `try/except` inside the loop.

- **2026-09-01 — switched AI extraction from Gemini to Groq entirely**, given
  how much churn the Gemini incidents above already were (dead SDK, dead
  model, then the account-restriction wall). `parse_story_with_ai()` now
  POSTs straight to Groq's OpenAI-compatible
  `https://api.groq.com/openai/v1/chat/completions` with
  `response_format: {"type": "json_object"}` — plain `requests` (already a
  dependency), no new SDK. `google-genai` dropped from `requirements.txt`.
  Model is `openai/gpt-oss-120b`; picked after listing live models with
  `GET /openai/v1/models` (no Llama models were on the account at all -
  Groq's hosted lineup has clearly moved on since training-data-era
  assumptions would suggest, so don't trust a remembered model id here
  either, re-check live if this needs revisiting) and confirming the
  120b/20b free-tier rate limits are identical, so there's no cost to
  picking the larger, better-quality model. One real quirk found via a live
  test call: gpt-oss sometimes returns a field (e.g. `issues`) as a JSON
  array even when told to return a plain string - `parse_story_with_ai()`
  normalizes every field to a string before it reaches the DB, since
  `news_items` columns are `TEXT` and psycopg2 would otherwise adapt a
  Python list into a Postgres array literal instead of erroring loudly.

- **2026-09-01 — added a branded template (logo/date/location) and moved
  image storage off the GitHub-commit workaround.** User didn't have access
  to Google Cloud Console (needed for any Drive service account), so instead
  of Drive: reused the *existing* Supabase project (already the DB) and
  enabled Storage on it - no new signup, no console, one `service_role` key.
  `upload_image_public()` (the GitHub Contents API commit hack) is gone;
  `upload_to_storage()`/`archive_cards_to_storage()` upload every card to a
  Supabase Storage bucket instead, unconditionally (not gated by the social
  post cap, since archiving and social publishing are separate concerns now).
  Instagram's Graph API `image_url` now points at the Supabase public URL
  instead of `raw.githubusercontent.com`. This also resolves the "image
  hosting" ceiling below - the git repo no longer grows with every story.
  `permissions: contents: write` and `GITHUB_TOKEN` dropped from the workflow
  since nothing writes to the repo from the pipeline anymore.

## Known ceilings (deliberate, not oversights)
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
  `DATABASE_URL`/`GROQ_API_KEY` configured.
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
| `GROQ_API_KEY` | yes | console.groq.com, free tier |
| `SUPABASE_URL` | for storage/IG | e.g. `https://xxxxx.supabase.co` — same project as `DATABASE_URL` |
| `SUPABASE_SERVICE_ROLE_KEY` | for storage/IG | Settings → API in the Supabase dashboard |
| `SUPABASE_STORAGE_BUCKET` | optional | defaults to `photocards` — create a **public** bucket with this name in Storage |
| `IMAGE_GEN_API_URL` | optional | a deployed `saurav-z/free-image-generation-api` Worker URL, for the low-opacity card backdrop |
| `IMAGE_GEN_API_KEY` | optional | that Worker's `API_KEY` |
| `META_ACCESS_TOKEN` | optional | long-lived Page token |
| `META_PAGE_ID` | optional | Facebook Page id |
| `META_IG_USER_ID` | optional | linked IG business account id |
| `X_CONSUMER_KEY` / `X_CONSUMER_SECRET` | optional | X app keys |
| `X_ACCESS_TOKEN` / `X_ACCESS_SECRET` | optional | user-context token, read+write |

Without `SUPABASE_URL`/`SUPABASE_SERVICE_ROLE_KEY` the pipeline still runs
fine (extraction, dashboard, X posting are unaffected) — it just skips
archiving cards, and skips Meta publishing entirely (both Facebook and
Instagram need a public `image_url` from Storage now that the GitHub-commit
workaround is gone).

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
- `assets/logo.png` — brand logo (fastSloth News, resized to 600x400)
- `requirements.txt` — requests, beautifulsoup4, psycopg2-binary, playwright
- `schema.sql` — the two tables, plus `news_items.cron_log_id`
- `.github/workflows/pipeline.yml` — cron + secrets wiring
- `app/api/telemetry/route.js` — status + latest-batch data
- `app/api/refresh/route.js` — triggers a pipeline run on demand
- `app/page.js`, `app/layout.js` — dashboard (Telemetry tab + Kanban tab)
