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
            f"https://api.canopyapi.co/v1/products/{asin}",
            headers={"Authorization": f"Bearer {CANOPY_KEY}"},
            timeout=10
        )
        product = res.json()
        title = product.get("title") or product.get("product_title") or product.get("name") or "Unknown Product"
        image = (product.get("main_image") or {}).get("url") or ""
        if not image and product.get("images"):
            image = product["images"][0].get("url", "")
        price = product.get("price", {})
        if isinstance(price, dict):
            price = price.get("value") or price.get("amount") or 0
        rating = product.get("rating") or product.get("stars") or 0
        reviews = product.get("ratings_total") or product.get("review_count") or 0
        category = product.get("category") or ""
        return jsonify({"title": title, "image": image, "price": price, "rating": rating, "reviews": reviews, "category": category, "asin": asin})
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
            headers={"Content-Type": "application/json", "anthropic-version": "2023-06-01", "x-api-key": CLAUDE_KEY},
            json={"model": "claude-sonnet-4-6", "max_tokens": 1500, "system": "You are a professional reselling and Amazon FBA analyst. Output ONLY raw JSON, no markdown, no backticks.", "messages": [{"role": "user", "content": prompt}]},
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
