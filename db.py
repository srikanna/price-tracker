"""
Database management module using Google Cloud Firestore.
Handles storing tracked URLs, product details, and historical price points.
Includes a graceful local fallback for offline development.
"""

import hashlib
import logging
import os
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional

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


def generate_product_id(url: str) -> str:
    """Generates a stable document ID from a URL."""
    # Normalize URL by removing trailing slashes
    norm_url = url.strip().rstrip("/")
    return hashlib.sha256(norm_url.encode("utf-8")).hexdigest()[:20]


from urllib.parse import urlparse


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


def save_or_update_product(
    url: str,
    name: str,
    price: Optional[float],
    currency: str = "$",
    image_url: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Saves a newly tracked product or updates an existing one.
    Tracks site_name, previous_price, lowest/highest price,
    recent price change (since last check), and overall change (since tracking began).
    """
    client = get_firestore_client()
    prod_id = generate_product_id(url)
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

            # Recent change (since previous scrape)
            recent_change = (price - prev_price) if (price is not None and prev_price is not None) else 0.0
            recent_pct = round((recent_change / prev_price) * 100, 2) if (prev_price and prev_price > 0) else 0.0

            # Overall change (since first tracked)
            overall_change = (price - initial) if (price is not None and initial is not None) else 0.0
            overall_pct = round((overall_change / initial) * 100, 2) if (initial and initial > 0) else 0.0

            # All-time low flag: true if at lowest price ever and has dropped from initial
            is_all_time_low = (price is not None and lowest is not None and price <= lowest and (overall_change < 0 or recent_change < 0))

            update_data = {
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
            # Create new document
            data = {
                "id": prod_id,
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


def normalize_product_dict(d: Dict[str, Any]) -> Dict[str, Any]:
    """Ensures backwards compatibility for all fields (site_name, dual changes, all_time_low, alerts, archive)."""
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

    # Per-item alert and archive controls
    d["alerts_enabled"] = d.get("alerts_enabled", True)
    d["is_archived"] = d.get("is_archived", False)
    d["last_alerted_price"] = d.get("last_alerted_price")

    return d


def get_all_products(include_archived: bool = True) -> List[Dict[str, Any]]:
    """Fetches tracked products from Firestore ordered by creation date."""
    client = get_firestore_client()
    products = []
    if client:
        try:
            docs = client.collection("tracked_products").order_by("created_at", direction="DESCENDING").stream()
            for doc in docs:
                d = doc.to_dict()
                d["id"] = doc.id
                products.append(normalize_product_dict(d))
        except Exception as e:
            logger.error(f"Error fetching products from Firestore: {e}")
            docs = client.collection("tracked_products").stream()
            products = [normalize_product_dict({**doc.to_dict(), "id": doc.id}) for doc in docs]
    else:
        products = [normalize_product_dict(p) for p in _LOCAL_STORE.values()]

    if not include_archived:
        return [p for p in products if not p.get("is_archived")]
    return products


def get_product(product_id: str) -> Optional[Dict[str, Any]]:
    """Retrieves a single product by ID."""
    client = get_firestore_client()
    if client:
        doc = client.collection("tracked_products").document(product_id).get()
        if doc.exists:
            d = doc.to_dict()
            d["id"] = doc.id
            return normalize_product_dict(d)
        return None
    p = _LOCAL_STORE.get(product_id)
    return normalize_product_dict(p) if p else None


def toggle_product_alert(product_id: str, enabled: Optional[bool] = None) -> bool:
    """Toggles or sets the alerts_enabled boolean for a specific product."""
    client = get_firestore_client()
    if client:
        doc_ref = client.collection("tracked_products").document(product_id)
        snap = doc_ref.get()
        if snap.exists:
            curr = snap.to_dict().get("alerts_enabled", True)
            new_val = not curr if enabled is None else enabled
            doc_ref.update({"alerts_enabled": new_val})
            return new_val
        return False
    else:
        if product_id in _LOCAL_STORE:
            curr = _LOCAL_STORE[product_id].get("alerts_enabled", True)
            new_val = not curr if enabled is None else enabled
            _LOCAL_STORE[product_id]["alerts_enabled"] = new_val
            return new_val
        return False


def toggle_product_archive(product_id: str, is_archived: Optional[bool] = None) -> bool:
    """Toggles or sets the is_archived boolean for a product (pauses/resumes scraping)."""
    client = get_firestore_client()
    if client:
        doc_ref = client.collection("tracked_products").document(product_id)
        snap = doc_ref.get()
        if snap.exists:
            curr = snap.to_dict().get("is_archived", False)
            new_val = not curr if is_archived is None else is_archived
            doc_ref.update({"is_archived": new_val})
            return new_val
        return False
    else:
        if product_id in _LOCAL_STORE:
            curr = _LOCAL_STORE[product_id].get("is_archived", False)
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


# Global App Settings (Google Chat Webhook, etc.)
_LOCAL_SETTINGS: Dict[str, Any] = {}


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


def delete_product(product_id: str) -> bool:
    """Deletes a product and its history subcollection from Firestore."""
    client = get_firestore_client()
    if client:
        doc_ref = client.collection("tracked_products").document(product_id)
        # Delete subcollection documents
        history_docs = doc_ref.collection("history").stream()
        for h in history_docs:
            h.reference.delete()
        # Delete main document
        doc_ref.delete()
        return True
    else:
        if product_id in _LOCAL_STORE:
            del _LOCAL_STORE[product_id]
            _LOCAL_HISTORY.pop(product_id, None)
            return True
        return False
