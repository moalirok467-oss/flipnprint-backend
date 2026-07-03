from flask import Flask, request, jsonify
from flask_cors import CORS
import requests
import os
import json

app = Flask(__name__)
CORS(app)

CANOPY_KEY = os.environ.get("CANOPY_KEY")
CLAUDE_KEY = os.environ.get("CLAUDE_KEY")

@app.route("/")
def home():
    return "FlipNPrint backend running"

@app.route("/test-canopy")
def test_canopy():
    asin = request.args.get("asin", "B0B3JBVDYP")
    try:
        res = requests.get(
            f"https://api.canopyapi.co/v1/amazon/product?asin={asin}",
            headers={"Authorization": f"Bearer {CANOPY_KEY}"},
            timeout=15
        )
        return jsonify({"status_code": res.status_code, "raw": res.json()})
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
            f"https://api.canopyapi.co/v1/amazon/product?asin={asin}",
            headers={"Authorization": f"Bearer {CANOPY_KEY}"},
            timeout=15
        )
        raw = res.json()
        product = raw.get("data", {}).get("amazonProduct", {}) if "data" in raw else raw

        title = product.get("title", "Unknown Product")
        image = product.get("mainImageUrl", "")
        if not image:
            urls = product.get("imageUrls", [])
            image = urls[0] if urls else ""

        price_obj = product.get("price", {})
        price = price_obj.get("value", 0) if isinstance(price_obj, dict) else price_obj

        rating = product.get("rating", 0)
        reviews = product.get("ratingsTotal", 0)

        categories = product.get("categories", [])
        category = categories[0].get("name", "") if categories else ""

        brand = product.get("brand", "")

        return jsonify({
            "title": title,
            "image": image,
            "price": float(price) if price else 0,
            "rating": float(rating) if rating else 0,
            "reviews": int(reviews) if reviews else 0,
            "category": category,
            "brand": brand,
            "asin": asin
        })

    except Exception as e:
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
