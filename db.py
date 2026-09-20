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
            return {**existing, **update_data, "id": prod_id}
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
            return data
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
            return existing
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
                "created_at": now,
                "last_scraped_at": now,
                "recent_history": [{"price": price, "currency": currency, "timestamp": now_iso}] if price else [],
            }
            _LOCAL_STORE[prod_id] = data
            if price is not None:
                _LOCAL_HISTORY.setdefault(prod_id, []).append({"price": price, "currency": currency, "timestamp": now, "status": "success"})
            return data


def normalize_product_dict(d: Dict[str, Any]) -> Dict[str, Any]:
    """Ensures backwards compatibility for all fields (site_name, dual changes, all_time_low)."""
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
    return d


def get_all_products() -> List[Dict[str, Any]]:
    """Fetches all tracked products from Firestore ordered by creation date."""
    client = get_firestore_client()
    if client:
        try:
            docs = client.collection("tracked_products").order_by("created_at", direction="DESCENDING").stream()
            products = []
            for doc in docs:
                d = doc.to_dict()
                d["id"] = doc.id
                products.append(normalize_product_dict(d))
            return products
        except Exception as e:
            logger.error(f"Error fetching products from Firestore: {e}")
            docs = client.collection("tracked_products").stream()
            return [normalize_product_dict({**doc.to_dict(), "id": doc.id}) for doc in docs]
    else:
        return [normalize_product_dict(p) for p in _LOCAL_STORE.values()]


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
