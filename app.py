from flask import Flask, request, jsonify
from flask_cors import CORS
import requests
import os
import json
import sys
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
        # Use ScraperAPI to fetch the page
        payload = {
            'api_key': SCRAPERAPI_KEY,
            'url': url,
        }
        
        scraperapi_url = 'https://api.scraperapi.com'
        sys.stdout.write(f"Requesting: {scraperapi_url} with URL: {url[:80]}\n")
        sys.stdout.flush()
        
        response = requests.get(scraperapi_url, params=payload, timeout=30)
        sys.stdout.write(f"Response status: {response.status_code}\n")
        sys.stdout.flush()
        
        if response.status_code != 200:
            return jsonify({"error": f"ScraperAPI error: {response.status_code}"}), 500
        
        soup = BeautifulSoup(response.text, 'html.parser')
        
        title = ""
        image = ""
        price = ""
        rating = ""
        
        # Extract title
        og_title = soup.find('meta', property='og:title')
        if og_title:
            title = og_title.get('content', '')
        if not title:
            title_tag = soup.find('h1')
            if title_tag:
                title = title_tag.get_text(strip=True)[:200]
        
        # Extract image
        og_image = soup.find('meta', property='og:image')
        if og_image:
            image = og_image.get('content', '')
        if not image:
            img_tag = soup.find('img')
            if img_tag:
                image = img_tag.get('src', '')
        
        # Clean up relative URLs
        if image and not image.startswith('http'):
            from urllib.parse import urljoin
            image = urljoin(url, image)
        
        # Extract price - try multiple methods for Amazon and AliExpress
        og_price = soup.find('meta', property='product:price:amount')
        if og_price:
            price = og_price.get('content', '')
        
        # If still no price, search page text for price patterns
        if not price:
            all_text = soup.get_text()
            price_matches = re.findall(r'\$\s*([\d,]+\.?\d{0,2})', all_text)
            if price_matches:
                for p in price_matches:
                    p_clean = p.replace(',', '')
                    try:
                        p_float = float(p_clean)
                        # Only accept reasonable prices (between $0.99 and $9999)
                        if 0.99 <= p_float <= 9999:
                            price = p_clean
                            break
                    except:
                        pass
        
        # Try AliExpress specific price extraction if still no price
        if not price:
            price_span = soup.find('span', class_=re.compile(r'price|cost|amount', re.I))
            if price_span:
                price_match = re.search(r'([\d,]+\.?\d{0,2})', price_span.get_text())
                if price_match:
                    price = price_match.group(1).replace(',', '')
        
        # Extract rating
        og_rating = soup.find('meta', property='product:rating')
        if og_rating:
            rating = og_rating.get('content', '')
        
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
        sys.stdout.write(f"ERROR: {str(e)}\n")
        sys.stdout.flush()
        return jsonify({"error": f"Failed to fetch: {str(e)}"}), 500

@app.route("/analyze", methods=["POST"])
def analyze():
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
                "system": "You are a professional reselling and Amazon FBA analyst. Output ONLY raw JSON, no markdown, no backticks.",
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
