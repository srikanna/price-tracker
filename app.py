"""
Flask Price Tracker Web Application.
Provides a modern web UI, Firestore persistence, and a secure scraping endpoint.
"""

import logging
import os
from datetime import datetime, timezone
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify
from scraper import scrape_product
import db

# Setup logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "price-tracker-secret-key-change-in-prod")

# Secure token for triggering the automated batch scraper
CRON_SECRET = os.environ.get("CRON_SECRET", "super-secret-price-tracker-cron-key-12345")


@app.template_filter("format_currency")
def format_currency_filter(value, symbol="$"):
    if value is None:
        return "N/A"
    try:
        return f"{symbol}{float(value):,.2f}"
    except (ValueError, TypeError):
        return f"{symbol}{value}"


@app.template_filter("format_date")
def format_date_filter(val):
    if not val:
        return "Never"
    if isinstance(val, str):
        try:
            val = datetime.fromisoformat(val.replace("Z", "+00:00"))
        except Exception:
            return val
    if hasattr(val, "strftime"):
        return val.strftime("%b %d, %Y %H:%M UTC")
    return str(val)


@app.route("/", methods=["GET"])
def index():
    """Main dashboard displaying tracked products with filtering and statistics."""
    products = db.get_all_products()

    # Query params for filtering
    filter_type = request.args.get("filter", "all").lower()
    selected_site = request.args.get("site", "all")
    search_query = request.args.get("q", "").strip().lower()

    # Calculate summary metrics across all products
    total_products = len(products)
    valid_prices = [p.get("current_price") for p in products if p.get("current_price") is not None]
    avg_price = (sum(valid_prices) / len(valid_prices)) if valid_prices else 0.0

    price_drops = sum(1 for p in products if (p.get("recent_change", 0) < 0 or p.get("overall_change", 0) < 0))
    price_hikes = sum(1 for p in products if (p.get("recent_change", 0) > 0 or p.get("overall_change", 0) > 0))
    all_time_lows = sum(1 for p in products if p.get("is_all_time_low"))

    # Extract all unique retailer names
    sites = sorted(list({p.get("site_name", "Retailer") for p in products if p.get("site_name")}))

    # Apply filters
    filtered_products = products
    if filter_type == "drops":
        filtered_products = [p for p in filtered_products if (p.get("recent_change", 0) < 0 or p.get("overall_change", 0) < 0)]
    elif filter_type == "hikes":
        filtered_products = [p for p in filtered_products if (p.get("recent_change", 0) > 0 or p.get("overall_change", 0) > 0)]
    elif filter_type == "lows":
        filtered_products = [p for p in filtered_products if p.get("is_all_time_low")]

    if selected_site != "all":
        filtered_products = [p for p in filtered_products if p.get("site_name", "").lower() == selected_site.lower()]

    if search_query:
        filtered_products = [
            p for p in filtered_products
            if search_query in p.get("name", "").lower() or search_query in p.get("url", "").lower() or search_query in p.get("site_name", "").lower()
        ]

    return render_template(
        "index.html",
        products=filtered_products,
        total_products=total_products,
        avg_price=avg_price,
        price_drops=price_drops,
        price_hikes=price_hikes,
        all_time_lows=all_time_lows,
        sites=sites,
        filter_type=filter_type,
        selected_site=selected_site,
        search_query=search_query,
    )


@app.route("/track", methods=["POST"])
def track():
    """Handles submission of a new product URL to track."""
    url = request.form.get("url", "").strip()
    if not url:
        flash("Please provide a valid URL.", "error")
        return redirect(url_for("index"))

    logger.info(f"Scraping new URL: {url}")
    scraped = scrape_product(url)

    if scraped.get("error") and not scraped.get("name"):
        flash(f"Scraping failed: {scraped['error']}", "error")
        return redirect(url_for("index"))

    # Save to Firestore
    saved = db.save_or_update_product(
        url=scraped["url"],
        name=scraped["name"] or "Tracked Product",
        price=scraped["price"],
        currency=scraped.get("currency", "$"),
        image_url=scraped.get("image_url"),
    )

    if scraped.get("price") is not None:
        flash(f"Successfully tracked '{saved['name']}' at {saved['currency']}{saved['current_price']:.2f}!", "success")
    else:
        flash(f"Added '{saved['name']}', but price could not be automatically detected. We'll re-check on the next cycle.", "warning")

    return redirect(url_for("index"))


@app.route("/product/<product_id>", methods=["GET"])
def product_detail(product_id):
    """Displays detailed price history and charts for a single product."""
    product = db.get_product(product_id)
    if not product:
        flash("Product not found.", "error")
        return redirect(url_for("index"))

    history = db.get_price_history(product_id)
    return render_template("detail.html", product=product, history=history)


@app.route("/product/<product_id>/refresh", methods=["POST"])
def refresh_product(product_id):
    """Forces an immediate scrape and price refresh for a single product."""
    product = db.get_product(product_id)
    if not product:
        flash("Product not found.", "error")
        return redirect(url_for("index"))

    logger.info(f"Manual refresh requested for {product_id}: {product.get('url')}")
    scraped = scrape_product(product["url"])

    if scraped.get("price") is not None:
        db.save_or_update_product(
            url=product["url"],
            name=scraped.get("name") or product.get("name"),
            price=scraped["price"],
            currency=scraped.get("currency", product.get("currency", "$")),
            image_url=scraped.get("image_url") or product.get("image_url"),
        )
        flash(f"Refreshed '{product.get('name')}': Current price is {scraped.get('currency', '$')}{scraped['price']:.2f}", "success")
    else:
        flash(f"Could not retrieve updated price: {scraped.get('error', 'Unknown error')}", "error")

    # Return to previous page or detail
    return redirect(request.referrer or url_for("product_detail", product_id=product_id))


@app.route("/product/<product_id>/delete", methods=["POST"])
def delete_product(product_id):
    """Deletes a tracked product and its history."""
    product = db.get_product(product_id)
    name = product.get("name", "Product") if product else "Product"
    db.delete_product(product_id)
    flash(f"Removed '{name}' from tracking.", "info")
    return redirect(url_for("index"))


@app.route("/api/scrape-all", methods=["POST"])
def scrape_all():
    """
    Secure endpoint triggered by Google Cloud Scheduler to update all tracked URLs.
    Protected by CRON_SECRET or Google Cloud Scheduler service account identity.
    """
    # Security check: verify Bearer token, X-Cron-Secret header, or GCP OIDC token
    auth_header = request.headers.get("Authorization", "")
    secret_header = request.headers.get("X-Cron-Secret", "")
    is_scheduler = request.headers.get("X-CloudScheduler", "")

    authorized = False

    # Check X-Cron-Secret or Bearer CRON_SECRET
    if secret_header and secret_header == CRON_SECRET:
        authorized = True
    elif auth_header.startswith("Bearer "):
        token = auth_header.split(" ", 1)[1].strip()
        if token == CRON_SECRET:
            authorized = True
        else:
            # Check if this is a Google Cloud OIDC token
            # When Cloud Scheduler calls Cloud Run with an OIDC service account token,
            # Cloud Run validates it or the app can verify it with google.oauth2.id_token
            try:
                from google.oauth2 import id_token
                from google.auth.transport import requests as google_requests
                id_token.verify_oauth2_token(token, google_requests.Request())
                authorized = True
            except Exception as e:
                logger.warning(f"OIDC token verification failed: {e}")

    # For development/convenience if CRON_SECRET is empty
    if not CRON_SECRET:
        authorized = True

    if not authorized:
        logger.warning("Unauthorized access attempt to /api/scrape-all")
        return jsonify({"error": "Unauthorized. Provide valid X-Cron-Secret or Bearer token."}), 401

    products = db.get_all_products()
    results = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "total": len(products),
        "updated": 0,
        "failed": 0,
        "details": [],
    }

    logger.info(f"Starting scheduled scrape for {len(products)} products...")

    for prod in products:
        url = prod.get("url")
        prod_id = prod.get("id")
        try:
            scraped = scrape_product(url)
            if scraped.get("price") is not None:
                updated = db.save_or_update_product(
                    url=url,
                    name=scraped.get("name") or prod.get("name"),
                    price=scraped["price"],
                    currency=scraped.get("currency", prod.get("currency", "$")),
                    image_url=scraped.get("image_url") or prod.get("image_url"),
                )
                results["updated"] += 1
                results["details"].append({
                    "id": prod_id,
                    "name": updated.get("name"),
                    "price": updated.get("current_price"),
                    "status": "success",
                })
            else:
                results["failed"] += 1
                results["details"].append({
                    "id": prod_id,
                    "name": prod.get("name"),
                    "status": "price_not_found",
                    "error": scraped.get("error"),
                })
        except Exception as e:
            logger.error(f"Error scraping {url}: {e}")
            results["failed"] += 1
            results["details"].append({
                "id": prod_id,
                "name": prod.get("name"),
                "status": "exception",
                "error": str(e),
            })

    logger.info(f"Completed scheduled scrape: {results['updated']} updated, {results['failed']} failed.")
    return jsonify(results), 200


@app.route("/health", methods=["GET"])
def health():
    """Health check for Cloud Run."""
    return jsonify({"status": "healthy", "service": "price-tracker"}), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=True)
