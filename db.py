"""
Database management module using Google Cloud Firestore.
Handles user management, per-user data segmentation, alert channels,
tracked products, and historical price points.
Includes a graceful local fallback for offline development and testing.
"""

import hashlib
import logging
import os
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# Global client holder
_db_client = None


def get_firestore_client():
    """Initializes and returns the Firestore client singleton."""
    global _db_client
    if _db_client is not None:
        return _db_client

    try:
        from google.cloud import firestore
        project = os.environ.get("GCP_PROJECT") or os.environ.get("GOOGLE_CLOUD_PROJECT") or "cep-demo-x"
        _db_client = firestore.Client(project=project)
        logger.info(f"Connected to Google Cloud Firestore for project: {project}")
        return _db_client
    except Exception as e:
        logger.warning(f"Could not connect to Google Cloud Firestore: {e}. Falling back to in-memory store for local testing.")
        return None


# Local memory store for offline testing fallback
_LOCAL_STORE: Dict[str, Dict[str, Any]] = {}
_LOCAL_HISTORY: Dict[str, List[Dict[str, Any]]] = {}
_LOCAL_USERS: Dict[str, Dict[str, Any]] = {}
_LOCAL_SETTINGS: Dict[str, Any] = {}


def generate_product_id(url: str, user_id: Optional[str] = None) -> str:
    """
    Generates a stable document ID from a URL scoped by user_id for multi-tenancy.
    """
    norm_url = url.strip().rstrip("/")
    if user_id:
        return hashlib.sha256(f"{user_id}:{norm_url}".encode("utf-8")).hexdigest()[:20]
    return hashlib.sha256(norm_url.encode("utf-8")).hexdigest()[:20]


def extract_site_name(url: str) -> str:
    """Extract a user-friendly site/store name from product URL."""
    if not url:
        return "Retailer"
    try:
        domain = urlparse(url).netloc.lower()
        if domain.startswith("www."):
            domain = domain[4:]

        known_stores = {
            "amazon": "Amazon",
            "costco": "Costco",
            "walmart": "Walmart",
            "target": "Target",
            "ebay": "eBay",
            "bestbuy": "Best Buy",
            "books.toscrape": "Books to Scrape",
            "homedepot": "Home Depot",
            "apple": "Apple",
            "newegg": "Newegg",
            "etsy": "Etsy",
            "bhphotovideo": "B&H Photo",
            "aliexpress": "AliExpress",
            "microcenter": "Micro Center",
            "samsclub": "Sam's Club",
            "ikea": "IKEA",
            "wayfair": "Wayfair",
        }
        for pattern, brand in known_stores.items():
            if pattern in domain:
                return brand

        parts = domain.split(".")
        if len(parts) >= 2:
            return parts[-2].capitalize()
        return domain.capitalize()
    except Exception:
        return "Retailer"


# ---------------------------------------------------------------------------
# User & Alert Channels Management
# ---------------------------------------------------------------------------

def get_or_create_user(user_info: Dict[str, Any]) -> Dict[str, Any]:
    """
    Retrieves or provisions a user profile with personal alert channel settings.
    """
    client = get_firestore_client()
    now = datetime.now(timezone.utc)
    user_id = str(user_info.get("id") or user_info.get("sub") or user_info.get("email"))
    email = user_info.get("email", "")
    name = user_info.get("name") or (email.split("@")[0] if email else "User")
    picture = user_info.get("picture", "")

    default_channels = {
        "google_chat": {
            "enabled": True,
            "webhook_url": get_global_settings().get("google_chat_webhook", "")
        },
        "email": {
            "enabled": False,
            "address": email
        },
        "slack": {
            "enabled": False,
            "webhook_url": ""
        }
    }

    if client:
        try:
            doc_ref = client.collection("users").document(user_id)
            snap = doc_ref.get()
            if snap.exists:
                existing = snap.to_dict()
                update_fields = {"last_login": now}
                if name and name != existing.get("name"):
                    update_fields["name"] = name
                if picture and picture != existing.get("picture"):
                    update_fields["picture"] = picture
                # Ensure alert_channels exists
                if "alert_channels" not in existing:
                    update_fields["alert_channels"] = default_channels

                doc_ref.update(update_fields)
                existing.update(update_fields)
                existing["id"] = user_id
                return existing
            else:
                new_user = {
                    "id": user_id,
                    "email": email,
                    "name": name,
                    "picture": picture,
                    "created_at": now,
                    "last_login": now,
                    "alert_channels": default_channels
                }
                doc_ref.set(new_user)
                return new_user
        except Exception as e:
            logger.error(f"Error getting/creating user in Firestore: {e}")

    # Local fallback
    if user_id in _LOCAL_USERS:
        existing = _LOCAL_USERS[user_id]
        existing["last_login"] = now
        if "alert_channels" not in existing:
            existing["alert_channels"] = default_channels
        return existing
    else:
        new_user = {
            "id": user_id,
            "email": email,
            "name": name,
            "picture": picture,
            "created_at": now,
            "last_login": now,
            "alert_channels": default_channels
        }
        _LOCAL_USERS[user_id] = new_user
        return new_user


def get_user(user_id: str) -> Optional[Dict[str, Any]]:
    """Retrieves a user profile by ID."""
    client = get_firestore_client()
    if client:
        try:
            doc = client.collection("users").document(user_id).get()
            if doc.exists:
                d = doc.to_dict()
                d["id"] = user_id
                return d
        except Exception as e:
            logger.error(f"Error retrieving user {user_id}: {e}")
    return _LOCAL_USERS.get(user_id)


def get_user_alert_channels(user_id: str) -> Dict[str, Any]:
    """Retrieves configured alert channels for a user."""
    user = get_user(user_id)
    if user and user.get("alert_channels"):
        return user["alert_channels"]
    
    # Fallback to global config
    global_hook = get_global_settings().get("google_chat_webhook", "")
    return {
        "google_chat": {
            "enabled": bool(global_hook),
            "webhook_url": global_hook
        },
        "email": {
            "enabled": False,
            "address": user.get("email", "") if user else ""
        }
    }


def update_user_alert_channels(user_id: str, alert_channels: Dict[str, Any]) -> bool:
    """Updates the alert channels for a specific user."""
    client = get_firestore_client()
    if client:
        try:
            client.collection("users").document(user_id).update({
                "alert_channels": alert_channels
            })
            return True
        except Exception as e:
            logger.error(f"Error updating alert channels for {user_id}: {e}")
            return False
    else:
        if user_id in _LOCAL_USERS:
            _LOCAL_USERS[user_id]["alert_channels"] = alert_channels
            return True
        return False


# ---------------------------------------------------------------------------
# Product Operations (Per-User Multi-Tenancy)
# ---------------------------------------------------------------------------

def normalize_product_dict(d: Dict[str, Any]) -> Dict[str, Any]:
    """Ensures backwards compatibility for all fields (site_name, dual changes, all_time_low, alerts, archive, user_id)."""
    if not d.get("site_name") and d.get("url"):
        d["site_name"] = extract_site_name(d["url"])
    if "recent_change" not in d:
        d["recent_change"] = d.get("price_change", 0.0)
        d["recent_change_pct"] = d.get("price_change_pct", 0.0)
    if "overall_change" not in d:
        curr = d.get("current_price")
        init = d.get("initial_price")
        if curr is not None and init is not None:
            d["overall_change"] = curr - init
            d["overall_change_pct"] = round(((curr - init) / init) * 100, 2) if init > 0 else 0.0
        else:
            d["overall_change"] = 0.0
            d["overall_change_pct"] = 0.0
    if "is_all_time_low" not in d:
        curr = d.get("current_price")
        lowest = d.get("lowest_price")
        d["is_all_time_low"] = bool(curr is not None and lowest is not None and curr <= lowest and (d.get("overall_change", 0) < 0 or d.get("recent_change", 0) < 0))

    d["alerts_enabled"] = d.get("alerts_enabled", True)
    d["is_archived"] = d.get("is_archived", False)
    d["last_alerted_price"] = d.get("last_alerted_price")
    d["user_id"] = d.get("user_id", "default_user")

    return d


def save_or_update_product(
    url: str,
    name: str,
    price: Optional[float],
    currency: str = "$",
    image_url: Optional[str] = None,
    user_id: str = "default_user",
) -> Dict[str, Any]:
    """
    Saves a newly tracked product or updates an existing one for a specific user.
    """
    client = get_firestore_client()
    prod_id = generate_product_id(url, user_id)
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    site_name = extract_site_name(url)

    if client:
        doc_ref = client.collection("tracked_products").document(prod_id)
        snapshot = doc_ref.get()

        if snapshot.exists:
            existing = snapshot.to_dict()
            prev_price = existing.get("current_price")
            lowest = min(existing.get("lowest_price", price or 0), price) if price else existing.get("lowest_price")
            highest = max(existing.get("highest_price", price or 0), price) if price else existing.get("highest_price")
            initial = existing.get("initial_price", price)

            recent_change = (price - prev_price) if (price is not None and prev_price is not None) else 0.0
            recent_pct = round((recent_change / prev_price) * 100, 2) if (prev_price and prev_price > 0) else 0.0

            overall_change = (price - initial) if (price is not None and initial is not None) else 0.0
            overall_pct = round((overall_change / initial) * 100, 2) if (initial and initial > 0) else 0.0

            is_all_time_low = (price is not None and lowest is not None and price <= lowest and (overall_change < 0 or recent_change < 0))

            update_data = {
                "user_id": user_id,
                "name": name or existing.get("name"),
                "url": url,
                "site_name": site_name,
                "current_price": price if price is not None else existing.get("current_price"),
                "previous_price": prev_price,
                "currency": currency,
                "image_url": image_url or existing.get("image_url"),
                "last_scraped_at": now,
                "lowest_price": lowest,
                "highest_price": highest,
                "price_change": recent_change,
                "price_change_pct": recent_pct,
                "recent_change": recent_change,
                "recent_change_pct": recent_pct,
                "overall_change": overall_change,
                "overall_change_pct": overall_pct,
                "is_all_time_low": is_all_time_low,
            }

            if price is not None:
                recent = existing.get("recent_history", [])
                recent.append({"price": price, "currency": currency, "timestamp": now_iso})
                if len(recent) > 50:
                    recent = recent[-50:]
                update_data["recent_history"] = recent

                doc_ref.collection("history").add({
                    "price": price,
                    "currency": currency,
                    "timestamp": now,
                    "status": "success",
                })

            doc_ref.update(update_data)
            return normalize_product_dict({**existing, **update_data, "id": prod_id})
        else:
            data = {
                "id": prod_id,
                "user_id": user_id,
                "url": url,
                "site_name": site_name,
                "name": name,
                "current_price": price,
                "previous_price": price,
                "currency": currency,
                "image_url": image_url,
                "initial_price": price,
                "lowest_price": price,
                "highest_price": price,
                "price_change": 0.0,
                "price_change_pct": 0.0,
                "recent_change": 0.0,
                "recent_change_pct": 0.0,
                "overall_change": 0.0,
                "overall_change_pct": 0.0,
                "is_all_time_low": False,
                "alerts_enabled": True,
                "is_archived": False,
                "last_alerted_price": None,
                "created_at": now,
                "last_scraped_at": now,
                "recent_history": [{"price": price, "currency": currency, "timestamp": now_iso}] if price else [],
            }
            doc_ref.set(data)

            if price is not None:
                doc_ref.collection("history").add({
                    "price": price,
                    "currency": currency,
                    "timestamp": now,
                    "status": "success",
                })
            return normalize_product_dict(data)
    else:
        # Local Fallback
        if prod_id in _LOCAL_STORE:
            existing = _LOCAL_STORE[prod_id]
            prev_price = existing.get("current_price")
            lowest = min(existing.get("lowest_price", price or 0), price) if price else existing.get("lowest_price")
            highest = max(existing.get("highest_price", price or 0), price) if price else existing.get("highest_price")
            initial = existing.get("initial_price", price)

            recent_change = (price - prev_price) if (price is not None and prev_price is not None) else 0.0
            recent_pct = round((recent_change / prev_price) * 100, 2) if (prev_price and prev_price > 0) else 0.0

            overall_change = (price - initial) if (price is not None and initial is not None) else 0.0
            overall_pct = round((overall_change / initial) * 100, 2) if (initial and initial > 0) else 0.0

            is_all_time_low = (price is not None and lowest is not None and price <= lowest and (overall_change < 0 or recent_change < 0))

            existing.update({
                "user_id": user_id,
                "name": name or existing.get("name"),
                "site_name": site_name,
                "current_price": price if price is not None else existing.get("current_price"),
                "previous_price": prev_price,
                "currency": currency,
                "image_url": image_url or existing.get("image_url"),
                "last_scraped_at": now,
                "lowest_price": lowest,
                "highest_price": highest,
                "price_change": recent_change,
                "price_change_pct": recent_pct,
                "recent_change": recent_change,
                "recent_change_pct": recent_pct,
                "overall_change": overall_change,
                "overall_change_pct": overall_pct,
                "is_all_time_low": is_all_time_low,
            })
            if price is not None:
                existing["recent_history"].append({"price": price, "currency": currency, "timestamp": now_iso})
                _LOCAL_HISTORY.setdefault(prod_id, []).append({"price": price, "currency": currency, "timestamp": now, "status": "success"})
            return normalize_product_dict(existing)
        else:
            data = {
                "id": prod_id,
                "user_id": user_id,
                "url": url,
                "site_name": site_name,
                "name": name,
                "current_price": price,
                "previous_price": price,
                "currency": currency,
                "image_url": image_url,
                "initial_price": price,
                "lowest_price": price,
                "highest_price": price,
                "price_change": 0.0,
                "price_change_pct": 0.0,
                "recent_change": 0.0,
                "recent_change_pct": 0.0,
                "overall_change": 0.0,
                "overall_change_pct": 0.0,
                "is_all_time_low": False,
                "alerts_enabled": True,
                "is_archived": False,
                "last_alerted_price": None,
                "created_at": now,
                "last_scraped_at": now,
                "recent_history": [{"price": price, "currency": currency, "timestamp": now_iso}] if price else [],
            }
            _LOCAL_STORE[prod_id] = data
            if price is not None:
                _LOCAL_HISTORY.setdefault(prod_id, []).append({"price": price, "currency": currency, "timestamp": now, "status": "success"})
            return normalize_product_dict(data)


def get_all_products(user_id: Optional[str] = None, include_archived: bool = True) -> List[Dict[str, Any]]:
    """
    Fetches tracked products from Firestore, filtered by user_id if provided.
    Also handles legacy products gracefully.
    """
    client = get_firestore_client()
    products = []
    if client:
        try:
            if user_id:
                # Query products matching user_id
                query = client.collection("tracked_products").where("user_id", "==", user_id)
                docs = query.stream()
                for doc in docs:
                    d = doc.to_dict()
                    d["id"] = doc.id
                    products.append(normalize_product_dict(d))
                
                # Check for legacy products without user_id (claim them or show them)
                legacy_docs = client.collection("tracked_products").where("user_id", "==", None).stream()
                for doc in legacy_docs:
                    d = doc.to_dict()
                    d["id"] = doc.id
                    # Auto-assign legacy to this user
                    client.collection("tracked_products").document(doc.id).update({"user_id": user_id})
                    d["user_id"] = user_id
                    products.append(normalize_product_dict(d))
            else:
                docs = client.collection("tracked_products").order_by("created_at", direction="DESCENDING").stream()
                for doc in docs:
                    d = doc.to_dict()
                    d["id"] = doc.id
                    products.append(normalize_product_dict(d))
        except Exception as e:
            logger.error(f"Error fetching products from Firestore: {e}")
            docs = client.collection("tracked_products").stream()
            for doc in docs:
                d = doc.to_dict()
                d["id"] = doc.id
                prod = normalize_product_dict(d)
                if not user_id or prod.get("user_id") == user_id:
                    products.append(prod)
    else:
        for p in _LOCAL_STORE.values():
            prod = normalize_product_dict(p)
            if not user_id or prod.get("user_id") == user_id or prod.get("user_id") == "default_user":
                products.append(prod)

    # Sort by created_at descending
    products.sort(key=lambda x: str(x.get("created_at", "")), reverse=True)

    if not include_archived:
        return [p for p in products if not p.get("is_archived")]
    return products


def get_all_active_products_all_users() -> List[Dict[str, Any]]:
    """Fetches all unarchived products across all users for background scraping."""
    client = get_firestore_client()
    products = []
    if client:
        try:
            docs = client.collection("tracked_products").stream()
            for doc in docs:
                d = doc.to_dict()
                d["id"] = doc.id
                prod = normalize_product_dict(d)
                if not prod.get("is_archived"):
                    products.append(prod)
        except Exception as e:
            logger.error(f"Error fetching all active products: {e}")
    else:
        for p in _LOCAL_STORE.values():
            prod = normalize_product_dict(p)
            if not prod.get("is_archived"):
                products.append(prod)
    return products


def get_product(product_id: str, user_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Retrieves a single product by ID, checking user ownership if user_id is passed."""
    client = get_firestore_client()
    if client:
        doc = client.collection("tracked_products").document(product_id).get()
        if doc.exists:
            d = doc.to_dict()
            d["id"] = doc.id
            prod = normalize_product_dict(d)
            if user_id and prod.get("user_id") not in (user_id, "default_user"):
                return None
            return prod
        return None
    p = _LOCAL_STORE.get(product_id)
    if p:
        prod = normalize_product_dict(p)
        if user_id and prod.get("user_id") not in (user_id, "default_user"):
            return None
        return prod
    return None


def toggle_product_alert(product_id: str, enabled: Optional[bool] = None, user_id: Optional[str] = None) -> bool:
    """Toggles or sets the alerts_enabled boolean for a specific product."""
    client = get_firestore_client()
    if client:
        doc_ref = client.collection("tracked_products").document(product_id)
        snap = doc_ref.get()
        if snap.exists:
            data = snap.to_dict()
            if user_id and data.get("user_id") not in (user_id, "default_user"):
                return False
            curr = data.get("alerts_enabled", True)
            new_val = not curr if enabled is None else enabled
            doc_ref.update({"alerts_enabled": new_val})
            return new_val
        return False
    else:
        if product_id in _LOCAL_STORE:
            data = _LOCAL_STORE[product_id]
            if user_id and data.get("user_id") not in (user_id, "default_user"):
                return False
            curr = data.get("alerts_enabled", True)
            new_val = not curr if enabled is None else enabled
            _LOCAL_STORE[product_id]["alerts_enabled"] = new_val
            return new_val
        return False


def toggle_product_archive(product_id: str, is_archived: Optional[bool] = None, user_id: Optional[str] = None) -> bool:
    """Toggles or sets the is_archived boolean for a product."""
    client = get_firestore_client()
    if client:
        doc_ref = client.collection("tracked_products").document(product_id)
        snap = doc_ref.get()
        if snap.exists:
            data = snap.to_dict()
            if user_id and data.get("user_id") not in (user_id, "default_user"):
                return False
            curr = data.get("is_archived", False)
            new_val = not curr if is_archived is None else is_archived
            doc_ref.update({"is_archived": new_val})
            return new_val
        return False
    else:
        if product_id in _LOCAL_STORE:
            data = _LOCAL_STORE[product_id]
            if user_id and data.get("user_id") not in (user_id, "default_user"):
                return False
            curr = data.get("is_archived", False)
            new_val = not curr if is_archived is None else is_archived
            _LOCAL_STORE[product_id]["is_archived"] = new_val
            return new_val
        return False


def update_last_alerted_price(product_id: str, price: float):
    """Updates the last price an alert was sent for to avoid repeating."""
    client = get_firestore_client()
    if client:
        client.collection("tracked_products").document(product_id).update({
            "last_alerted_price": price
        })
    elif product_id in _LOCAL_STORE:
        _LOCAL_STORE[product_id]["last_alerted_price"] = price


# ---------------------------------------------------------------------------
# Global App & OAuth Settings
# ---------------------------------------------------------------------------

def get_global_settings() -> Dict[str, Any]:
    """Fetches global settings (e.g. Google Chat Webhook URL) from Firestore."""
    client = get_firestore_client()
    default_webhook = os.environ.get("GOOGLE_CHAT_WEBHOOK", "")
    if client:
        try:
            doc = client.collection("settings").document("app_config").get()
            if doc.exists:
                data = doc.to_dict()
                if not data.get("google_chat_webhook"):
                    data["google_chat_webhook"] = default_webhook
                return data
        except Exception as e:
            logger.warning(f"Error fetching settings: {e}")
    return {"google_chat_webhook": _LOCAL_SETTINGS.get("google_chat_webhook", default_webhook)}


def save_global_settings(settings: Dict[str, Any]) -> bool:
    """Saves global settings to Firestore."""
    client = get_firestore_client()
    if client:
        try:
            client.collection("settings").document("app_config").set(settings, merge=True)
            return True
        except Exception as e:
            logger.error(f"Error saving settings to Firestore: {e}")
            return False
    else:
        _LOCAL_SETTINGS.update(settings)
        return True


def get_oauth_config() -> Dict[str, str]:
    """
    Retrieves Google OAuth client credentials.
    Checks environment variables first, then Firestore settings/oauth_config.
    """
    client_id = os.environ.get("GOOGLE_CLIENT_ID", "").strip()
    client_secret = os.environ.get("GOOGLE_CLIENT_SECRET", "").strip()

    if client_id and client_secret:
        return {"client_id": client_id, "client_secret": client_secret}

    client = get_firestore_client()
    if client:
        try:
            doc = client.collection("settings").document("oauth_config").get()
            if doc.exists:
                data = doc.to_dict()
                return {
                    "client_id": client_id or data.get("client_id", "").strip(),
                    "client_secret": client_secret or data.get("client_secret", "").strip(),
                }
        except Exception as e:
            logger.warning(f"Error fetching OAuth config: {e}")

    return {
        "client_id": client_id or _LOCAL_SETTINGS.get("google_client_id", ""),
        "client_secret": client_secret or _LOCAL_SETTINGS.get("google_client_secret", "")
    }


def save_oauth_config(client_id: str, client_secret: str) -> bool:
    """Saves Google OAuth client credentials to Firestore settings/oauth_config."""
    client = get_firestore_client()
    data = {"client_id": client_id.strip(), "client_secret": client_secret.strip()}
    if client:
        try:
            client.collection("settings").document("oauth_config").set(data, merge=True)
            return True
        except Exception as e:
            logger.error(f"Error saving OAuth config: {e}")
            return False
    else:
        _LOCAL_SETTINGS["google_client_id"] = client_id.strip()
        _LOCAL_SETTINGS["google_client_secret"] = client_secret.strip()
        return True


def get_price_history(product_id: str, limit: int = 100) -> List[Dict[str, Any]]:
    """Retrieves price history logs for a product."""
    client = get_firestore_client()
    if client:
        try:
            docs = (
                client.collection("tracked_products")
                .document(product_id)
                .collection("history")
                .order_by("timestamp", direction="DESCENDING")
                .limit(limit)
                .stream()
            )
            history = []
            for doc in docs:
                d = doc.to_dict()
                d["id"] = doc.id
                history.append(d)
            return history
        except Exception as e:
            logger.error(f"Error fetching history from Firestore: {e}")
            return []
    return list(reversed(_LOCAL_HISTORY.get(product_id, [])))[:limit]


def delete_product(product_id: str, user_id: Optional[str] = None) -> bool:
    """Deletes a product and its history subcollection from Firestore."""
    client = get_firestore_client()
    if client:
        doc_ref = client.collection("tracked_products").document(product_id)
        snap = doc_ref.get()
        if snap.exists:
            data = snap.to_dict()
            if user_id and data.get("user_id") not in (user_id, "default_user"):
                return False
            # Delete subcollection documents
            history_docs = doc_ref.collection("history").stream()
            for h in history_docs:
                h.reference.delete()
            # Delete main document
            doc_ref.delete()
            return True
        return False
    else:
        if product_id in _LOCAL_STORE:
            data = _LOCAL_STORE[product_id]
            if user_id and data.get("user_id") not in (user_id, "default_user"):
                return False
            del _LOCAL_STORE[product_id]
            _LOCAL_HISTORY.pop(product_id, None)
            return True
        return False
