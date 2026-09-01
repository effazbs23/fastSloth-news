import socket

# Force IPv4 socket resolution globally - GitHub Actions runners resolve some
# managed Postgres hosts (Supabase/Neon pooler) to an IPv6 address first, which
# fails with "Network is unreachable" since the runner has no IPv6 route.
orig_getaddrinfo = socket.getaddrinfo
def getaddrinfo_ipv4(host, port, family=0, type=0, proto=0, flags=0):
    # family is force-overridden below, not forwarded - forwarding it as both
    # a positional arg (from callers like urllib3) and a kwarg raised
    # "got multiple values for argument 'family'" and broke every requests.get().
    return orig_getaddrinfo(host, port, socket.AF_INET, type, proto, flags)

socket.getaddrinfo = getaddrinfo_ipv4

import base64
import hashlib
import hmac
import json
import os
import re
import time
import urllib.parse
import uuid
from datetime import datetime
from urllib.parse import urljoin

import psycopg2
import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

# DATABASE_URL must use the pooled connection (port 6543) with sslmode=require,
# e.g. postgresql://user:pass@host:6543/postgres?sslmode=require
# Percent-encode special characters in the password (`[`->%5B, `]`->%5D, `@`->%40,
# `#`->%23, `/`->%2F) or psycopg2 will fail to parse the URL / DNS-resolve the host.
DB_URL = os.environ["DATABASE_URL"]

# Groq's chat completions API is OpenAI-compatible, so a plain requests.post()
# (already a dependency) covers it - no SDK needed.
GROQ_API_KEY = os.environ["GROQ_API_KEY"]
GROQ_MODEL = "openai/gpt-oss-120b"

# Optional - social publishing is skipped per-platform when its secrets are absent.
META_ACCESS_TOKEN = os.environ.get("META_ACCESS_TOKEN")
META_PAGE_ID = os.environ.get("META_PAGE_ID")
META_IG_USER_ID = os.environ.get("META_IG_USER_ID")

X_CONSUMER_KEY = os.environ.get("X_CONSUMER_KEY")
X_CONSUMER_SECRET = os.environ.get("X_CONSUMER_SECRET")
X_ACCESS_TOKEN = os.environ.get("X_ACCESS_TOKEN")
X_ACCESS_SECRET = os.environ.get("X_ACCESS_SECRET")

# Supabase Storage - archives every generated card, and (once configured)
# gives Instagram's Graph API the public image_url it needs to fetch a photo.
SUPABASE_URL = os.environ.get("SUPABASE_URL")  # e.g. https://xxxxx.supabase.co
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
SUPABASE_STORAGE_BUCKET = os.environ.get("SUPABASE_STORAGE_BUCKET", "photocards")

# Optional low-opacity card backdrop - real stock photography from Pexels
# (free, 200 req/hr, commercial use OK - api.pexels.com), not AI-generated.
# Cards render fine without it, just on the plain brand background.
PEXELS_API_KEY = os.environ.get("PEXELS_API_KEY")

# --- Brand template tokens, sampled from assets/logo.png (fastSloth News) ---
BRAND_LOGO_PATH = "assets/logo.png"  # falls back to a text label if this file is missing; background removed (transparent)
BRAND_BG_COLOR = "#ffffff"
BRAND_ACCENT_COLOR = "#f03018"   # red-orange from the sloth icon's gradient
BRAND_TEXT_COLOR = "#101018"     # near-black, matches the "fast"/"news" wordmark
BRAND_MUTED_COLOR = "#6b7280"    # neutral gray for the date - not in the logo itself
# Inter for crisp modern Latin type, Hind Siliguri so Bengali headlines render
# correctly instead of falling back to a generic/missing glyph font - Chromium
# picks whichever font in the stack actually has each character's glyph.
BRAND_FONT_FAMILY = "'Inter','Hind Siliguri',sans-serif"

# English-language editions only (2026-09-01: dropped Bangla-only sources).
# is_article matches on the resolved absolute URL - a numeric article-id
# segment in the path reliably tells real stories apart from nav/category links.
NEWS_CHANNELS = [
    {
        "name": "The Daily Star",
        "url": "https://www.thedailystar.net/todays-news",
        # Article links are relative (resolved via urljoin below) and don't
        # live directly under /news/, e.g. /sports/football/news/<slug>-<id>
        "is_article": re.compile(r"thedailystar\.net/.+-\d{5,}$").search,
    },
    {
        "name": "Ittefaq",
        "url": "https://en.ittefaq.com.bd/",  # English edition (Bangla is the default www.ittefaq.com.bd)
        # Links are protocol-relative (//en.ittefaq.com.bd/<id>/<slug>); urljoin below
        # resolves those to https:// same as it does Daily Star's path-relative ones.
        "is_article": re.compile(r"ittefaq\.com\.bd/\d+/").search,
    },
    # Dropped (no accessible English edition):
    # - Star News BD (starnews.com.bd) - Bangla-only, no English edition found.
    # - Daily Campus (thedailycampus.com) - Bangla-only, no English edition found.
    # - bdnews24 - does publish in English (bdnews24.com), but that domain sits
    #   behind a Cloudflare JS challenge; only their Bangla subdomain
    #   (bangla.bdnews24.com) is actually reachable by a plain requests
    #   scraper, so it doesn't qualify as an accessible English source.
    #
    # Jamuna TV (jamuna.tv), Kalerkantho (kalerkantho.com): Cloudflare-blocked
    # on every path tried (JS challenge / WAF), same issue as bdnews24 above.
    # Amardesh (amardesh.com): not a news publisher at all - a link directory
    # to other outlets' homepages and static reference pages, nothing to extract.
]


def get_db():
    return psycopg2.connect(DB_URL)


def is_processed(url):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM news_items WHERE url = %s", (url,))
    exists = cur.fetchone() is not None
    cur.close()
    conn.close()
    return exists


def start_run_log():
    """Insert a RUNNING cron_logs row up front so news_items can be tagged with it."""
    conn = get_db()
    cur = conn.cursor()
    cur.execute("INSERT INTO cron_logs (status, fetched_per_provider, total_fetched) VALUES ('RUNNING', '{}', 0) RETURNING id")
    log_id = cur.fetchone()[0]
    conn.commit()
    cur.close()
    conn.close()
    return log_id


def finish_run_log(log_id, status, provider_counts):
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "UPDATE cron_logs SET status = %s, fetched_per_provider = %s, total_fetched = %s WHERE id = %s",
        (status, json.dumps(provider_counts), sum(provider_counts.values()), log_id),
    )
    conn.commit()
    cur.close()
    conn.close()


def fetch_article_text(url, headers, max_chars=4000):
    """Fetch the article page and pull its paragraph text for AI extraction."""
    res = requests.get(url, headers=headers, timeout=10)
    soup = BeautifulSoup(res.text, "html.parser")
    paragraphs = [p.get_text(strip=True) for p in soup.find_all("p")]
    return " ".join(paragraphs)[:max_chars]


def parse_story_with_ai(title, text):
    prompt = f"""
    Analyze the news article below and return ONLY a JSON object with exactly
    these keys, each value a single plain string (never a list or nested object):
    {{
        "location": "location mentioned",
        "context": "1-2 sentence core summary",
        "accused_victim": "accused or victim details",
        "issues": "core issues or topics, as one comma-separated string"
    }}
    Article Title: {title}
    Article Text: {text[:2000]}
    """
    res = requests.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
        json={
            "model": GROQ_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "response_format": {"type": "json_object"},
            "temperature": 0.2,
        },
        timeout=30,
    )
    res.raise_for_status()
    data = json.loads(res.json()["choices"][0]["message"]["content"])
    # gpt-oss sometimes returns a field as a list even when told to use a
    # plain string - normalize so it always fits the news_items TEXT columns.
    return {k: (", ".join(map(str, v)) if isinstance(v, list) else str(v)) for k, v in data.items()}


def _brand_logo_data_uri():
    """Base64-embeds the logo so Playwright renders it with no file:// path issues."""
    if not os.path.exists(BRAND_LOGO_PATH):
        return None
    with open(BRAND_LOGO_PATH, "rb") as f:
        encoded = base64.b64encode(f.read()).decode()
    ext = os.path.splitext(BRAND_LOGO_PATH)[1].lstrip(".") or "png"
    return f"data:image/{ext};base64,{encoded}"


def fetch_background_image(data):
    """Best-effort low-opacity backdrop from Pexels - real stock photography,
    not AI-generated, chosen by the story's topic (falls back to location).

    This is deliberately NOT used for anything that needs to be exact - no
    logo, no brand colors, no text. It only ever sits behind the real
    HTML/CSS text layer at low opacity, so an unrelated stock photo being an
    imperfect match doesn't matter the way it would for the rest of the
    card. Returns None (card just renders on the plain brand background) if
    unconfigured, no results, or the call fails.
    """
    if not PEXELS_API_KEY:
        return None
    query = data.get("issues") or data.get("location") or "news"
    try:
        resp = requests.get(
            "https://api.pexels.com/v1/search",
            headers={"Authorization": PEXELS_API_KEY},
            params={"query": query, "per_page": 1, "orientation": "square"},
            timeout=15,
        )
        resp.raise_for_status()
        photos = resp.json().get("photos") or []
        if not photos:
            return None
        img_resp = requests.get(photos[0]["src"]["large"], timeout=15)
        img_resp.raise_for_status()
        encoded = base64.b64encode(img_resp.content).decode()
        return f"data:image/jpeg;base64,{encoded}"
    except requests.RequestException as e:
        print(f"Background image fetch skipped: {e}")
        return None


def render_image_cards(data):
    """Renders one branded photocard for the story: logo top-left, date
    top-right, a location pill in the bottom-left corner, and the news
    centered over a low-opacity stock-photo backdrop. No source/vendor
    attribution anywhere on the card.
    """
    logo_uri = _brand_logo_data_uri()
    logo_html = (
        f'<img src="{logo_uri}" style="height:110px; display:block;">'
        if logo_uri
        else f'<div style="font-weight:800; font-size:34px; color:{BRAND_ACCENT_COLOR};">fastSloth News</div>'
    )
    date_str = datetime.now().strftime("%d %b %Y")
    location = data.get("location") or "N/A"
    news_text = data.get("context", "N/A")

    bg_image_uri = fetch_background_image(data)
    bg_layer_html = (
        f'<img src="{bg_image_uri}" style="position:absolute; inset:0; width:100%; height:100%; '
        f'object-fit:cover; opacity:0.16;">'
        if bg_image_uri
        else ""
    )

    html_content = f"""
    <html>
    <head>
        <link rel="preconnect" href="https://fonts.googleapis.com">
        <link href="https://fonts.googleapis.com/css2?family=Inter:wght@600;800&family=Hind+Siliguri:wght@600;700&display=swap" rel="stylesheet">
    </head>
    <body style="margin:0; padding:0; width:1080px; height:1080px; background:{BRAND_BG_COLOR}; box-sizing:border-box;">
        <div style="position:relative; width:100%; height:100%; overflow:hidden;">
            {bg_layer_html}
            <div style="position:absolute; inset:0; background:linear-gradient(135deg, {BRAND_ACCENT_COLOR}1a, transparent 60%);"></div>
            <div style="position:relative; z-index:1; display:flex; flex-direction:column; width:100%; height:100%; padding:56px; box-sizing:border-box; color:{BRAND_TEXT_COLOR}; font-family:{BRAND_FONT_FAMILY};">
                <div style="display:flex; justify-content:space-between; align-items:flex-start;">
                    {logo_html}
                    <div style="font-size:18px; font-weight:600; color:{BRAND_MUTED_COLOR};">{date_str}</div>
                </div>
                <div style="flex:1; display:flex; flex-direction:column; justify-content:center; align-items:center; text-align:center; gap:28px;">
                    <div style="width:64px; height:6px; border-radius:3px; background:{BRAND_ACCENT_COLOR};"></div>
                    <p style="font-size:46px; line-height:1.5; font-weight:800; max-width:880px; margin:0;">{news_text}</p>
                </div>
                <div>
                    <span style="display:inline-flex; align-items:center; gap:8px; background:{BRAND_ACCENT_COLOR}; color:#ffffff; font-weight:700; font-size:20px; padding:12px 24px; border-radius:999px;">📍 {location}</span>
                </div>
            </div>
        </div>
    </body>
    </html>
    """

    story_id = uuid.uuid4().hex[:8]
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1080, "height": 1080})
        page.set_content(html_content)
        path = f"card_{story_id}.png"
        page.screenshot(path=path)
        browser.close()
    return [path]


def upload_to_storage(local_path):
    """Upload a card PNG to the Supabase Storage bucket and return its public URL."""
    dest_path = os.path.basename(local_path)
    with open(local_path, "rb") as f:
        file_bytes = f.read()
    resp = requests.post(
        f"{SUPABASE_URL}/storage/v1/object/{SUPABASE_STORAGE_BUCKET}/{dest_path}",
        headers={
            "apikey": SUPABASE_SERVICE_ROLE_KEY,
            "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
            "Content-Type": "image/png",
        },
        data=file_bytes,
        timeout=15,
    )
    resp.raise_for_status()
    return f"{SUPABASE_URL}/storage/v1/object/public/{SUPABASE_STORAGE_BUCKET}/{dest_path}"


def archive_cards_to_storage(images):
    """Upload every generated card for archival. Returns the public URLs (empty list if not configured)."""
    if not (SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY):
        print("Storage upload skipped: SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY not set.")
        return []
    urls = []
    for img in images:
        try:
            urls.append(upload_to_storage(img))
        except requests.HTTPError as e:
            print(f"Storage upload failed for {img}: {e.response.text if e.response is not None else e}")
    return urls


def publish_to_meta(public_urls, caption):
    """Post the card carousel to the Facebook Page and Instagram business account."""
    if not (META_ACCESS_TOKEN and META_PAGE_ID):
        print("Meta publishing skipped: META_ACCESS_TOKEN / META_PAGE_ID not set.")
        return
    if not public_urls:
        print("Meta publishing skipped: no public image URLs (Supabase Storage not configured or upload failed).")
        return

    # Facebook Page: upload each photo unpublished, then attach all to one feed post.
    photo_ids = []
    for url in public_urls:
        resp = requests.post(
            f"https://graph.facebook.com/v19.0/{META_PAGE_ID}/photos",
            data={"url": url, "published": "false", "access_token": META_ACCESS_TOKEN},
            timeout=15,
        )
        resp.raise_for_status()
        photo_ids.append(resp.json()["id"])

    requests.post(
        f"https://graph.facebook.com/v19.0/{META_PAGE_ID}/feed",
        data={
            "message": caption,
            "access_token": META_ACCESS_TOKEN,
            **{f"attached_media[{i}]": json.dumps({"media_fbid": pid}) for i, pid in enumerate(photo_ids)},
        },
        timeout=15,
    ).raise_for_status()

    # Instagram: a single photo posts directly - CAROUSEL requires 2+ children,
    # which no longer happens now that each story renders exactly one card.
    if META_IG_USER_ID:
        if len(public_urls) == 1:
            container = requests.post(
                f"https://graph.facebook.com/v19.0/{META_IG_USER_ID}/media",
                data={"image_url": public_urls[0], "caption": caption, "access_token": META_ACCESS_TOKEN},
                timeout=15,
            )
        else:
            child_ids = []
            for url in public_urls:
                resp = requests.post(
                    f"https://graph.facebook.com/v19.0/{META_IG_USER_ID}/media",
                    data={"image_url": url, "is_carousel_item": "true", "access_token": META_ACCESS_TOKEN},
                    timeout=15,
                )
                resp.raise_for_status()
                child_ids.append(resp.json()["id"])

            container = requests.post(
                f"https://graph.facebook.com/v19.0/{META_IG_USER_ID}/media",
                data={
                    "media_type": "CAROUSEL",
                    "caption": caption,
                    "children": ",".join(child_ids),
                    "access_token": META_ACCESS_TOKEN,
                },
                timeout=15,
            )
        container.raise_for_status()
        requests.post(
            f"https://graph.facebook.com/v19.0/{META_IG_USER_ID}/media_publish",
            data={"creation_id": container.json()["id"], "access_token": META_ACCESS_TOKEN},
            timeout=15,
        ).raise_for_status()

    print(f"Published {len(public_urls)} slides to Facebook{' and Instagram' if META_IG_USER_ID else ''}.")


def _oauth1_header(method, url, params, extra_signing_params=None):
    """Minimal OAuth 1.0a user-context signer (stdlib only) for the X API."""
    oauth_params = {
        "oauth_consumer_key": X_CONSUMER_KEY,
        "oauth_nonce": uuid.uuid4().hex,
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp": str(int(time.time())),
        "oauth_token": X_ACCESS_TOKEN,
        "oauth_version": "1.0",
    }
    signing_params = {**(extra_signing_params or {}), **oauth_params}
    base_str = "&".join([
        method.upper(),
        urllib.parse.quote(url, safe=""),
        urllib.parse.quote(
            "&".join(f"{urllib.parse.quote(k, safe='')}={urllib.parse.quote(str(v), safe='')}"
                      for k, v in sorted(signing_params.items())),
            safe="",
        ),
    ])
    signing_key = f"{urllib.parse.quote(X_CONSUMER_SECRET, safe='')}&{urllib.parse.quote(X_ACCESS_SECRET, safe='')}"
    signature = base64.b64encode(hmac.new(signing_key.encode(), base_str.encode(), hashlib.sha1).digest()).decode()
    oauth_params["oauth_signature"] = signature
    return "OAuth " + ", ".join(f'{urllib.parse.quote(k, safe="")}="{urllib.parse.quote(v, safe="")}"' for k, v in sorted(oauth_params.items()))


def publish_to_x(images, caption):
    """Upload the first card as media and post it with the summary text via X API v2."""
    if not (X_CONSUMER_KEY and X_CONSUMER_SECRET and X_ACCESS_TOKEN and X_ACCESS_SECRET):
        print("X publishing skipped: X_CONSUMER_KEY/SECRET or X_ACCESS_TOKEN/SECRET not set.")
        return

    upload_url = "https://upload.twitter.com/1.1/media/upload.json"
    with open(images[0], "rb") as f:
        media_resp = requests.post(
            upload_url,
            headers={"Authorization": _oauth1_header("POST", upload_url, {})},
            files={"media": f},
            timeout=30,
        )
    media_resp.raise_for_status()
    media_id = media_resp.json()["media_id_string"]

    tweet_url = "https://api.twitter.com/2/tweets"
    body = {"text": caption[:280], "media": {"media_ids": [media_id]}}
    tweet_resp = requests.post(
        tweet_url,
        headers={"Authorization": _oauth1_header("POST", tweet_url, {}), "Content-Type": "application/json"},
        json=body,
        timeout=15,
    )
    tweet_resp.raise_for_status()
    print("Published card 1 + summary to X.")


def publish_to_socials(images, public_urls, data):
    # No source/vendor attribution in the caption, matching the card itself.
    caption = f"{data.get('context')}\n\n#News #Updates"
    try:
        publish_to_meta(public_urls, caption)
    except requests.HTTPError as e:
        print(f"Meta publish failed: {e.response.text if e.response is not None else e}")
    try:
        publish_to_x(images, caption)
    except requests.HTTPError as e:
        print(f"X publish failed: {e.response.text if e.response is not None else e}")


# Runs once daily now, so a provider's homepage listing (which shows roughly
# the last day of stories) is scanned in full instead of stopping at the
# first unprocessed link.
# ponytail: flat safety caps rather than real rate-limiting - fine for a
# handful of homepages/day, revisit if a provider ever floods the listing.
MAX_STORIES_PER_PROVIDER = 20
MAX_SOCIAL_POSTS_PER_RUN = 3  # keep FB/IG/X from getting a day's backlog dumped on them at once


def run():
    headers = {"User-Agent": "Mozilla/5.0"}
    provider_counts = {c["name"]: 0 for c in NEWS_CHANNELS}
    errors = []
    log_id = start_run_log()
    social_posts_made = 0

    for channel in NEWS_CHANNELS:
        seen_hrefs = set()
        try:
            res = requests.get(channel["url"], headers=headers, timeout=10)
            soup = BeautifulSoup(res.text, "html.parser")

            for a in soup.find_all("a", href=True):
                href, title = urljoin(channel["url"], a['href']), a.get_text(strip=True)
                if href in seen_hrefs or not channel["is_article"](href) or len(title) <= 20:
                    continue
                seen_hrefs.add(href)
                if is_processed(href):
                    continue
                if provider_counts[channel['name']] >= MAX_STORIES_PER_PROVIDER:
                    break

                print(f"Processing new link from {channel['name']}: {title}")

                # One story's failure (AI extraction, rendering, publishing, ...)
                # shouldn't take the rest of this provider's batch down with it.
                try:
                    # AI Extraction
                    article_text = fetch_article_text(href, headers)
                    data = parse_story_with_ai(title, article_text)

                    # Image Generation (3-5 slides)
                    cards = render_image_cards(data)

                    # Archive every card to storage, regardless of the social-posting cap below
                    public_urls = archive_cards_to_storage(cards)

                    # Social Publishing (capped per run, see MAX_SOCIAL_POSTS_PER_RUN)
                    if social_posts_made < MAX_SOCIAL_POSTS_PER_RUN:
                        publish_to_socials(cards, public_urls, data)
                        social_posts_made += 1

                    # DB Logging
                    conn = get_db()
                    cur = conn.cursor()
                    cur.execute("""
                        INSERT INTO news_items (url, source, title, location, context, accused_victim, issues, cron_log_id)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING
                    """, (href, channel['name'], title, data.get('location'), data.get('context'), data.get('accused_victim'), data.get('issues'), log_id))
                    conn.commit()
                    cur.close()
                    conn.close()

                    provider_counts[channel['name']] += 1
                except Exception as e:
                    print(f"Error processing {href}: {e}")
                    errors.append(f"{channel['name']} - {href}: {e}")
        except Exception as e:
            print(f"Error checking {channel['name']}: {e}")
            errors.append(f"{channel['name']}: {e}")

    # Write telemetry execution log - always, even when no new articles were found.
    status = "SUCCESS" if not errors else ("PARTIAL" if sum(provider_counts.values()) > 0 else "ERROR")
    finish_run_log(log_id, status, provider_counts)


if __name__ == "__main__":
    run()
