import socket

# Force IPv4 socket resolution globally - GitHub Actions runners resolve some
# managed Postgres hosts (Supabase/Neon pooler) to an IPv6 address first, which
# fails with "Network is unreachable" since the runner has no IPv6 route.
orig_getaddrinfo = socket.getaddrinfo
def getaddrinfo_ipv4(*args, **kwargs):
    kwargs['family'] = socket.AF_INET
    return orig_getaddrinfo(*args, **kwargs)

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
from urllib.parse import urljoin

import psycopg2
import requests
from bs4 import BeautifulSoup
from google import genai
from playwright.sync_api import sync_playwright

# DATABASE_URL must use the pooled connection (port 6543) with sslmode=require,
# e.g. postgresql://user:pass@host:6543/postgres?sslmode=require
# Percent-encode special characters in the password (`[`->%5B, `]`->%5D, `@`->%40,
# `#`->%23, `/`->%2F) or psycopg2 will fail to parse the URL / DNS-resolve the host.
DB_URL = os.environ["DATABASE_URL"]
gemini_client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
# gemini-2.5-flash shuts down 2026-10-16; bump this if that's already passed.
GEMINI_MODEL = "gemini-3.5-flash"

# Optional - social publishing is skipped per-platform when its secrets are absent.
META_ACCESS_TOKEN = os.environ.get("META_ACCESS_TOKEN")
META_PAGE_ID = os.environ.get("META_PAGE_ID")
META_IG_USER_ID = os.environ.get("META_IG_USER_ID")

X_CONSUMER_KEY = os.environ.get("X_CONSUMER_KEY")
X_CONSUMER_SECRET = os.environ.get("X_CONSUMER_SECRET")
X_ACCESS_TOKEN = os.environ.get("X_ACCESS_TOKEN")
X_ACCESS_SECRET = os.environ.get("X_ACCESS_SECRET")

# GitHub-Actions-provided - used to host generated card PNGs at a public raw URL,
# since Instagram's Graph API only accepts a fetchable image_url, not raw bytes.
GITHUB_REPOSITORY = os.environ.get("GITHUB_REPOSITORY")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
GITHUB_REF_NAME = os.environ.get("GITHUB_REF_NAME", "main")

# is_article matches on the resolved absolute URL. Both sites list plenty of
# nav/category/tag links alongside real stories; a numeric article-id segment
# in the path is what reliably tells them apart.
NEWS_CHANNELS = [
    {
        "name": "Star News BD",
        "url": "https://starnews.com.bd/",
        "is_article": re.compile(r"starnews\.com\.bd/[a-z-]+/\d{3,}/").search,
    },
    {
        "name": "The Daily Star",
        "url": "https://www.thedailystar.net/todays-news",
        # Daily Star article links are relative (resolved via urljoin below)
        # and don't live directly under /news/, e.g. /sports/football/news/<slug>-<id>
        "is_article": re.compile(r"thedailystar\.net/.+-\d{5,}$").search,
    },
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


def fetch_article_text(url, headers, max_chars=4000):
    """Fetch the article page and pull its paragraph text for AI extraction."""
    res = requests.get(url, headers=headers, timeout=10)
    soup = BeautifulSoup(res.text, "html.parser")
    paragraphs = [p.get_text(strip=True) for p in soup.find_all("p")]
    return " ".join(paragraphs)[:max_chars]


def parse_story_with_ai(title, text):
    prompt = f"""
    Analyze the news article below and return ONLY a JSON object:
    {{
        "location": "location mentioned",
        "context": "1-2 sentence core summary",
        "accused_victim": "accused or victim details",
        "issues": "core issues or topics"
    }}
    Article Title: {title}
    Article Text: {text[:2000]}
    """
    res = gemini_client.models.generate_content(
        model=GEMINI_MODEL,
        contents=prompt,
        config={"response_mime_type": "application/json"},
    )
    return json.loads(res.text)


def render_image_cards(data, source_name):
    """Converts story data into 3-5 visual cards using HTML template rendering"""
    cards = [
        {"title": "LOCATION", "content": data.get("location", "N/A")},
        {"title": "CONTEXT", "content": data.get("context", "N/A")},
        {"title": "KEY ENTITIES", "content": data.get("accused_victim", "N/A")},
        {"title": "ISSUES", "content": data.get("issues", "N/A")}
    ]

    story_id = uuid.uuid4().hex[:8]
    generated_files = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1080, "height": 1080})

        for idx, card in enumerate(cards):
            html_content = f"""
            <html>
            <body style="background:#0f172a; color:#fff; font-family:sans-serif; display:flex; flex-direction:column; justify-content:center; align-items:center; height:100vh; margin:0; padding:40px; box-sizing:border-box;">
                <div style="position:absolute; top:40px; left:40px; font-weight:bold; color:#38bdf8;">{source_name.upper()}</div>
                <h2 style="color:#94a3b8; letter-spacing:2px; font-size:24px;">{card['title']}</h2>
                <p style="font-size:36px; text-align:center; line-height:1.4; font-weight:600;">{card['content']}</p>
            </body>
            </html>
            """
            page.set_content(html_content)
            path = f"card_{story_id}_{idx + 1}.png"
            page.screenshot(path=path)
            generated_files.append(path)
        browser.close()
    return generated_files


# ponytail: images are committed straight into the repo for a free public URL.
# Fine at low volume; if story throughput grows, swap for a Supabase Storage
# bucket (also free tier) so the git history doesn't grow unbounded.
def upload_image_public(local_path):
    """Commit a card PNG into the repo so Instagram's Graph API can fetch it by URL."""
    with open(local_path, "rb") as f:
        content_b64 = base64.b64encode(f.read()).decode()
    dest_path = f"public/cards/{os.path.basename(local_path)}"
    api_url = f"https://api.github.com/repos/{GITHUB_REPOSITORY}/contents/{dest_path}"
    resp = requests.put(
        api_url,
        headers={"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept": "application/vnd.github+json"},
        json={"message": f"cards: add {os.path.basename(local_path)}", "content": content_b64, "branch": GITHUB_REF_NAME},
        timeout=15,
    )
    resp.raise_for_status()
    return f"https://raw.githubusercontent.com/{GITHUB_REPOSITORY}/{GITHUB_REF_NAME}/{dest_path}"


def publish_to_meta(images, caption):
    """Post the card carousel to the Facebook Page and Instagram business account."""
    if not (META_ACCESS_TOKEN and META_PAGE_ID):
        print("Meta publishing skipped: META_ACCESS_TOKEN / META_PAGE_ID not set.")
        return

    public_urls = [upload_image_public(img) for img in images]

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

    # Instagram: build a carousel container from each image, then publish it.
    if META_IG_USER_ID:
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

    print(f"Published {len(images)} slides to Facebook{' and Instagram' if META_IG_USER_ID else ''}.")


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


def publish_to_socials(images, data, source):
    caption = f"[{source}] {data.get('context')}\n\n#News #Updates"
    try:
        publish_to_meta(images, caption)
    except requests.HTTPError as e:
        print(f"Meta publish failed: {e.response.text if e.response is not None else e}")
    try:
        publish_to_x(images, caption)
    except requests.HTTPError as e:
        print(f"X publish failed: {e.response.text if e.response is not None else e}")


def run():
    headers = {"User-Agent": "Mozilla/5.0"}
    provider_counts = {c["name"]: 0 for c in NEWS_CHANNELS}
    errors = []

    for channel in NEWS_CHANNELS:
        try:
            res = requests.get(channel["url"], headers=headers, timeout=10)
            soup = BeautifulSoup(res.text, "html.parser")

            for a in soup.find_all("a", href=True):
                href, title = urljoin(channel["url"], a['href']), a.get_text(strip=True)
                if channel["is_article"](href) and len(title) > 20:
                    if not is_processed(href):
                        print(f"Processing new link from {channel['name']}: {title}")

                        # AI Extraction
                        article_text = fetch_article_text(href, headers)
                        data = parse_story_with_ai(title, article_text)

                        # Image Generation (3-5 slides)
                        cards = render_image_cards(data, channel['name'])

                        # Social Publishing
                        publish_to_socials(cards, data, channel['name'])

                        # DB Logging
                        conn = get_db()
                        cur = conn.cursor()
                        cur.execute("""
                            INSERT INTO news_items (url, source, title, location, context, accused_victim, issues)
                            VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING
                        """, (href, channel['name'], title, data.get('location'), data.get('context'), data.get('accused_victim'), data.get('issues')))
                        conn.commit()
                        cur.close()
                        conn.close()

                        provider_counts[channel['name']] += 1
                        break  # Process 1 new story per provider per run
        except Exception as e:
            print(f"Error checking {channel['name']}: {e}")
            errors.append(f"{channel['name']}: {e}")

    # Write telemetry execution log - always, even when no new articles were found.
    status = "SUCCESS" if not errors else ("PARTIAL" if sum(provider_counts.values()) > 0 else "ERROR")
    conn = get_db()
    cur = conn.cursor()
    cur.execute("INSERT INTO cron_logs (status, fetched_per_provider, total_fetched) VALUES (%s, %s, %s)",
                (status, json.dumps(provider_counts), sum(provider_counts.values())))
    conn.commit()
    cur.close()
    conn.close()


if __name__ == "__main__":
    run()
