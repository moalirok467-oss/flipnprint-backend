from flask import Flask, request, jsonify
from flask_cors import CORS
import requests
import os
import json
import re
from bs4 import BeautifulSoup

app = Flask(__name__)
CORS(app)

SCRAPERAPI_KEY = os.environ.get("SCRAPERAPI_KEY")
CLAUDE_KEY = os.environ.get("CLAUDE_KEY")

@app.route("/")
def home():
    return "FlipNPrint backend running"

@app.route("/fetch-product", methods=["POST"])
def fetch_product():
    data = request.json
    url = data.get("url")
    if not url:
        return jsonify({"error": "No URL provided"}), 400
    
    try:
        payload = {
            'api_key': SCRAPERAPI_KEY,
            'url': url,
        }
        
        response = requests.get('https://api.scraperapi.com', params=payload, timeout=30)
        
        if response.status_code != 200:
            return jsonify({"error": f"ScraperAPI error: {response.status_code}"}), 500
        
        soup = BeautifulSoup(response.text, 'html.parser')
        
        title = ""
        image = ""
        price = ""
        rating = ""
        
        # Extract title - try meta tag first, then h1, then AliExpress specific
        og_title = soup.find('meta', property='og:title')
        if og_title:
            title = og_title.get('content', '')
        if not title:
            title_tag = soup.find('h1')
            if title_tag:
                title = title_tag.get_text(strip=True)[:200]
        if not title:
            # Try AliExpress specific title selectors
            title_div = soup.find('div', class_=re.compile(r'ProductTitle|product-title', re.I))
            if title_div:
                title = title_div.get_text(strip=True)[:200]
        
        # Extract image - try meta tag first, then img
        og_image = soup.find('meta', property='og:image')
        if og_image:
            image = og_image.get('content', '')
        if not image:
            img_tag = soup.find('img')
            if img_tag:
                image = img_tag.get('src', '')
        
        # Fix relative URLs
        if image and not image.startswith('http'):
            from urllib.parse import urljoin
            image = urljoin(url, image)
        
        # Extract price - AMAZON SPECIFIC
        # Look for the actual price element on Amazon
        price_span = soup.find('span', class_='a-price-whole')
        if price_span:
            price_text = price_span.get_text(strip=True)
            match = re.search(r'[\d,]+\.?\d{0,2}', price_text)
            if match:
                price = match.group(0).replace(',', '')
        
        # If not found, try other Amazon price patterns
        if not price:
            price_span = soup.find('span', class_=re.compile(r'a-price'))
            if price_span:
                price_text = price_span.get_text(strip=True)
                match = re.search(r'[\d,]+\.?\d{0,2}', price_text)
                if match:
                    price = match.group(0).replace(',', '')
        
        # Last resort - search all text but only accept reasonable prices
        if not price:
            all_text = soup.get_text()
            price_matches = re.findall(r'\$\s*?([\d,]+\.?\d{0,2})', all_text)
            if price_matches:
                for p in price_matches:
                    p_clean = p.replace(',', '')
                    try:
                        p_float = float(p_clean)
                        # Only accept prices between $2 and $500
                        if 2 <= p_float <= 500:
                            price = p_clean
                            break
                    except:
                        pass
        
        # AliExpress specific price extraction
        if not price:
            price_span = soup.find('span', class_=re.compile(r'price|cost', re.I))
            if price_span:
                price_text = price_span.get_text(strip=True)
                match = re.search(r'[\d,]+\.?\d{0,2}', price_text)
                if match:
                    price = match.group(0).replace(',', '')
        
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

@app.route("/analyze", methods=["POST"])
def analyze():
    # Verify Google sign-in token
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return jsonify({"error": "Sign in required"}), 401
    token = auth_header.split(" ", 1)[1]
    try:
        from google.oauth2 import id_token as google_id_token
        from google.auth.transport import requests as google_requests
        GOOGLE_CLIENT_ID = "536188060764-lsuk40m1vj4k8lnuu3go4iearvc0bpnn.apps.googleusercontent.com"
        user_info = google_id_token.verify_oauth2_token(
            token, google_requests.Request(), GOOGLE_CLIENT_ID
        )
        user_email = user_info.get("email", "unknown")
        print(f"Analysis by: {user_email}")
    except Exception:
        return jsonify({"error": "Invalid or expired sign-in"}), 401

    data = request.json
    prompt = data.get("prompt")
    if not prompt:
        return jsonify({"error": "No prompt"}), 400
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
            timeout=30
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
