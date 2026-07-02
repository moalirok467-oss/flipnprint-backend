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

@app.route("/fetch-product", methods=["POST"])
def fetch_product():
    data = request.json
    asin = data.get("asin")
    if not asin:
        return jsonify({"error": "No ASIN provided"}), 400
    try:
        res = requests.get(
            "https://rest.canopyapi.co/api/amazon/product",
            params={"asin": asin},
            headers={
                "Authorization": f"Bearer {CANOPY_KEY}",
                "API-KEY": CANOPY_KEY
            },
            timeout=15
        )
        product = res.json()
        print("Canopy response:", json.dumps(product)[:500])

        title = product.get("title") or product.get("product_title") or product.get("name") or "Unknown Product"
        image = ""
        if product.get("main_image"):
            image = product["main_image"].get("url", "") if isinstance(product["main_image"], dict) else product["main_image"]
        if not image and product.get("images"):
            imgs = product["images"]
            image = imgs[0].get("url", "") if isinstance(imgs[0], dict) else imgs[0]

        price = product.get("price") or product.get("buybox_price") or 0
        if isinstance(price, dict):
            price = price.get("value") or price.get("amount") or 0

        rating = product.get("rating") or product.get("stars") or 0
        reviews = product.get("ratings_total") or product.get("review_count") or product.get("num_ratings") or 0
        category = product.get("category") or product.get("breadcrumb") or ""

        return jsonify({
            "title": title,
            "image": image,
            "price": float(price) if price else 0,
            "rating": float(rating) if rating else 0,
            "reviews": int(reviews) if reviews else 0,
            "category": str(category),
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
