from flask import Flask, request, jsonify
from flask_cors import CORS
import requests
import os
import json
import re
import time
import threading
from bs4 import BeautifulSoup

app = Flask(__name__)
# CORS locked to YOUR sites only
CORS(app, origins=[
    "https://flipnprint.com",
    "https://www.flipnprint.com",
    "https://adorable-gecko-6dffd3.netlify.app"
])

SCRAPERAPI_KEY = os.environ.get("SCRAPERAPI_KEY")
CLAUDE_KEY = os.environ.get("CLAUDE_KEY")
GOOGLE_CLIENT_ID = "536188060764-lsuk40m1vj4k8lnuu3go4iearvc0bpnn.apps.googleusercontent.com"

# ===== Paddle subscription tiers + monthly quotas =====
PADDLE_API_KEY = os.environ.get("PADDLE_API_KEY")
OWNER_EMAIL = (os.environ.get("OWNER_EMAIL") or "").strip().lower()

TIER_LIMITS = {"free": 3, "hustler": 75, "empire": 400, "owner": None}  # None = unlimited
PRICE_TIERS = {
    "pri_01kwzktjrx7kkd86qpk6kjw9vf": "hustler",
    "pri_01kwzksjcw251bs1wjqmmaxys5": "empire",
}

tier_cache = {}   # email -> (tier, expires_at)
usage = {}        # email -> {"month": "YYYY-MM", "count": n}
# NOTE: in-memory = resets when Render redeploys/restarts. Fine for launch;
# move to a database when revenue justifies it.

def get_tier(email):
    e = (email or "").lower()
    if OWNER_EMAIL and e == OWNER_EMAIL:
        return "owner"
    now = time.time()
    cached = tier_cache.get(e)
    if cached and cached[1] > now:
        return cached[0]
    tier = "free"
    if PADDLE_API_KEY:
        try:
            h = {"Authorization": f"Bearer {PADDLE_API_KEY}"}
            r = requests.get("https://api.paddle.com/customers",
                             params={"email": e}, headers=h, timeout=10)
            for cust in (r.json().get("data") or []):
                r2 = requests.get("https://api.paddle.com/subscriptions",
                                  params={"customer_id": cust.get("id"), "status": "active"},
                                  headers=h, timeout=10)
                for sub in (r2.json().get("data") or []):
                    for item in (sub.get("items") or []):
                        t = PRICE_TIERS.get((item.get("price") or {}).get("id"))
                        if t == "empire":
                            tier = "empire"
                        elif t == "hustler" and tier != "empire":
                            tier = "hustler"
        except Exception:
            pass  # Paddle unreachable -> treat as free this request; cache below is short
    tier_cache[e] = (tier, now + 600)  # re-check Paddle every 10 min
    return tier

def month_key():
    return time.strftime("%Y-%m")

# Daily fetch caps (protects ScraperAPI credits)
FETCH_LIMITS = {"free": 15, "hustler": 100, "empire": 300, "owner": None}
fetch_usage = {}  # email -> {"day": "YYYY-MM-DD", "count": n}

def use_fetch_quota(email, tier):
    limit = FETCH_LIMITS.get(tier, 15)
    if limit is None:
        return True
    today = time.strftime("%Y-%m-%d")
    rec = fetch_usage.get(email)
    if not rec or rec["day"] != today:
        fetch_usage[email] = {"day": today, "count": 1}
        return True
    if rec["count"] >= limit:
        return False
    rec["count"] += 1
    return True

def quota_status(email, tier):
    limit = TIER_LIMITS.get(tier)
    rec = usage.get(email)
    used = rec["count"] if rec and rec["month"] == month_key() else 0
    return used, limit

def use_quota(email, tier):
    limit = TIER_LIMITS.get(tier)
    if limit is None:
        return True, 0, None
    m = month_key()
    rec = usage.get(email)
    if not rec or rec["month"] != m:
        usage[email] = {"month": m, "count": 1}
        return True, 1, limit
    if rec["count"] >= limit:
        return False, rec["count"], limit
    rec["count"] += 1
    return True, rec["count"], limit

def verify_google(req):
    """Returns user email or None."""
    auth_header = req.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return None
    token = auth_header.split(" ", 1)[1]
    try:
        from google.oauth2 import id_token as google_id_token
        from google.auth.transport import requests as google_requests
        info = google_id_token.verify_oauth2_token(
            token, google_requests.Request(), GOOGLE_CLIENT_ID
        )
        return info.get("email")
    except Exception:
        return None

# ===== SHARED SCRAPER =====
def scrape_product(url):
    """Fetch a product page via ScraperAPI and extract title/image/price. Returns dict or None."""
    try:
        payload = {'api_key': SCRAPERAPI_KEY, 'url': url}
        response = requests.get('https://api.scraperapi.com', params=payload, timeout=35)
        if response.status_code != 200:
            return None
        soup = BeautifulSoup(response.text, 'html.parser')
        title, image, price = "", "", ""
        og_title = soup.find('meta', property='og:title')
        if og_title:
            title = og_title.get('content', '')
        if not title:
            t = soup.find('h1')
            if t: title = t.get_text(strip=True)[:200]
        og_image = soup.find('meta', property='og:image')
        if og_image:
            image = og_image.get('content', '')
        if not image:
            img = soup.find('img')
            if img: image = img.get('src', '')
        if image and not image.startswith('http'):
            from urllib.parse import urljoin
            image = urljoin(url, image)
        ps = soup.find('span', class_='a-price-whole')
        if ps:
            m = re.search(r'[\d,]+\.?\d{0,2}', ps.get_text(strip=True))
            if m: price = m.group(0).replace(',', '')
        if not price:
            for p in re.findall(r'\$\s*?([\d,]+\.?\d{0,2})', soup.get_text()):
                try:
                    pf = float(p.replace(',', ''))
                    if 2 <= pf <= 500:
                        price = p.replace(',', ''); break
                except: pass
        if not title:
            return None
        return {"title": title, "image": image, "price": float(price) if price else 0, "url": url}
    except Exception:
        return None

# ===== DAILY DROPS ENGINE =====
DROPS = {"date": None, "items": [], "generating": False, "error": None, "runs": 0}
DROPS_LOCK = threading.Lock()
DROPS_COUNT = 6

def run_quick_analysis(name, listed_price):
    """Run a quick-mode Claude analysis for a drops product. Returns parsed JSON or None."""
    prompt = f'''Product: "{name}"
Real listed price on Amazon: ${listed_price or "unknown"}
Sourcing from: not specified (assume AliExpress/Alibaba typical pricing)
Selling on: not specified — pick the single best marketplace for this product
The user did NOT provide cost or resell price.
QUICK MODE: Estimate a realistic per-unit sourcing cost for this product (typical AliExpress/Alibaba price), a realistic resell price, and pick the best marketplace. Base ALL fee math, net profit, and margin on YOUR estimates. You MUST include an "assumptions" object in the JSON with your numbers.
Units planning to buy: 50

You are a professional reselling analyst covering ALL marketplaces. 

CRITICAL: The fees, risks, and sell plan MUST be specific to the marketplace you pick — use that marketplace's real fee structure.

Return ONLY raw JSON no markdown no backticks:
{{
  "assumptions": {{"cost": 4.50, "resell": 24.99, "marketplace": "chosen marketplace"}},
  "flipScore": 7,
  "demandScore": 7,
  "competitionScore": 5,
  "tiktokScore": 6,
  "overview": "2-3 sentence honest take, mention the chosen marketplace",
  "fees": {{
    "items": [
      {{"name": "fee name", "desc": "3-5 word explanation", "amount": 3.50}},
      {{"name": "second fee", "desc": "short explanation", "amount": 1.20}},
      {{"name": "third fee if applicable", "desc": "short explanation", "amount": 0.30}}
    ],
    "totalFeesPerUnit": 5.00,
    "netProfitAfterFees": 15.28,
    "netMarginAfterFees": 42,
    "feeInsight": "one sentence about the biggest fee"
  }},
  "competition": [
    {{"sellerName": "competitor name", "estimatedPrice": 29.99, "estimatedMonthlyRevenue": 8500, "reviewCount": 1240, "rating": 4.3, "weaknesses": "one sentence weakness"}},
    {{"sellerName": "competitor name", "estimatedPrice": 24.99, "estimatedMonthlyRevenue": 4200, "reviewCount": 456, "rating": 4.1, "weaknesses": "one sentence weakness"}},
    {{"sellerName": "competitor name", "estimatedPrice": 19.99, "estimatedMonthlyRevenue": 1800, "reviewCount": 89, "rating": 3.7, "weaknesses": "one sentence weakness"}}
  ],
  "competitionSummary": "one sentence on market saturation",
  "risks": [
    {{"level": "high", "title": "risk name", "desc": "specific risk", "action": "mitigation"}},
    {{"level": "medium", "title": "risk name", "desc": "specific risk", "action": "mitigation"}},
    {{"level": "low", "title": "risk name", "desc": "specific risk", "action": "mitigation"}}
  ],
  "overallRisk": "Medium",
  "overallRiskColor": "#FFD747",
  "sellPlan": [
    {{"step": 1, "title": "title", "desc": "one sentence action"}},
    {{"step": 2, "title": "title", "desc": "one sentence action"}},
    {{"step": 3, "title": "title", "desc": "one sentence action"}},
    {{"step": 4, "title": "title", "desc": "one sentence action"}},
    {{"step": 5, "title": "title", "desc": "one sentence action"}}
  ]
}}'''
    try:
        res = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"Content-Type": "application/json", "anthropic-version": "2023-06-01", "x-api-key": CLAUDE_KEY},
            json={"model": "claude-sonnet-4-6", "max_tokens": 1500,
                  "system": "You are a professional reselling analyst. Output ONLY raw JSON, no markdown.",
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=90
        )
        result = res.json()
        if "error" in result:
            print(f"DROPS: Claude error: {result['error'].get('message','')[:120]} — retrying in 6s", flush=True)
            time.sleep(6)
            res = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers={"Content-Type": "application/json", "anthropic-version": "2023-06-01", "x-api-key": CLAUDE_KEY},
                json={"model": "claude-sonnet-4-6", "max_tokens": 1500,
                      "system": "You are a professional reselling analyst. Output ONLY raw JSON, no markdown.",
                      "messages": [{"role": "user", "content": prompt}]},
                timeout=90
            )
            result = res.json()
            if "error" in result:
                return None
        text = result["content"][0]["text"]
        return json.loads(text[text.index("{"):text.rindex("}")+1])
    except Exception as e:
        print(f"DROPS: analysis exception: {e}", flush=True)
        return None

def generate_drops_background():
    today = time.strftime("%Y-%m-%d")
    try:
        # 1. Scrape Amazon Movers & Shakers for trending product links
        payload = {'api_key': SCRAPERAPI_KEY, 'url': 'https://www.amazon.com/gp/movers-and-shakers'}
        r = requests.get('https://api.scraperapi.com', params=payload, timeout=45)
        asins = []
        if r.status_code == 200:
            for href in re.findall(r'/dp/([A-Z0-9]{10})', r.text):
                if href not in asins:
                    asins.append(href)
        print(f"DROPS: movers page gave {len(asins)} candidate ASINs", flush=True)
        if not asins:
            with DROPS_LOCK:
                DROPS["generating"] = False
                DROPS["error"] = "Couldn't load trending products right now — pull to refresh in a minute."
            return
        done_asins = set()
        with DROPS_LOCK:
            done_asins = {it.get("asin") for it in DROPS["items"]}
        # 2. For each trending product: scrape details + run quick analysis
        for asin in asins[:24]:
            if asin in done_asins:
                continue
            with DROPS_LOCK:
                if len(DROPS["items"]) >= DROPS_COUNT:
                    break
            product = scrape_product(f"https://www.amazon.com/dp/{asin}")
            if not product or not product.get("title") or product["title"] == "Product":
                print(f"DROPS: scrape FAILED for {asin}", flush=True)
                continue
            print(f"DROPS: scraped OK: {product['title'][:60]}", flush=True)
            ai = run_quick_analysis(product["title"], product.get("price"))
            if not ai or not ai.get("assumptions"):
                print(f"DROPS: analysis FAILED for {asin}", flush=True)
                continue
            print(f"DROPS: drop READY ({asin}) score={ai.get('flipScore')}", flush=True)
            a = ai["assumptions"]
            cost = float(a.get("cost") or 0)
            resell = float(a.get("resell") or 0)
            item = {
                "asin": asin,
                "name": product["title"],
                "image": product.get("image", ""),
                "listedPrice": product.get("price", 0),
                "cost": cost,
                "resell": resell,
                "margin": resell - cost,
                "marginPercent": round(((resell - cost) / resell) * 100) if resell else 0,
                "units": 50,
                "source": "AliExpress (AI estimate)",
                "sell": a.get("marketplace", "eBay"),
                "ai": ai,
                "aiEstimated": True
            }
            with DROPS_LOCK:
                if DROPS["date"] == today:
                    DROPS["items"].append(item)
                    DROPS["items"].sort(key=lambda x: -(x["ai"].get("flipScore") or 0))
    finally:
        with DROPS_LOCK:
            DROPS["generating"] = False

@app.route("/drops", methods=["GET"])
def drops():
    email = verify_google(request)
    if not email:
        return jsonify({"error": "Sign in required"}), 401
    today = time.strftime("%Y-%m-%d")
    with DROPS_LOCK:
        new_day = DROPS["date"] != today
        needs_topup = (not new_day and len(DROPS["items"]) < DROPS_COUNT and DROPS["runs"] < 4)
        if (new_day or needs_topup) and not DROPS["generating"]:
            if new_day:
                DROPS["date"] = today
                DROPS["items"] = []
                DROPS["runs"] = 0
            DROPS["error"] = None
            DROPS["generating"] = True
            DROPS["runs"] += 1
            print(f"DROPS: kicking generation run #{DROPS['runs']} for {today}", flush=True)
            threading.Thread(target=generate_drops_background, daemon=True).start()
        return jsonify({
            "date": DROPS["date"],
            "items": DROPS["items"],
            "generating": DROPS["generating"],
            "error": DROPS["error"]
        })

@app.route("/")
def home():
    return "FlipNPrint backend running"

@app.route("/fetch-product", methods=["POST"])
def fetch_product():
    # Auth required — bots can't burn ScraperAPI credits anymore
    email = verify_google(request)
    if not email:
        return jsonify({"error": "Sign in required"}), 401
    if not use_fetch_quota(email, get_tier(email)):
        return jsonify({"error": "Daily fetch limit reached — try again tomorrow or upgrade your plan."}), 429

    data = request.json
    url = data.get("url", "")
    if not url or not url.startswith("http"):
        return jsonify({"error": "No valid URL provided"}), 400

    try:
        payload = {'api_key': SCRAPERAPI_KEY, 'url': url}
        response = requests.get('https://api.scraperapi.com', params=payload, timeout=30)
        if response.status_code != 200:
            return jsonify({"error": f"ScraperAPI error: {response.status_code}"}), 500

        soup = BeautifulSoup(response.text, 'html.parser')
        title = ""
        image = ""
        price = ""
        rating = ""

        og_title = soup.find('meta', property='og:title')
        if og_title:
            title = og_title.get('content', '')
        if not title:
            title_tag = soup.find('h1')
            if title_tag:
                title = title_tag.get_text(strip=True)[:200]
        if not title:
            title_div = soup.find('div', class_=re.compile(r'ProductTitle|product-title', re.I))
            if title_div:
                title = title_div.get_text(strip=True)[:200]

        og_image = soup.find('meta', property='og:image')
        if og_image:
            image = og_image.get('content', '')
        if not image:
            img_tag = soup.find('img')
            if img_tag:
                image = img_tag.get('src', '')
        if image and not image.startswith('http'):
            from urllib.parse import urljoin
            image = urljoin(url, image)

        price_span = soup.find('span', class_='a-price-whole')
        if price_span:
            m = re.search(r'[\d,]+\.?\d{0,2}', price_span.get_text(strip=True))
            if m:
                price = m.group(0).replace(',', '')
        if not price:
            price_span = soup.find('span', class_=re.compile(r'a-price'))
            if price_span:
                m = re.search(r'[\d,]+\.?\d{0,2}', price_span.get_text(strip=True))
                if m:
                    price = m.group(0).replace(',', '')
        if not price:
            all_text = soup.get_text()
            for p in re.findall(r'\$\s*?([\d,]+\.?\d{0,2})', all_text):
                try:
                    pf = float(p.replace(',', ''))
                    if 2 <= pf <= 500:
                        price = p.replace(',', '')
                        break
                except:
                    pass
        if not price:
            price_span = soup.find('span', class_=re.compile(r'price|cost', re.I))
            if price_span:
                m = re.search(r'[\d,]+\.?\d{0,2}', price_span.get_text(strip=True))
                if m:
                    price = m.group(0).replace(',', '')

        if not title:
            title = "Product"

        return jsonify({
            "title": title,
            "image": image,
            "price": float(price) if price else 0,
            "rating": float(rating) if rating else 0,
            "reviews": 0,
            "category": "",
            "brand": "",
            "url": url
        })
    except Exception as e:
        return jsonify({"error": f"Failed to fetch: {str(e)}"}), 500

@app.route("/me", methods=["GET"])
def me():
    email = verify_google(request)
    if not email:
        return jsonify({"error": "Sign in required"}), 401
    tier = get_tier(email)
    used, limit = quota_status(email, tier)
    return jsonify({"email": email, "tier": tier, "used": used, "limit": limit})

@app.route("/analyze", methods=["POST"])
def analyze():
    email = verify_google(request)
    if not email:
        return jsonify({"error": "Sign in required"}), 401
    tier = get_tier(email)
    ok, used, limit = use_quota(email, tier)
    if not ok:
        return jsonify({
            "error": f"You've used all {limit} analyses on your {tier.title()} plan this month.",
            "tier": tier, "used": used, "limit": limit, "upgrade": True
        }), 429

    d = request.json or {}
    # Structured fields only — the prompt is built HERE, users can't send arbitrary prompts
    name = str(d.get("name", ""))[:250]
    source = str(d.get("source", "not specified"))[:100]
    sell = str(d.get("sell", "not specified"))[:100]
    units = int(d.get("units") or 50)
    quick = bool(d.get("quick"))
    listed_price = d.get("listedPrice") or "unknown"
    rating = d.get("rating") or "unknown"
    reviews = d.get("reviews") or "unknown"
    category = d.get("category") or "unknown"
    try:
        cost = float(d.get("cost")) if d.get("cost") else None
        resell = float(d.get("resell")) if d.get("resell") else None
    except:
        cost, resell = None, None

    if not name:
        return jsonify({"error": "Missing product name"}), 400
    if not quick and (not cost or not resell):
        return jsonify({"error": "Missing cost or resell price"}), 400

    if quick:
        pricing = f"""The user did NOT provide cost or resell price.{f' (They did enter cost: ${cost}.)' if cost else ''}{f' (They did enter resell: ${resell}.)' if resell else ''}
QUICK MODE: Estimate a realistic per-unit sourcing cost for this product (typical AliExpress/Alibaba price), a realistic resell price, and pick the best marketplace. Base ALL fee math, net profit, and margin on YOUR estimates. You MUST include an "assumptions" object in the JSON with your numbers."""
    else:
        margin = resell - cost
        margin_pct = round((margin / resell) * 100) if resell else 0
        pricing = f"""Your cost per unit: ${cost}
Your resell price: ${resell}
Gross margin: ${margin:.2f} ({margin_pct}%)"""

    prompt = f"""Product: "{name}"
Real product data fetched live from the listing:
- Listed price: ${listed_price}
- Star rating: {rating} stars
- Review count: {reviews} reviews
- Category: {category}
Sourcing from: {source}
Selling on: {sell}
{pricing}
Units planning to buy: {units}

You are a professional reselling analyst covering ALL marketplaces (eBay, Facebook Marketplace, Depop, Poshmark, Mercari, Amazon FBA/FBM, TikTok Shop, Etsy, local selling, etc). The user is selling on: {sell}.

CRITICAL: The fees, risks, and sell plan MUST be specific to selling on {sell} — use that marketplace's real fee structure (e.g. eBay final value fee ~13%, Depop 10%, FB Marketplace shipping fee or $0 local, Amazon referral + FBA fees, TikTok Shop commission). If the marketplace is "not specified", pick the best marketplace for this product, say which one you picked in the overview, and base everything on it.

Return ONLY raw JSON no markdown no backticks:
{{
  "assumptions": {{"cost": 4.50, "resell": 24.99, "marketplace": "the marketplace all analysis is based on"}},
  "flipScore": 7,
  "demandScore": 7,
  "competitionScore": 5,
  "tiktokScore": 6,
  "overview": "2-3 sentence honest take using the real listing data above — mention the actual rating and review count if available, and reference the chosen marketplace",
  "fees": {{
    "items": [
      {{"name": "fee name specific to {sell}", "desc": "3-5 word explanation", "amount": 3.50}},
      {{"name": "second fee", "desc": "short explanation", "amount": 1.20}},
      {{"name": "third fee if applicable", "desc": "short explanation", "amount": 0.30}}
    ],
    "totalFeesPerUnit": 5.00,
    "netProfitAfterFees": 15.28,
    "netMarginAfterFees": 42,
    "feeInsight": "one sentence about the biggest fee eating this product's margin on {sell}"
  }},
  "competition": [
    {{"sellerName": "competitor name", "estimatedPrice": 29.99, "estimatedMonthlyRevenue": 8500, "reviewCount": 1240, "rating": 4.3, "weaknesses": "one sentence weakness to exploit"}},
    {{"sellerName": "competitor name", "estimatedPrice": 24.99, "estimatedMonthlyRevenue": 4200, "reviewCount": 456, "rating": 4.1, "weaknesses": "one sentence weakness"}},
    {{"sellerName": "competitor name", "estimatedPrice": 19.99, "estimatedMonthlyRevenue": 1800, "reviewCount": 89, "rating": 3.7, "weaknesses": "one sentence weakness"}}
  ],
  "competitionSummary": "one sentence on market saturation and opportunity on {sell} specifically",
  "risks": [
    {{"level": "high|medium|low", "title": "risk name", "desc": "specific risk for this product on {sell}", "action": "how to handle it"}},
    {{"level": "high|medium|low", "title": "risk name", "desc": "specific risk", "action": "mitigation"}},
    {{"level": "high|medium|low", "title": "risk name", "desc": "specific risk", "action": "mitigation"}}
  ],
  "overallRisk": "Low|Medium|High",
  "overallRiskColor": "#00FF87",
  "sellPlan": [
    {{"step": 1, "title": "title", "desc": "one sentence action specific to launching on {sell}"}},
    {{"step": 2, "title": "title", "desc": "one sentence action"}},
    {{"step": 3, "title": "title", "desc": "one sentence action"}},
    {{"step": 4, "title": "title", "desc": "one sentence action"}},
    {{"step": 5, "title": "title", "desc": "one sentence action"}}
  ]
}}"""

    try:
        res = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "Content-Type": "application/json",
                "anthropic-version": "2023-06-01",
                "x-api-key": CLAUDE_KEY
            },
            json={
                "model": "claude-sonnet-4-6",
                "max_tokens": 1500,
                "system": "You are a professional reselling analyst. Output ONLY raw JSON, no markdown.",
                "messages": [{"role": "user", "content": prompt}]
            },
            timeout=90
        )
        result = res.json()
        if "error" in result:
            return jsonify({"error": result["error"]["message"]}), 500
        text = result["content"][0]["text"]
        start = text.index("{")
        end = text.rindex("}") + 1
        parsed = json.loads(text[start:end])
        return jsonify(parsed)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
