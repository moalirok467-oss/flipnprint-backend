from flask import Flask, request, jsonify
from flask_cors import CORS
import requests
from bs4 import BeautifulSoup
import re
import os
import json

app = Flask(__name__)
CORS(app)

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
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        }
        res = requests.get(url, headers=headers, timeout=15)
        res.raise_for_status()
        soup = BeautifulSoup(res.content, 'html.parser')
        
        # Try to extract from meta tags (works for most sites)
        title = ""
        image = ""
        price = ""
        rating = ""
        
        # Meta tags
        og_title = soup.find('meta', property='og:title')
        if og_title:
            title = og_title.get('content', '')
        
        og_image = soup.find('meta', property='og:image')
        if og_image:
            image = og_image.get('content', '')
        
        og_price = soup.find('meta', property='product:price:amount')
        if og_price:
            price = og_price.get('content', '')
        
        # If meta tags didn't work, try scraping from page content
        if not title:
            title_tag = soup.find('h1')
            if title_tag:
                title = title_tag.get_text(strip=True)[:200]
        
        if not image:
            img_tag = soup.find('img')
            if img_tag:
                image = img_tag.get('src', '')
                if image.startswith('/'):
                    from urllib.parse import urljoin
                    image = urljoin(url, image)
        
        # Extract price from text (look for common patterns)
        if not price:
            price_text = soup.find(string=re.compile(r'\$\d+'))
            if price_text:
                match = re.search(r'\$?([\d,]+\.?\d*)', str(price_text))
                if match:
                    price = match.group(1).replace(',', '')
        
        # Extract rating if available
        rating_text = soup.find(string=re.compile(r'(\d+\.?\d*)\s*out of|★'))
        if rating_text:
            match = re.search(r'(\d+\.?\d*)', str(rating_text))
            if match:
                rating = match.group(1)
        
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
