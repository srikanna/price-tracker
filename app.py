"""
Flask Price Tracker Web Application.
Provides Google OAuth2 authentication, per-user data segmentation,
extensible multi-channel price drop alerts, Firestore persistence,
and an automated batch scraping endpoint for Cloud Scheduler.
"""

import logging
import os
import secrets
from datetime import datetime, timezone
from functools import wraps
from urllib.parse import urlencode

import requests
from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    flash,
    jsonify,
    session,
    g,
)

from scraper import scrape_product
import db
from notifier import send_test_google_chat_alert, dispatch_user_alerts

# Setup logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "price-tracker-secret-key-change-in-prod-v2")

# Secure token for triggering the automated batch scraper
CRON_SECRET = os.environ.get("CRON_SECRET", "super-secret-price-tracker-cron-key-12345")


# ---------------------------------------------------------------------------
# Template Filters & Context Processors
# ---------------------------------------------------------------------------

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


def resolve_current_user():
    """
    Resolves the authenticated user from:
    1. Google Cloud Identity-Aware Proxy (IAP) headers (X-Goog-Authenticated-User-Email, X-Goog-Authenticated-User-Id).
    2. Active Flask session (from Google OAuth or dev sign-in).
    """
    iap_email_header = request.headers.get("X-Goog-Authenticated-User-Email")
    iap_id_header = request.headers.get("X-Goog-Authenticated-User-Id")

    if iap_email_header:
        # e.g. "accounts.google.com:alice@example.com"
        email = iap_email_header.split(":")[-1].strip()
        user_id = iap_id_header.split(":")[-1].strip() if iap_id_header else email

        if email:
            user = db.get_or_create_user({
                "id": user_id,
                "email": email,
                "name": email.split("@")[0].capitalize(),
                "picture": f"https://api.dicebear.com/7.x/bottts/svg?seed={user_id}",
            })
            session["user"] = {
                "id": user["id"],
                "email": user["email"],
                "name": user["name"],
                "picture": user.get("picture", ""),
                "auth_source": "iap",
            }
            return session["user"]

    return session.get("user")


@app.context_processor
def inject_user_and_globals():
    """Injects current logged-in user and OAuth status into all templates."""
    oauth_cfg = db.get_oauth_config()
    has_oauth = bool(oauth_cfg.get("client_id") and oauth_cfg.get("client_secret"))
    user = resolve_current_user()
    return {
        "current_user": user,
        "has_google_oauth": has_oauth,
        "is_iap_active": bool(request.headers.get("X-Goog-Authenticated-User-Email")),
    }


# ---------------------------------------------------------------------------
# Authentication Decorator & Handlers
# ---------------------------------------------------------------------------

def login_required(f):
    """Ensures endpoint is accessible only to authenticated users (via IAP or session)."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        user = resolve_current_user()
        if not user:
            session["next_url"] = request.url
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated_function


@app.route("/login", methods=["GET"])
def login():
    """Displays Sign in with Google page (bypassed if Google IAP is active)."""
    if resolve_current_user():
        return redirect(url_for("index"))

    oauth_cfg = db.get_oauth_config()
    has_oauth = bool(oauth_cfg.get("client_id") and oauth_cfg.get("client_secret"))

    scheme = request.headers.get("X-Forwarded-Proto", request.scheme)
    callback_url = url_for("auth_callback", _external=True, _scheme=scheme)

    return render_template(
        "login.html",
        has_oauth=has_oauth,
        client_id=oauth_cfg.get("client_id", ""),
        callback_url=callback_url,
    )


@app.route("/auth/google", methods=["GET"])
def auth_google():
    """Initiates Google OAuth2 Authorization Code flow."""
    oauth_cfg = db.get_oauth_config()
    client_id = oauth_cfg.get("client_id")
    if not client_id:
        flash("Google Client ID is not configured yet. Please configure OAuth or use Dev Sign-In.", "warning")
        return redirect(url_for("login"))

    scheme = request.headers.get("X-Forwarded-Proto", request.scheme)
    redirect_uri = url_for("auth_callback", _external=True, _scheme=scheme)

    state = secrets.token_urlsafe(32)
    session["oauth_state"] = state

    google_auth_endpoint = "https://accounts.google.com/o/oauth2/v2/auth"
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "access_type": "offline",
        "prompt": "select_account",
    }
    return redirect(f"{google_auth_endpoint}?{urlencode(params)}")


@app.route("/auth/callback", methods=["GET"])
def auth_callback():
    """Handles callback from Google OAuth2."""
    error = request.args.get("error")
    if error:
        flash(f"Google Sign-In failed: {error}", "error")
        return redirect(url_for("login"))

    code = request.args.get("code")
    state = request.args.get("state")
    expected_state = session.pop("oauth_state", None)

    if not state or state != expected_state:
        flash("Authentication session expired or invalid state. Please try again.", "error")
        return redirect(url_for("login"))

    oauth_cfg = db.get_oauth_config()
    client_id = oauth_cfg.get("client_id")
    client_secret = oauth_cfg.get("client_secret")

    scheme = request.headers.get("X-Forwarded-Proto", request.scheme)
    redirect_uri = url_for("auth_callback", _external=True, _scheme=scheme)

    token_url = "https://oauth2.googleapis.com/token"
    token_data = {
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    }

    try:
        token_resp = requests.post(token_url, data=token_data, timeout=10)
        if token_resp.status_code != 200:
            logger.error(f"Failed to exchange token with Google: {token_resp.text}")
            flash("Failed to authenticate with Google. Please check your credentials.", "error")
            return redirect(url_for("login"))

        tokens = token_resp.json()
        access_token = tokens.get("access_token")

        userinfo_resp = requests.get(
            "https://www.googleapis.com/oauth2/v3/userinfo",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10,
        )
        if userinfo_resp.status_code != 200:
            flash("Could not retrieve user info from Google.", "error")
            return redirect(url_for("login"))

        google_user = userinfo_resp.json()

        # Provision or update user in Firestore
        user = db.get_or_create_user({
            "id": google_user.get("sub"),
            "email": google_user.get("email"),
            "name": google_user.get("name"),
            "picture": google_user.get("picture"),
        })

        session["user"] = {
            "id": user["id"],
            "email": user["email"],
            "name": user["name"],
            "picture": user.get("picture", ""),
        }
        flash(f"Welcome back, {user['name']}!", "success")
        return redirect(session.pop("next_url", None) or url_for("index"))

    except Exception as e:
        logger.error(f"Error during Google OAuth callback: {e}")
        flash(f"Sign-in error: {e}", "error")
        return redirect(url_for("login"))


@app.route("/auth/dev-login", methods=["POST", "GET"])
def dev_login():
    """
    Development/demo sign-in option for local testing or before Google OAuth is provisioned.
    Allows switching between users to verify data segmentation.
    """
    user_choice = request.values.get("user_id", "demo_user").strip()
    name = request.values.get("name", "").strip() or "Demo User"
    email = request.values.get("email", "").strip() or f"{user_choice}@example.com"

    user = db.get_or_create_user({
        "id": user_choice,
        "email": email,
        "name": name,
        "picture": f"https://api.dicebear.com/7.x/bottts/svg?seed={user_choice}",
    })

    session["user"] = {
        "id": user["id"],
        "email": user["email"],
        "name": user["name"],
        "picture": user.get("picture", ""),
    }
    flash(f"Signed in as {user['name']} ({user['email']})", "info")
    return redirect(session.pop("next_url", None) or url_for("index"))


@app.route("/auth/setup-oauth", methods=["POST"])
def setup_oauth():
    """Saves Google OAuth Client ID and Secret to Firestore."""
    client_id = request.form.get("client_id", "").strip()
    client_secret = request.form.get("client_secret", "").strip()

    if not client_id or not client_secret:
        flash("Both Google Client ID and Client Secret are required.", "error")
        return redirect(url_for("login"))

    db.save_oauth_config(client_id, client_secret)
    flash("Google OAuth credentials saved successfully! You can now Sign in with Google.", "success")
    return redirect(url_for("login"))


@app.route("/logout", methods=["GET", "POST"])
def logout():
    """Signs out the current user."""
    session.pop("user", None)
    flash("You have been signed out.", "info")
    return redirect(url_for("login"))


# ---------------------------------------------------------------------------
# Dashboard & Core App Routes (Segmented per user)
# ---------------------------------------------------------------------------

@app.route("/", methods=["GET"])
@login_required
def index():
    """Main dashboard displaying tracked products for the logged-in user."""
    user_id = session["user"]["id"]
    all_products = db.get_all_products(user_id=user_id, include_archived=True)
    user_alert_channels = db.get_user_alert_channels(user_id)

    # Query params for filtering
    filter_type = request.args.get("filter", "active").lower()
    selected_site = request.args.get("site", "all")
    search_query = request.args.get("q", "").strip().lower()

    # Calculate summary metrics across user's products
    total_products = len(all_products)
    archived_count = sum(1 for p in all_products if p.get("is_archived"))
    active_count = total_products - archived_count

    active_products = [p for p in all_products if not p.get("is_archived")]
    valid_prices = [p.get("current_price") for p in active_products if p.get("current_price") is not None]
    avg_price = (sum(valid_prices) / len(valid_prices)) if valid_prices else 0.0

    price_drops = sum(1 for p in active_products if (p.get("recent_change", 0) < 0 or p.get("overall_change", 0) < 0))
    price_hikes = sum(1 for p in active_products if (p.get("recent_change", 0) > 0 or p.get("overall_change", 0) > 0))
    all_time_lows = sum(1 for p in active_products if p.get("is_all_time_low"))

    # Extract user's unique retailer names
    sites = sorted(list({p.get("site_name", "Retailer") for p in all_products if p.get("site_name")}))

    # Determine baseline products for current tab
    if filter_type == "archived":
        filtered_products = [p for p in all_products if p.get("is_archived")]
    elif filter_type == "all":
        filtered_products = all_products
    else:
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
        user_alert_channels=user_alert_channels,
    )


@app.route("/track", methods=["POST"])
@login_required
def track():
    """Handles submission of a new product URL to track for the current user."""
    user_id = session["user"]["id"]
    url = request.form.get("url", "").strip()
    if not url:
        flash("Please provide a valid URL.", "error")
        return redirect(url_for("index"))

    logger.info(f"User {user_id} scraping new URL: {url}")
    scraped = scrape_product(url)

    if scraped.get("error") and not scraped.get("name"):
        flash(f"Scraping failed: {scraped['error']}", "error")
        return redirect(url_for("index"))

    # Save to Firestore scoped by user_id
    saved = db.save_or_update_product(
        url=scraped["url"],
        name=scraped["name"] or "Tracked Product",
        price=scraped["price"],
        currency=scraped.get("currency", "$"),
        image_url=scraped.get("image_url"),
        user_id=user_id,
    )

    if scraped.get("price") is not None:
        flash(f"Successfully tracking '{saved['name']}' from {saved['site_name']} at {saved['currency']}{saved['current_price']:.2f}!", "success")
    else:
        flash(f"Added '{saved['name']}' ({saved['site_name']}), but price could not be automatically detected. We'll re-check on the next cycle.", "warning")

    return redirect(url_for("index"))


@app.route("/product/<product_id>", methods=["GET"])
@login_required
def product_detail(product_id):
    """Displays detailed price history and charts for a single user-owned product."""
    user_id = session["user"]["id"]
    product = db.get_product(product_id, user_id=user_id)
    if not product:
        flash("Product not found or access denied.", "error")
        return redirect(url_for("index"))

    history = db.get_price_history(product_id)
    user_alert_channels = db.get_user_alert_channels(user_id)
    return render_template("detail.html", product=product, history=history, user_alert_channels=user_alert_channels)


@app.route("/product/<product_id>/toggle-alert", methods=["POST"])
@login_required
def toggle_alert(product_id):
    """Toggles price drop notifications on/off for a single product."""
    user_id = session["user"]["id"]
    new_state = db.toggle_product_alert(product_id, user_id=user_id)
    state_str = "enabled 🔔" if new_state else "disabled 🔕"
    flash(f"Price drop alerts {state_str} for this item.", "info")
    return redirect(request.referrer or url_for("index"))


@app.route("/product/<product_id>/toggle-archive", methods=["POST"])
@login_required
def toggle_archive(product_id):
    """Archives or unarchives a product (pausing/resuming background tracking)."""
    user_id = session["user"]["id"]
    new_state = db.toggle_product_archive(product_id, user_id=user_id)
    if new_state:
        flash("Product moved to Archive. Background tracking paused (history is preserved).", "info")
    else:
        flash("Product unarchived! Resumed background price tracking.", "success")
    return redirect(request.referrer or url_for("index"))


@app.route("/settings/alert-channels", methods=["POST"])
@login_required
def save_alert_channels():
    """Saves configured alert channels (Google Chat, Email, Slack) for the current user."""
    user_id = session["user"]["id"]
    current_channels = db.get_user_alert_channels(user_id)

    # Google Chat config
    google_chat_webhook = request.form.get("google_chat_webhook", "").strip()
    google_chat_enabled = request.form.get("google_chat_enabled") == "on" or request.form.get("google_chat_enabled") == "true"

    current_channels["google_chat"] = {
        "enabled": google_chat_enabled,
        "webhook_url": google_chat_webhook,
    }

    # Email config
    email_address = request.form.get("email_address", "").strip()
    email_enabled = request.form.get("email_enabled") == "on"
    current_channels["email"] = {
        "enabled": email_enabled,
        "address": email_address or session["user"].get("email", ""),
    }

    # Slack config
    slack_webhook = request.form.get("slack_webhook", "").strip()
    slack_enabled = request.form.get("slack_enabled") == "on"
    current_channels["slack"] = {
        "enabled": slack_enabled,
        "webhook_url": slack_webhook,
    }

    db.update_user_alert_channels(user_id, current_channels)
    flash("Your alert channels have been updated successfully!", "success")
    return redirect(request.referrer or url_for("index"))


@app.route("/settings/test-channel", methods=["POST"])
@login_required
def test_channel():
    """Sends a sample test alert card to the user's configured channel."""
    channel_name = request.form.get("channel", "google_chat")
    user_id = session["user"]["id"]
    user_channels = db.get_user_alert_channels(user_id)

    if channel_name == "google_chat":
        webhook = user_channels.get("google_chat", {}).get("webhook_url")
        if not webhook:
            flash("Please enter and save your Google Chat Webhook URL first.", "error")
            return redirect(request.referrer or url_for("index"))

        success = send_test_google_chat_alert(webhook)
        if success:
            flash("Test alert card delivered successfully to your Google Chat Space! 🚀", "success")
        else:
            flash("Failed to send test alert. Please verify your Google Chat Webhook URL.", "error")
    else:
        flash(f"{channel_name.capitalize()} channel test is coming soon!", "info")

    return redirect(request.referrer or url_for("index"))


@app.route("/product/<product_id>/refresh", methods=["POST"])
@login_required
def refresh_product(product_id):
    """Forces an immediate scrape and price refresh for a single product."""
    user_id = session["user"]["id"]
    product = db.get_product(product_id, user_id=user_id)
    if not product:
        flash("Product not found.", "error")
        return redirect(url_for("index"))

    logger.info(f"Manual refresh requested for {product_id} by user {user_id}")
    scraped = scrape_product(product["url"])

    if scraped.get("price") is not None:
        old_price = product.get("current_price")
        updated = db.save_or_update_product(
            url=product["url"],
            name=scraped.get("name") or product.get("name"),
            price=scraped["price"],
            currency=scraped.get("currency", product.get("currency", "$")),
            image_url=scraped.get("image_url") or product.get("image_url"),
            user_id=user_id,
        )
        flash(f"Refreshed '{product.get('name')}': Current price is {scraped.get('currency', '$')}{scraped['price']:.2f}", "success")

        # Check if price dropped and alerts enabled
        if product.get("alerts_enabled") and not product.get("is_archived") and old_price and scraped["price"] < old_price:
            user_channels = db.get_user_alert_channels(user_id)
            dispatch_user_alerts(user_channels, updated)
            db.update_last_alerted_price(product_id, scraped["price"])
    else:
        flash(f"Could not retrieve updated price: {scraped.get('error', 'Unknown error')}", "error")

    return redirect(request.referrer or url_for("product_detail", product_id=product_id))


@app.route("/product/<product_id>/delete", methods=["POST"])
@login_required
def delete_product(product_id):
    """Deletes a tracked product and its history for the current user."""
    user_id = session["user"]["id"]
    product = db.get_product(product_id, user_id=user_id)
    name = product.get("name", "Product") if product else "Product"
    db.delete_product(product_id, user_id=user_id)
    flash(f"Removed '{name}' from tracking.", "info")
    return redirect(url_for("index"))


# ---------------------------------------------------------------------------
# Background Scheduler Batch Scrape Endpoint (Multi-Tenant)
# ---------------------------------------------------------------------------

@app.route("/api/scrape-all", methods=["POST"])
def scrape_all():
    """
    Secure endpoint triggered by Google Cloud Scheduler to update all active tracked URLs across all users.
    Protected by CRON_SECRET or Google Cloud Scheduler service account identity.
    Dispatches alerts to each product owner's configured channels when price drops.
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

    # Fetch all active products across all users
    products = db.get_all_active_products_all_users()

    results = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "total_active": len(products),
        "updated": 0,
        "failed": 0,
        "alerts_sent": 0,
        "details": [],
    }

    logger.info(f"Starting scheduled scrape for {len(products)} active products across users...")

    for prod in products:
        url = prod.get("url")
        prod_id = prod.get("id")
        user_id = prod.get("user_id", "default_user")
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
                    user_id=user_id,
                )
                results["updated"] += 1

                # Check for price drop alert to this specific user's channels
                alert_dispatched = False
                if alerts_enabled and old_price is not None and new_price < old_price:
                    last_alerted = prod.get("last_alerted_price")
                    if last_alerted is None or new_price < last_alerted:
                        user_channels = db.get_user_alert_channels(user_id)
                        alert_res = dispatch_user_alerts(user_channels, updated)
                        if alert_res.get("dispatched"):
                            alert_dispatched = True
                            results["alerts_sent"] += len(alert_res["dispatched"])
                            db.update_last_alerted_price(prod_id, new_price)

                results["details"].append({
                    "id": prod_id,
                    "user_id": user_id,
                    "name": updated.get("name"),
                    "site_name": updated.get("site_name"),
                    "price": updated.get("current_price"),
                    "alert_sent": alert_dispatched,
                    "status": "success",
                })
            else:
                results["failed"] += 1
                results["details"].append({
                    "id": prod_id,
                    "user_id": user_id,
                    "name": prod.get("name"),
                    "status": "price_not_found",
                    "error": scraped.get("error"),
                })
        except Exception as e:
            logger.error(f"Error scraping {url}: {e}")
            results["failed"] += 1
            results["details"].append({
                "id": prod_id,
                "user_id": user_id,
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
