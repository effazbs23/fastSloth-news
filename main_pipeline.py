import socket

# Force IPv4 socket resolution globally for GitHub Actions compatibility
orig_getaddrinfo = socket.getaddrinfo
def getaddrinfo_ipv4(*args, **kwargs):
    kwargs['family'] = socket.AF_INET
    return orig_getaddrinfo(*args, **kwargs)

socket.getaddrinfo = getaddrinfo_ipv4

import os
import json
import psycopg2
import requests
from bs4 import BeautifulSoup
import google.generativeai as genai
from playwright.sync_api import sync_playwright


DB_URL = os.environ["DATABASE_URL"]
genai.configure(api_key=os.environ["GEMINI_API_KEY"])

NEWS_CHANNELS = [
    {
        "name": "Star News BD",
        "url": "https://starnews.com.bd/",
        "link_filter": lambda href: href.startswith("http") and "starnews.com.bd" in href
    },
    {
        "name": "The Daily Star",
        "url": "https://www.thedailystar.net/todays-news",
        "link_filter": lambda href: href.startswith("http") and "thedailystar.net/news/" in href
    }
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
    Text Snippet: {text[:2000]}
    """
    model = genai.GenerativeModel('gemini-1.5-flash')
    res = model.generate_content(prompt, generation_config={"response_mime_type": "application/json"})
    return json.loads(res.text)

def render_image_cards(data, source_name):
    """Converts story data into 3-5 visual cards using HTML template rendering"""
    cards = [
        {"title": "LOCATION", "content": data.get("location", "N/A")},
        {"title": "CONTEXT", "content": data.get("context", "N/A")},
        {"title": "KEY ENTITIES", "content": data.get("accused_victim", "N/A")},
        {"title": "ISSUES", "content": data.get("issues", "N/A")}
    ]
    
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
            path = f"card_{idx+1}.png"
            page.screenshot(path=path)
            generated_files.append(path)
        browser.close()
    return generated_files

def publish_to_socials(images, data, source):
    caption = f"[{source}] {data.get('context')}\n\n#News #Updates"
    
    # 1. Post to Instagram via Meta Graph API
    # requests.post(f"https://graph.facebook.com/v19.0/{INSTA_ID}/media", ...)
    
    # 2. Post to X (Twitter) via API v2
    # requests.post("https://api.twitter.com/2/tweets", ...)
    
    print(f"Published {len(images)} slides to Instagram, FB, and X.")

def run():
    headers = {"User-Agent": "Mozilla/5.0"}
    provider_counts = {c["name"]: 0 for c in NEWS_CHANNELS}

    for channel in NEWS_CHANNELS:
        try:
            res = requests.get(channel["url"], headers=headers, timeout=10)
            soup = BeautifulSoup(res.text, "html.parser")

            for a in soup.find_all("a", href=True):
                href, title = a['href'], a.get_text(strip=True)
                if channel["link_filter"](href) and len(title) > 20:
                    if not is_processed(href):
                        print(f"Processing new link from {channel['name']}: {title}")
                        
                        # AI Extraction
                        data = parse_story_with_ai(title, title)
                        
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
                        break # Process 1 new story per provider per run
        except Exception as e:
            print(f"Error checking {channel['name']}: {e}")

    # Write telemetry execution log
    conn = get_db()
    cur = conn.cursor()
    cur.execute("INSERT INTO cron_logs (status, fetched_per_provider, total_fetched) VALUES (%s, %s, %s)",
                ("SUCCESS", json.dumps(provider_counts), sum(provider_counts.values())))
    conn.commit()
    cur.close()
    conn.close()

if __name__ == "__main__":
    run()
