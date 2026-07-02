from flask import Flask, request, jsonify
from flask_cors import CORS
import requests
import os
import json
import sys

app = Flask(__name__)
CORS(app)

CANOPY_KEY = os.environ.get("CANOPY_KEY")
CLAUDE_KEY = os.environ.get("CLAUDE_KEY")

@app.route("/")
def home():
    return "FlipNPrint backend running"

@app.route("/test-canopy")
def test_canopy():
    """GET this in browser to see raw Canopy response"""
    try:
        res = requests.get(
            "https://rest.canopyapi.co/api/amazon/product",
            params={"asin": "B0B3JBVDYP", "domain": "US"},
            headers={"API-KEY": CANOPY_KEY, "Content-Type": "application/json"},
            timeout=15
        )
        return jsonify({
            "status_code": res.status_code,
            "raw": res.json()
        })
    except Exception as e:
        return jsonify({"error": str(e)})

@app.route("/fetch-product", methods=["POST"])
def fetch_product():
    data = request.json
    asin = data.get("asin")
    if not asin:
        return jsonify({"error": "No ASIN provided"}), 400
    try:
        res = requests.get(
            "https://rest.canopyapi.co/api/amazon/product",
            params={"asin": asin, "domain": "US"},
            headers={"API-KEY": CANOPY_KEY, "Content-Type": "application/json"},
            timeout=15
        )
        product = res.json()
        sys.stdout.write("CANOPY: " + json.dumps(product)[:500] + "\n")
        sys.stdout.flush()

        def get_field(obj, *keys):
            for k in keys:
                if k in obj and obj[k]:
                    return obj[k]
            return None

        title = get_field(product, "title", "product_title", "name", "item_name") or "Unknown Product"

        image = ""
        for img_key in ["main_image", "image", "primary_image", "thumbnail"]:
            img = product.get(img_key)
            if img:
                image = img.get("url", img) if isinstance(img, dict) else img
                break
        if not image:
            for imgs_key in ["images", "image_urls", "photos"]:
                imgs = product.get(imgs_key)
                if imgs and len(imgs) > 0:
                    image = imgs[0].get("url", imgs[0]) if isinstance(imgs[0], dict) else imgs[0]
                    break

        price = get_field(product, "price", "buybox_price", "list_price", "current_price") or 0
        if isinstance(price, dict):
            price = get_field(price, "value", "amount", "current") or 0

        rating = get_field(product, "rating", "stars", "average_rating", "star_rating") or 0
        reviews = get_field(product, "ratings_total", "review_count", "num_ratings", "reviews_count", "total_ratings") or 0
        category = get_field(product, "category", "breadcrumb", "department", "product_category") or ""

        return jsonify({
            "title": str(title),
            "image": str(image),
            "price": float(price) if price else 0,
            "rating": float(rating) if rating else 0,
            "reviews": int(str(reviews).replace(",", "")) if reviews else 0,
            "category": str(category),
            "asin": asin
        })

    except Exception as e:
        sys.stdout.write("ERROR: " + str(e) + "\n")
        sys.stdout.flush()
        return jsonify({"error": str(e)}), 500

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
