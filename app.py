from flask import Flask, request, jsonify
from flask_cors import CORS
import base64
import requests
import os
import json
import re
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from bs4 import BeautifulSoup

# ===== MARKETPLACE WHITELIST =====
VALID_MARKETPLACES = ["eBay", "Amazon FBA", "FB Marketplace", "Depop", "Etsy", "TikTok Shop", "Poshmark"]
VALID_SOURCES = ["AliExpress", "Alibaba", "DHgate", "1688", "Amazon", "Walmart", "eBay", "not specified"]

# ===== SUPABASE =====
try:
    from supabase import create_client, Client
    SUPABASE_URL = os.environ.get("SUPABASE_URL")
    SUPABASE_KEY = os.environ.get("SUPABASE_SECRET_KEY")  # service_role for backend
    if SUPABASE_URL and SUPABASE_KEY:
        supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
        SUPABASE_ENABLED = True
        print("✓ Supabase connected", flush=True)
    else:
        SUPABASE_ENABLED = False
        print("⚠ Supabase not configured — using in-memory fallback", flush=True)
except Exception as e:
    SUPABASE_ENABLED = False
    print(f"⚠ Supabase init failed: {e} — using in-memory fallback", flush=True)

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 1 * 1024 * 1024  # 1MB max request body
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

import threading as _threading_quota
QUOTA_LOCK = _threading_quota.Lock()

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
    with QUOTA_LOCK:
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
    with QUOTA_LOCK:
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
DROPS_COUNT = 15

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
  "bestPlay": {{"strategy": "one of: FB Marketplace Flip | eBay Flip | Amazon FBA | Dropshipping | TikTok Shop | Etsy Shop", "reason": "one short sentence why this strategy fits this exact product"}},
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
        # ===== 1. Gather trending candidates from 5 sources in parallel =====
        SOURCES = [
            ("amazon",     "https://www.amazon.com/gp/movers-and-shakers", r"/dp/([A-Z0-9]{10})"),
            ("ebay",       "https://www.ebay.com/rpp/hot-items",           r"/itm/(\d{9,13})"),
            ("walmart",    "https://www.walmart.com/shop/trending",        r"/ip/(?:[^\"]*?/)?(\d{6,12})"),
            ("etsy",       "https://www.etsy.com/featured/trending-items", r"/listing/(\d{6,12})"),
            ("aliexpress", "https://www.aliexpress.com",                   r"/item/(\d{10,16})\.html"),
        ]
        URL_BUILDERS = {
            "amazon":     lambda i: f"https://www.amazon.com/dp/{i}",
            "ebay":       lambda i: f"https://www.ebay.com/itm/{i}",
            "walmart":    lambda i: f"https://www.walmart.com/ip/{i}",
            "etsy":       lambda i: f"https://www.etsy.com/listing/{i}",
            "aliexpress": lambda i: f"https://www.aliexpress.com/item/{i}.html",
        }

        def fetch_source(src):
            name, url, pattern = src
            try:
                r = requests.get("https://api.scraperapi.com",
                                 params={"api_key": SCRAPERAPI_KEY, "url": url}, timeout=45)
                ids = []
                if r.status_code == 200:
                    for pid in re.findall(pattern, r.text):
                        if pid not in ids:
                            ids.append(pid)
                print(f"DROPS: {name} gave {len(ids)} candidates", flush=True)
                return [(name, pid) for pid in ids[:10]]
            except Exception as e:
                print(f"DROPS: {name} source failed: {e}", flush=True)
                return []

        per_source = {}
        with ThreadPoolExecutor(max_workers=5) as pool:
            futures = {pool.submit(fetch_source, s): s[0] for s in SOURCES}
            for f in as_completed(futures):
                results = f.result()
                if results:
                    per_source[results[0][0]] = results

        # Round-robin interleave for diversity: amazon, ebay, walmart, etsy, ali, amazon, ...
        candidates = []
        idx = 0
        while True:
            added = False
            for name in ["amazon", "ebay", "walmart", "etsy", "aliexpress"]:
                lst = per_source.get(name, [])
                if idx < len(lst):
                    candidates.append(lst[idx])
                    added = True
            if not added:
                break
            idx += 1

        print(f"DROPS: total candidates across sources: {len(candidates)}", flush=True)
        if len(candidates) < 2:
            with DROPS_LOCK:
                DROPS["generating"] = False
                DROPS["error"] = "Couldn't load trending products — try again in a minute."
            return

        done_keys = set()
        with DROPS_LOCK:
            done_keys = {it.get("asin") for it in DROPS["items"]}

        JUNK = ['gift card', 'egift', 'amazon business', 'amazon prime', 'prime video',
                'kindle', 'audible', 'alexa', 'subscribe & save', 'amazon reload', 'echo ',
                'sign in', 'robot check', 'access denied', 'page not found']

        def process_candidate(cand):
            platform, pid = cand
            key = f"{platform}:{pid}"
            url = URL_BUILDERS[platform](pid)
            product = scrape_product(url)
            if not product or not product.get("title") or product["title"] == "Product":
                print(f"DROPS: scrape FAILED [{platform}] {pid}", flush=True)
                return None
            tl = product["title"].lower()
            if any(j in tl for j in JUNK):
                print(f"DROPS: junk filtered: {product['title'][:50]}", flush=True)
                return None
            print(f"DROPS: scraped OK [{platform}]: {product['title'][:55]}", flush=True)
            ai = run_quick_analysis(product["title"], product.get("price"))
            if not ai or not ai.get("assumptions"):
                print(f"DROPS: analysis FAILED [{platform}] {pid}", flush=True)
                return None
            score = ai.get("flipScore") or 0
            if score < 5:
                print(f"DROPS: low score ({score}) filtered [{platform}]: {product['title'][:45]}", flush=True)
                return None
            a = ai["assumptions"]
            cost = float(a.get("cost") or 0)
            resell = float(a.get("resell") or 0)
            print(f"DROPS: drop READY [{platform}] score={score}", flush=True)
            return {
                "asin": key,
                "platform": platform.title(),
                "url": url,
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

        pending = [cd for cd in candidates if f"{cd[0]}:{cd[1]}" not in done_keys][:30]
        for i in range(0, len(pending), 3):
            with DROPS_LOCK:
                if len(DROPS["items"]) >= DROPS_COUNT:
                    break
            wave = pending[i:i+3]
            with ThreadPoolExecutor(max_workers=3) as pool:
                futures = [pool.submit(process_candidate, cd) for cd in wave]
                for f in as_completed(futures):
                    item = f.result()
                    if item:
                        with DROPS_LOCK:
                            if DROPS["date"] == today and len(DROPS["items"]) < DROPS_COUNT:
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
    track_user(email)
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

@app.route("/supplier", methods=["POST"])
def supplier():
    email = verify_google(request)
    if not email:
        return jsonify({"error": "Sign in required"}), 401
    if not use_fetch_quota(email, get_tier(email)):
        return jsonify({"error": "Daily fetch limit reached — try again tomorrow or upgrade your plan."}), 429

    d = request.json or {}
    url = str(d.get("url", "")).strip()
    desc = str(d.get("desc", "")).strip()[:150]

    # Resolve the search query: from a pasted link, or a typed description
    query = desc
    if url and url.startswith("http"):
        product = scrape_product(url)
        if product and product.get("title") and product["title"] != "Product":
            query = product["title"]
        elif not desc:
            return jsonify({"error": "Couldn't read that link — try typing a short description instead."}), 400
    if not query:
        return jsonify({"error": "Paste a product link or type a description."}), 400

    # Trim query to its meaningful head (long Amazon titles hurt search)
    short_q = " ".join(re.sub(r"[^\w\s-]", " ", query).split()[:8])
    from urllib.parse import quote_plus
    q_enc = quote_plus(short_q)

    SEARCHES = [
        ("AliExpress", f"https://www.aliexpress.com/wholesale?SearchText={q_enc}", "/item/", r"/item/(\d{8,16})\.html"),
        ("Alibaba",    f"https://www.alibaba.com/trade/search?SearchText={q_enc}", "/product-detail/", r"/product-detail/([\w-]+?)_(\d{8,16})\.html"),
        ("DHgate",     f"https://www.dhgate.com/wholesale/search.do?act=search&searchkey={q_enc}", "/product/", r"/product/([\w-]+?)/(\d{6,12})\.html"),
    ]

    def search_source(src):
        name, surl, marker, _pat = src
        out = []
        try:
            r = requests.get("https://api.scraperapi.com",
                             params={"api_key": SCRAPERAPI_KEY, "url": surl}, timeout=40)
            if r.status_code != 200:
                print(f"SUPPLIER: {name} search HTTP {r.status_code}", flush=True)
                return out
            soup = BeautifulSoup(r.text, "html.parser")
            seen = set()
            for a in soup.find_all("a", href=True):
                href = a["href"]
                if marker not in href:
                    continue
                if href.startswith("//"):
                    href = "https:" + href
                elif href.startswith("/"):
                    base = {"AliExpress": "https://www.aliexpress.com",
                            "Alibaba": "https://www.alibaba.com",
                            "DHgate": "https://www.dhgate.com"}[name]
                    href = base + href
                href = href.split("?")[0]
                if href in seen:
                    continue
                seen.add(href)
                # Title: anchor text, title attr, or the URL slug
                title = (a.get_text(" ", strip=True) or a.get("title") or "").strip()
                if not title or len(title) < 8:
                    m = re.search(r"/(?:product-detail|product)/([\w-]{10,})", href)
                    if m:
                        title = m.group(1).replace("-", " ").replace("_", " ")[:120]
                if not title or len(title) < 8:
                    continue
                # Price: look near the anchor for a $ amount
                price = ""
                ctx = a.get_text(" ", strip=True)
                parent = a.find_parent()
                if parent:
                    ctx = ctx + " " + parent.get_text(" ", strip=True)[:300]
                pm = re.search(r"(?:US\s*)?\$\s*(\d{1,4}(?:[.,]\d{1,2})?)", ctx)
                if pm:
                    price = pm.group(1).replace(",", "")
                out.append({"source": name, "url": href, "title": title[:150], "price": price})
                if len(out) >= 10:
                    break
        except Exception as e:
            print(f"SUPPLIER: {name} failed: {e}", flush=True)
        print(f"SUPPLIER: {name} gave {len(out)} candidates", flush=True)
        return out

    candidates = []
    with ThreadPoolExecutor(max_workers=3) as pool:
        for f in as_completed([pool.submit(search_source, s) for s in SEARCHES]):
            candidates.extend(f.result())

    if not candidates:
        return jsonify({"query": short_q, "matches": [],
                        "error": "No supplier listings found — try a shorter, simpler description."})

    # Claude ranks the true matches
    listing = "\n".join(f'{i}| {c["source"]} | {c["title"][:100]} | ${c["price"] or "?"}'
                         for i, c in enumerate(candidates))
    rank_prompt = f"""Target product: "{query[:150]}"

Candidate supplier listings (index | source | title | price):
{listing}

Pick up to 6 candidates that are genuinely the SAME or near-identical product (not accessories, not different products). Return ONLY raw JSON:
{{"matches": [{{"index": 0, "match": 92, "note": "5-8 word reason"}}]}}
Order by match confidence descending. If none genuinely match, return {{"matches": []}}."""
    picks = []
    try:
        res = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"Content-Type": "application/json", "anthropic-version": "2023-06-01", "x-api-key": CLAUDE_KEY},
            json={"model": "claude-sonnet-4-6", "max_tokens": 500,
                  "system": "You match products to supplier listings. Output ONLY raw JSON.",
                  "messages": [{"role": "user", "content": rank_prompt}]},
            timeout=60
        )
        result = res.json()
        if "error" not in result:
            text = result["content"][0]["text"]
            picks = json.loads(text[text.index("{"):text.rindex("}")+1]).get("matches", [])
    except Exception as e:
        print(f"SUPPLIER: ranking failed: {e}", flush=True)

    matches = []
    if picks:
        for p in picks:
            i = p.get("index")
            if isinstance(i, int) and 0 <= i < len(candidates):
                cand = dict(candidates[i])
                cand["match"] = min(100, max(0, int(p.get("match") or 0)))
                cand["note"] = str(p.get("note", ""))[:120]
                matches.append(cand)
    else:
        # Fallback: raw top candidates, unranked
        matches = [dict(cd, match=None, note="") for cd in candidates[:6]]

    return jsonify({"query": short_q, "matches": matches})

# Early access list storage (Supabase-backed)
EARLY_ACCESS_LIST = {}  # local cache for fast reads
USERS = {}  # local cache for fast reads

def track_user(email, analysis=False):
    if not email:
        return
    email = email.lower()
    now_ts = time.strftime("%Y-%m-%d %H:%M:%S")
    
    # Update local cache
    rec = USERS.get(email)
    if not rec:
        USERS[email] = {"first": now_ts, "last": now_ts, "visits": 1, "analyses": 1 if analysis else 0}
        print(f"USERS: new user {email} (total: {len(USERS)})", flush=True)
    else:
        rec["last"] = now_ts
        rec["visits"] += 1
        if analysis:
            rec["analyses"] += 1
    
    # Persist to Supabase (non-blocking, fire-and-forget)
    if SUPABASE_ENABLED:
        try:
            threading.Thread(target=lambda: supabase.table("users").upsert({
                "email": email,
                "first_seen": USERS[email]["first"],
                "last_seen": now_ts,
                "visit_count": USERS[email]["visits"],
                "analyses_count": USERS[email]["analyses"]
            }).execute(), daemon=True).start()
        except Exception as e:
            print(f"SUPABASE: track_user failed: {e}", flush=True)

EMAIL_RE = re.compile(r"^[\w.+-]{1,64}@[\w-]{1,63}(\.[\w-]{2,63})+$")

@app.route("/early-access", methods=["POST"])
def early_access():
    d = request.json or {}
    email = str(d.get("email", "")).strip().lower()[:255]
    if not EMAIL_RE.match(email or ""):
        return jsonify({"error": "Invalid email"}), 400
    if SUPABASE_ENABLED:
        try:
            result = supabase.table("early_access").select("id").eq("email", email).execute()
            if result.data:
                return jsonify({"message": "Already on the list"}), 200
            supabase.table("early_access").insert({"email": email}).execute()
            print(f"EARLY_ACCESS: {email} joined (Supabase)", flush=True)
            # Update local cache
            EARLY_ACCESS_LIST[email] = time.strftime("%Y-%m-%d %H:%M:%S")
            return jsonify({"message": "Added to early access list"}), 201
        except Exception as e:
            print(f"EARLY_ACCESS: Supabase insert failed: {e} — falling back to local", flush=True)
            if len(EARLY_ACCESS_LIST) >= 5000:
                return jsonify({"error": "List is full"}), 429
            if email in EARLY_ACCESS_LIST:
                return jsonify({"message": "Already on the list"}), 200
            EARLY_ACCESS_LIST[email] = time.strftime("%Y-%m-%d %H:%M:%S")
            return jsonify({"message": "Added to early access list"}), 201
    else:
        if len(EARLY_ACCESS_LIST) >= 5000:
            return jsonify({"error": "List is full"}), 429
        if email in EARLY_ACCESS_LIST:
            return jsonify({"message": "Already on the list"}), 200
        EARLY_ACCESS_LIST[email] = time.strftime("%Y-%m-%d %H:%M:%S")
        return jsonify({"message": "Added to early access list"}), 201

@app.route("/admin/stats", methods=["GET"])
def admin_stats():
    email = verify_google(request)
    if not email or email.lower() != OWNER_EMAIL:
        return jsonify({"error": "Unauthorized"}), 403
    today = time.strftime("%Y-%m-%d")
    
    if SUPABASE_ENABLED:
        try:
            users_data = supabase.table("users").select("*").order("last_seen", desc=True).limit(300).execute()
            early_data = supabase.table("early_access").select("*").order("created_at", desc=True).execute()
            users_list = [(u["email"], {"first": u["first_seen"], "last": u["last_seen"], "visits": u["visit_count"], "analyses": u["analyses_count"]}) for u in (users_data.data or [])]
            new_today = sum(1 for u in (users_data.data or []) if u["first_seen"].startswith(today))
            analyses_total = sum(u["analyses_count"] for u in (users_data.data or []))
            early_list = [(e["email"], e["created_at"][:10]) for e in (early_data.data or [])]
            return jsonify({
                "totalUsers": len(users_data.data or []),
                "newToday": new_today,
                "analysesTotal": analyses_total,
                "earlyAccessTotal": len(early_data.data or []),
                "users": [{"email": e[0], **{"first": e[1]["first"], "last": e[1]["last"], "visits": e[1]["visits"], "analyses": e[1]["analyses"]}} for e in users_list],
                "earlyAccess": early_list,
                "note": "Data persisted in Supabase — survives redeploys!"
            })
        except Exception as e:
            print(f"SUPABASE: admin_stats failed: {e} — falling back to local", flush=True)
    
    # Fallback to in-memory if Supabase unavailable
    users_list = sorted(USERS.items(), key=lambda x: x[1]["last"], reverse=True)[:300]
    new_today = sum(1 for _, u in USERS.items() if u["first"].startswith(today))
    analyses_total = sum(u["analyses"] for u in USERS.values())
    return jsonify({
        "totalUsers": len(USERS),
        "newToday": new_today,
        "analysesTotal": analyses_total,
        "earlyAccessTotal": len(EARLY_ACCESS_LIST),
        "users": [{"email": e, **u} for e, u in users_list],
        "earlyAccess": sorted(EARLY_ACCESS_LIST.items(), key=lambda x: x[1], reverse=True),
        "note": "In-memory fallback (Supabase unavailable)"
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
    track_user(email)
    tier = get_tier(email)
    used, limit = quota_status(email, tier)
    return jsonify({"email": email, "tier": tier, "used": used, "limit": limit})

@app.route("/listing", methods=["POST"])
def listing():
    email = verify_google(request)
    if not email:
        return jsonify({"error": "Sign in required"}), 401
    tier = get_tier(email)
    ok, used, limit = use_quota(email, tier)
    if not ok:
        return jsonify({"error": f"You've used all {limit} analyses on your {tier.title()} plan this month.", "upgrade": True}), 429

    d = request.json or {}
    name = str(d.get("name", ""))[:250]
    sell = str(d.get("sell", "eBay"))[:100]
    
    # Validate marketplace
    if sell not in VALID_MARKETPLACES:
        return jsonify({"error": f"Invalid marketplace. Must be one of: {', '.join(VALID_MARKETPLACES)}"}), 400
    cost = d.get("cost") or 0
    resell = d.get("resell") or 0
    if not name:
        return jsonify({"error": "Missing product name"}), 400

    prompt = f"""Product: "{name}"
Marketplace: {sell}
Their cost: ${cost} | Target resell: ${resell}

You are an expert reseller copywriter. Create a complete ready-to-post listing pack for this exact product on {sell}. Use that marketplace's real SEO and buyer psychology.

Return ONLY raw JSON no markdown no backticks:
{{
  "title": "SEO-optimized listing title, max 80 chars, keyword-rich the way {sell} search actually ranks",
  "description": "compelling 4-6 sentence sales description with a hook, benefits, condition/shipping note, and call to action. Use line breaks with \\n where natural.",
  "itemSpecifics": ["5-7 short searchable attributes like brand/color/size/material/style"],
  "hashtags": ["8-12 platform-appropriate hashtags without # symbol"],
  "pricing": {{
    "listPrice": 29.99,
    "anchorPrice": 39.99,
    "floorPrice": 22.00,
    "strategy": "one sentence on the pricing psychology used"
  }},
  "negotiationReplies": [
    {{"situation": "Lowball offer", "reply": "polite firm response template"}},
    {{"situation": "Is this available?", "reply": "response that pushes to close"}},
    {{"situation": "Will you do $X less?", "reply": "counter-offer template that protects margin"}}
  ]
}}"""
    try:
        res = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"Content-Type": "application/json", "anthropic-version": "2023-06-01", "x-api-key": CLAUDE_KEY},
            json={"model": "claude-sonnet-4-6", "max_tokens": 1200,
                  "system": "You are an expert marketplace listing copywriter. Output ONLY raw JSON, no markdown.",
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=90
        )
        result = res.json()
        if "error" in result:
            return jsonify({"error": result["error"]["message"]}), 500
        text = result["content"][0]["text"]
        return jsonify(json.loads(text[text.index("{"):text.rindex("}")+1]))
    except Exception as e:
        return jsonify({"error": str(e)}), 500

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

    track_user(email, analysis=True)
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
