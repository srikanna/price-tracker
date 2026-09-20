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


from notifier import send_google_chat_alert, send_test_google_chat_alert


@app.route("/", methods=["GET"])
def index():
    """Main dashboard displaying tracked products with filtering and statistics."""
    all_products = db.get_all_products(include_archived=True)
    global_settings = db.get_global_settings()

    # Query params for filtering
    filter_type = request.args.get("filter", "active").lower()
    selected_site = request.args.get("site", "all")
    search_query = request.args.get("q", "").strip().lower()

    # Calculate summary metrics across products
    total_products = len(all_products)
    archived_count = sum(1 for p in all_products if p.get("is_archived"))
    active_count = total_products - archived_count

    active_products = [p for p in all_products if not p.get("is_archived")]
    valid_prices = [p.get("current_price") for p in active_products if p.get("current_price") is not None]
    avg_price = (sum(valid_prices) / len(valid_prices)) if valid_prices else 0.0

    price_drops = sum(1 for p in active_products if (p.get("recent_change", 0) < 0 or p.get("overall_change", 0) < 0))
    price_hikes = sum(1 for p in active_products if (p.get("recent_change", 0) > 0 or p.get("overall_change", 0) > 0))
    all_time_lows = sum(1 for p in active_products if p.get("is_all_time_low"))

    # Extract all unique retailer names
    sites = sorted(list({p.get("site_name", "Retailer") for p in all_products if p.get("site_name")}))

    # Determine baseline products for current tab
    if filter_type == "archived":
        filtered_products = [p for p in all_products if p.get("is_archived")]
    elif filter_type == "all":
        filtered_products = all_products
    else:
        # Default views: active products only
        filtered_products = active_products

    # Apply status filters
    if filter_type == "drops":
        filtered_products = [p for p in filtered_products if (p.get("recent_change", 0) < 0 or p.get("overall_change", 0) < 0)]
    elif filter_type == "hikes":
        filtered_products = [p for p in filtered_products if (p.get("recent_change", 0) > 0 or p.get("overall_change", 0) > 0)]
    elif filter_type == "lows":
        filtered_products = [p for p in filtered_products if p.get("is_all_time_low")]

    # Apply site filter
    if selected_site != "all":
        filtered_products = [p for p in filtered_products if p.get("site_name", "").lower() == selected_site.lower()]

    # Apply search keyword filter
    if search_query:
        filtered_products = [
            p for p in filtered_products
            if search_query in p.get("name", "").lower() or search_query in p.get("url", "").lower() or search_query in p.get("site_name", "").lower()
        ]

    return render_template(
        "index.html",
        products=filtered_products,
        total_products=total_products,
        active_count=active_count,
        archived_count=archived_count,
        avg_price=avg_price,
        price_drops=price_drops,
        price_hikes=price_hikes,
        all_time_lows=all_time_lows,
        sites=sites,
        filter_type=filter_type,
        selected_site=selected_site,
        search_query=search_query,
        google_chat_webhook=global_settings.get("google_chat_webhook", ""),
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
        flash(f"Successfully tracking '{saved['name']}' from {saved['site_name']} at {saved['currency']}{saved['current_price']:.2f}!", "success")
    else:
        flash(f"Added '{saved['name']}' ({saved['site_name']}), but price could not be automatically detected. We'll re-check on the next cycle.", "warning")

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


@app.route("/product/<product_id>/toggle-alert", methods=["POST"])
def toggle_alert(product_id):
    """Toggles price drop notifications on/off for a single product."""
    new_state = db.toggle_product_alert(product_id)
    state_str = "enabled 🔔" if new_state else "disabled 🔕"
    flash(f"Price drop alerts {state_str} for this item.", "info")
    return redirect(request.referrer or url_for("index"))


@app.route("/product/<product_id>/toggle-archive", methods=["POST"])
def toggle_archive(product_id):
    """Archives or unarchives a product (pausing/resuming background tracking)."""
    new_state = db.toggle_product_archive(product_id)
    if new_state:
        flash("Product moved to Archive. Background tracking paused (history is preserved).", "info")
    else:
        flash("Product unarchived! Resumed background price tracking.", "success")
    return redirect(request.referrer or url_for("index"))


@app.route("/settings/google-chat", methods=["POST"])
def save_chat_settings():
    """Saves the Google Chat Space webhook URL."""
    webhook = request.form.get("webhook_url", "").strip()
    db.save_global_settings({"google_chat_webhook": webhook})
    flash("Google Chat Webhook settings saved successfully!", "success")
    return redirect(request.referrer or url_for("index"))


@app.route("/test-google-chat", methods=["POST"])
def test_chat():
    """Sends a sample Card v2 to verify the configured Google Chat Space."""
    settings = db.get_global_settings()
    webhook = settings.get("google_chat_webhook")
    if not webhook:
        flash("Please enter and save a Google Chat Webhook URL first.", "error")
        return redirect(request.referrer or url_for("index"))

    success = send_test_google_chat_alert(webhook)
    if success:
        flash("Test alert card delivered successfully to your Google Chat Space! 🚀", "success")
    else:
        flash("Failed to send test alert. Please check your Google Chat Webhook URL.", "error")
    return redirect(request.referrer or url_for("index"))


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
        old_price = product.get("current_price")
        updated = db.save_or_update_product(
            url=product["url"],
            name=scraped.get("name") or product.get("name"),
            price=scraped["price"],
            currency=scraped.get("currency", product.get("currency", "$")),
            image_url=scraped.get("image_url") or product.get("image_url"),
        )
        flash(f"Refreshed '{product.get('name')}': Current price is {scraped.get('currency', '$')}{scraped['price']:.2f}", "success")

        # Check if price dropped and alerts enabled
        if product.get("alerts_enabled") and not product.get("is_archived") and old_price and scraped["price"] < old_price:
            settings = db.get_global_settings()
            webhook = settings.get("google_chat_webhook")
            if webhook:
                send_google_chat_alert(webhook, updated)
                db.update_last_alerted_price(product_id, scraped["price"])
    else:
        flash(f"Could not retrieve updated price: {scraped.get('error', 'Unknown error')}", "error")

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
    Secure endpoint triggered by Google Cloud Scheduler to update all active tracked URLs.
    Protected by CRON_SECRET or Google Cloud Scheduler service account identity.
    Dispatches Google Chat alerts when prices drop on alert-enabled items.
    """
    auth_header = request.headers.get("Authorization", "")
    secret_header = request.headers.get("X-Cron-Secret", "")

    authorized = False

    if secret_header and secret_header == CRON_SECRET:
        authorized = True
    elif auth_header.startswith("Bearer "):
        token = auth_header.split(" ", 1)[1].strip()
        if token == CRON_SECRET:
            authorized = True
        else:
            try:
                from google.oauth2 import id_token
                from google.auth.transport import requests as google_requests
                id_token.verify_oauth2_token(token, google_requests.Request())
                authorized = True
            except Exception as e:
                logger.warning(f"OIDC token verification failed: {e}")

    if not CRON_SECRET:
        authorized = True

    if not authorized:
        logger.warning("Unauthorized access attempt to /api/scrape-all")
        return jsonify({"error": "Unauthorized. Provide valid X-Cron-Secret or Bearer token."}), 401

    # Fetch only active products (skipping archived products)
    products = db.get_all_products(include_archived=False)
    settings = db.get_global_settings()
    chat_webhook = settings.get("google_chat_webhook")

    results = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "total_active": len(products),
        "updated": 0,
        "failed": 0,
        "alerts_sent": 0,
        "details": [],
    }

    logger.info(f"Starting scheduled scrape for {len(products)} active products...")

    for prod in products:
        url = prod.get("url")
        prod_id = prod.get("id")
        old_price = prod.get("current_price")
        alerts_enabled = prod.get("alerts_enabled", True)

        try:
            scraped = scrape_product(url)
            if scraped.get("price") is not None:
                new_price = scraped["price"]
                updated = db.save_or_update_product(
                    url=url,
                    name=scraped.get("name") or prod.get("name"),
                    price=new_price,
                    currency=scraped.get("currency", prod.get("currency", "$")),
                    image_url=scraped.get("image_url") or prod.get("image_url"),
                )
                results["updated"] += 1

                # Check for price drop alert
                alert_sent = False
                if alerts_enabled and old_price is not None and new_price < old_price and chat_webhook:
                    last_alerted = prod.get("last_alerted_price")
                    if last_alerted is None or new_price < last_alerted:
                        sent = send_google_chat_alert(chat_webhook, updated)
                        if sent:
                            alert_sent = True
                            results["alerts_sent"] += 1
                            db.update_last_alerted_price(prod_id, new_price)

                results["details"].append({
                    "id": prod_id,
                    "name": updated.get("name"),
                    "site_name": updated.get("site_name"),
                    "price": updated.get("current_price"),
                    "alert_sent": alert_sent,
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

    logger.info(f"Completed scheduled scrape: {results['updated']} updated, {results['failed']} failed, {results['alerts_sent']} alerts sent.")
    return jsonify(results), 200


@app.route("/health", methods=["GET"])
def health():
    """Health check for Cloud Run."""
    return jsonify({"status": "healthy", "service": "price-tracker"}), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=True)
