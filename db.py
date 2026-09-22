"""
Database management module using Google Cloud Firestore.
Implements the Canonical Products + User Subscriptions architecture:
1. canonical_products: Shared product catalog (scraped once, holds master price history).
2. user_subscriptions: User-specific watches (alert toggles, personal archive status, personal baselines).
3. users: Profiles & personal alert channels (Google Chat Spaces, Email, etc.).
Includes an in-memory fallback for local development and offline automated testing.
"""

import hashlib
import logging
import os
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional
from urllib.parse import urlparse

from scraper import canonicalize_url

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


# ---------------------------------------------------------------------------
# In-Memory Fallback Stores for Offline / Testing Mode
# ---------------------------------------------------------------------------
_LOCAL_CANONICAL: Dict[str, Dict[str, Any]] = {}
_LOCAL_SUBSCRIPTIONS: Dict[str, Dict[str, Any]] = {}
_LOCAL_HISTORY: Dict[str, List[Dict[str, Any]]] = {}
_LOCAL_USERS: Dict[str, Dict[str, Any]] = {}
_LOCAL_SETTINGS: Dict[str, Any] = {}


def generate_product_id(url: str, user_id: Optional[str] = None) -> str:
    """
    Generates a deterministic 20-character product ID from a canonicalized URL.
    Identical across all users tracking the same product.
    """
    clean_url = canonicalize_url(url)
    return hashlib.sha256(clean_url.encode("utf-8")).hexdigest()[:20]


def generate_subscription_id(user_id: str, product_id: str) -> str:
    """Generates a composite subscription key for a user watching a product."""
    return f"{user_id}_{product_id}"


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
# Canonical Product Catalog Operations
# ---------------------------------------------------------------------------

def get_canonical_product(product_id: str) -> Optional[Dict[str, Any]]:
    """Retrieves a product from the canonical catalog by its product ID."""
    client = get_firestore_client()
    if client:
        try:
            doc = client.collection("canonical_products").document(product_id).get()
            if doc.exists:
                d = doc.to_dict()
                d["id"] = doc.id
                return d
        except Exception as e:
            logger.error(f"Error fetching canonical product {product_id}: {e}")
    p = _LOCAL_CANONICAL.get(product_id)
    return dict(p) if p else None


def save_or_update_canonical_product(
    url: str,
    name: str,
    price: Optional[float],
    currency: str = "$",
    image_url: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Creates or updates an entry in the shared canonical_products collection.
    Records historical prices in the master subcollection history.
    """
    clean_url = canonicalize_url(url)
    prod_id = generate_product_id(clean_url)
    client = get_firestore_client()
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    site_name = extract_site_name(clean_url)

    if client:
        doc_ref = client.collection("canonical_products").document(prod_id)
        snap = doc_ref.get()

        if snap.exists:
            existing = snap.to_dict()
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
                "name": name or existing.get("name"),
                "url": clean_url,
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
                if len(recent) > 60:
                    recent = recent[-60:]
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
            data = {
                "id": prod_id,
                "url": clean_url,
                "site_name": site_name,
                "name": name or "Tracked Product",
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
        # Local in-memory store
        if prod_id in _LOCAL_CANONICAL:
            existing = _LOCAL_CANONICAL[prod_id]
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
                "url": clean_url,
                "site_name": site_name,
                "name": name or "Tracked Product",
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
            _LOCAL_CANONICAL[prod_id] = data
            if price is not None:
                _LOCAL_HISTORY.setdefault(prod_id, []).append({"price": price, "currency": currency, "timestamp": now, "status": "success"})
            return data


# ---------------------------------------------------------------------------
# User Subscription (Watchlist) Operations
# ---------------------------------------------------------------------------

def subscribe_user_to_product(
    user_id: str,
    product_id: str,
    user_initial_price: Optional[float] = None
) -> Dict[str, Any]:
    """
    Subscribes a user to watch a canonical product.
    Tracks user's personal alerts toggle, archive state, and starting price.
    """
    sub_id = generate_subscription_id(user_id, product_id)
    client = get_firestore_client()
    now = datetime.now(timezone.utc)

    sub_data = {
        "id": sub_id,
        "user_id": user_id,
        "product_id": product_id,
        "alerts_enabled": True,
        "is_archived": False,
        "user_initial_price": user_initial_price,
        "last_alerted_price": None,
        "created_at": now,
    }

    if client:
        try:
            doc_ref = client.collection("user_subscriptions").document(sub_id)
            snap = doc_ref.get()
            if snap.exists:
                existing = snap.to_dict()
                # If was archived, reactivate
                doc_ref.update({"is_archived": False})
                existing["is_archived"] = False
                return existing
            doc_ref.set(sub_data)
            return sub_data
        except Exception as e:
            logger.error(f"Error subscribing user {user_id} to product {product_id}: {e}")

    # Local fallback
    if sub_id in _LOCAL_SUBSCRIPTIONS:
        _LOCAL_SUBSCRIPTIONS[sub_id]["is_archived"] = False
        return _LOCAL_SUBSCRIPTIONS[sub_id]
    _LOCAL_SUBSCRIPTIONS[sub_id] = sub_data
    return sub_data


def unsubscribe_user_from_product(user_id: str, product_id: str) -> bool:
    """Removes a user's subscription to a product."""
    sub_id = generate_subscription_id(user_id, product_id)
    client = get_firestore_client()
    if client:
        try:
            client.collection("user_subscriptions").document(sub_id).delete()
            return True
        except Exception as e:
            logger.error(f"Error deleting subscription {sub_id}: {e}")
            return False
    else:
        if sub_id in _LOCAL_SUBSCRIPTIONS:
            del _LOCAL_SUBSCRIPTIONS[sub_id]
            return True
        return False


def toggle_subscription_alert(user_id: str, product_id: str, enabled: Optional[bool] = None) -> bool:
    """Toggles or sets the personal alert toggle for a user's subscription."""
    sub_id = generate_subscription_id(user_id, product_id)
    client = get_firestore_client()
    if client:
        try:
            doc_ref = client.collection("user_subscriptions").document(sub_id)
            snap = doc_ref.get()
            if snap.exists:
                curr = snap.to_dict().get("alerts_enabled", True)
                new_val = not curr if enabled is None else enabled
                doc_ref.update({"alerts_enabled": new_val})
                return new_val
        except Exception as e:
            logger.error(f"Error toggling alert for {sub_id}: {e}")
            return False
    else:
        if sub_id in _LOCAL_SUBSCRIPTIONS:
            curr = _LOCAL_SUBSCRIPTIONS[sub_id].get("alerts_enabled", True)
            new_val = not curr if enabled is None else enabled
            _LOCAL_SUBSCRIPTIONS[sub_id]["alerts_enabled"] = new_val
            return new_val
    return False


def toggle_subscription_archive(user_id: str, product_id: str, is_archived: Optional[bool] = None) -> bool:
    """Toggles or sets the personal archive state for a user's subscription."""
    sub_id = generate_subscription_id(user_id, product_id)
    client = get_firestore_client()
    if client:
        try:
            doc_ref = client.collection("user_subscriptions").document(sub_id)
            snap = doc_ref.get()
            if snap.exists:
                curr = snap.to_dict().get("is_archived", False)
                new_val = not curr if is_archived is None else is_archived
                doc_ref.update({"is_archived": new_val})
                return new_val
        except Exception as e:
            logger.error(f"Error toggling archive for {sub_id}: {e}")
            return False
    else:
        if sub_id in _LOCAL_SUBSCRIPTIONS:
            curr = _LOCAL_SUBSCRIPTIONS[sub_id].get("is_archived", False)
            new_val = not curr if is_archived is None else is_archived
            _LOCAL_SUBSCRIPTIONS[sub_id]["is_archived"] = new_val
            return new_val
    return False


def update_subscription_last_alerted_price(user_id: str, product_id: str, price: float):
    """Updates the last alerted price on a user's subscription to prevent alert spam."""
    sub_id = generate_subscription_id(user_id, product_id)
    client = get_firestore_client()
    if client:
        try:
            client.collection("user_subscriptions").document(sub_id).update({
                "last_alerted_price": price
            })
        except Exception as e:
            logger.error(f"Error updating last alerted price on {sub_id}: {e}")
    elif sub_id in _LOCAL_SUBSCRIPTIONS:
        _LOCAL_SUBSCRIPTIONS[sub_id]["last_alerted_price"] = price


# ---------------------------------------------------------------------------
# Composite View Methods (Merging Canonical Catalog + User Watchlist)
# ---------------------------------------------------------------------------

def merge_canonical_and_subscription(canon: Dict[str, Any], sub: Dict[str, Any]) -> Dict[str, Any]:
    """Merges canonical product details with personal user subscription state."""
    curr = canon.get("current_price")
    prev = canon.get("previous_price") or curr
    user_init = sub.get("user_initial_price") or canon.get("initial_price") or curr

    # User's personal overall change since THEY started tracking
    user_overall_change = (curr - user_init) if (curr is not None and user_init is not None) else 0.0
    user_overall_pct = round((user_overall_change / user_init) * 100, 2) if (user_init and user_init > 0) else 0.0

    return {
        **canon,
        "id": canon.get("id"),
        "product_id": canon.get("id"),
        "subscription_id": sub.get("id"),
        "user_id": sub.get("user_id"),
        "alerts_enabled": sub.get("alerts_enabled", True),
        "is_archived": sub.get("is_archived", False),
        "last_alerted_price": sub.get("last_alerted_price"),
        "user_initial_price": user_init,
        "user_overall_change": user_overall_change,
        "user_overall_change_pct": user_overall_pct,
        "subscribed_at": sub.get("created_at"),
    }


def get_all_products(user_id: Optional[str] = None, include_archived: bool = True) -> List[Dict[str, Any]]:
    """
    Returns all products tracked by user_id, merged with canonical product data.
    """
    client = get_firestore_client()
    items = []

    if client:
        try:
            # Query user's subscriptions
            query = client.collection("user_subscriptions")
            if user_id:
                query = query.where("user_id", "==", user_id)
            
            subs = list(query.stream())
            for sub_doc in subs:
                sub_data = sub_doc.to_dict()
                if not include_archived and sub_data.get("is_archived"):
                    continue

                prod_id = sub_data.get("product_id")
                canon_doc = client.collection("canonical_products").document(prod_id).get()
                if canon_doc.exists:
                    canon_data = canon_doc.to_dict()
                    canon_data["id"] = canon_doc.id
                    merged = merge_canonical_and_subscription(canon_data, sub_data)
                    items.append(merged)
        except Exception as e:
            logger.error(f"Error fetching user products: {e}")
    else:
        for sub in _LOCAL_SUBSCRIPTIONS.values():
            if user_id and sub.get("user_id") not in (user_id, "default_user"):
                continue
            if not include_archived and sub.get("is_archived"):
                continue
            prod_id = sub.get("product_id")
            canon = _LOCAL_CANONICAL.get(prod_id)
            if canon:
                items.append(merge_canonical_and_subscription(canon, sub))

    # Sort by creation date descending
    items.sort(key=lambda x: str(x.get("subscribed_at") or x.get("created_at", "")), reverse=True)
    return items


def get_product(product_id: str, user_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Retrieves a single product for a user, combining canonical catalog data with subscription."""
    canon = get_canonical_product(product_id)
    if not canon:
        return None

    if user_id:
        sub_id = generate_subscription_id(user_id, product_id)
        client = get_firestore_client()
        sub_data = None
        if client:
            try:
                sub_doc = client.collection("user_subscriptions").document(sub_id).get()
                if sub_doc.exists:
                    sub_data = sub_doc.to_dict()
            except Exception as e:
                logger.error(f"Error fetching subscription {sub_id}: {e}")
        else:
            sub_data = _LOCAL_SUBSCRIPTIONS.get(sub_id)

        if not sub_data:
            # User is not subscribed to this product
            return None
        return merge_canonical_and_subscription(canon, sub_data)

    return canon


def save_or_update_product(
    url: str,
    name: str,
    price: Optional[float],
    currency: str = "$",
    image_url: Optional[str] = None,
    user_id: str = "default_user",
) -> Dict[str, Any]:
    """
    High-level entry point called by app routes:
    1. Saves/updates the shared canonical product entry.
    2. Subscribes the user to watch this product.
    Returns the composite product dictionary.
    """
    clean_url = canonicalize_url(url)
    prod_id = generate_product_id(clean_url)

    # 1. Update/create canonical entry
    canon = save_or_update_canonical_product(
        url=clean_url,
        name=name,
        price=price,
        currency=currency,
        image_url=image_url,
    )

    # 2. Subscribe user
    sub = subscribe_user_to_product(
        user_id=user_id,
        product_id=prod_id,
        user_initial_price=price or canon.get("current_price"),
    )

    return merge_canonical_and_subscription(canon, sub)


def toggle_product_alert(product_id: str, enabled: Optional[bool] = None, user_id: Optional[str] = None) -> bool:
    """Toggles alert for a product subscription."""
    if not user_id:
        user_id = "default_user"
    return toggle_subscription_alert(user_id, product_id, enabled)


def toggle_product_archive(product_id: str, is_archived: Optional[bool] = None, user_id: Optional[str] = None) -> bool:
    """Toggles archive state for a product subscription."""
    if not user_id:
        user_id = "default_user"
    return toggle_subscription_archive(user_id, product_id, is_archived)


def delete_product(product_id: str, user_id: Optional[str] = None) -> bool:
    """Unsubscribes a user from a product."""
    if not user_id:
        user_id = "default_user"
    return unsubscribe_user_from_product(user_id, product_id)


def update_last_alerted_price(product_id: str, price: float, user_id: Optional[str] = None):
    """Updates the last alerted price on a user subscription."""
    if not user_id:
        user_id = "default_user"
    update_subscription_last_alerted_price(user_id, product_id, price)


def get_price_history(product_id: str, limit: int = 100) -> List[Dict[str, Any]]:
    """Retrieves the master historical price timeline for a product from canonical catalog."""
    client = get_firestore_client()
    if client:
        try:
            docs = (
                client.collection("canonical_products")
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


# ---------------------------------------------------------------------------
# Background Scheduler Batch Helpers
# ---------------------------------------------------------------------------

def get_active_subscriptions_and_canonical_products() -> List[Dict[str, Any]]:
    """
    Returns unique canonical products that have at least one active (unarchived) user subscription,
    along with their list of interested subscriber user IDs.
    """
    client = get_firestore_client()
    active_map: Dict[str, Dict[str, Any]] = {}

    if client:
        try:
            # Query all active subscriptions
            subs = client.collection("user_subscriptions").where("is_archived", "==", False).stream()
            for sub_doc in subs:
                sub = sub_doc.to_dict()
                prod_id = sub.get("product_id")
                if prod_id not in active_map:
                    canon_doc = client.collection("canonical_products").document(prod_id).get()
                    if canon_doc.exists:
                        c = canon_doc.to_dict()
                        c["id"] = prod_id
                        c["subscribers"] = []
                        active_map[prod_id] = c
                if prod_id in active_map:
                    active_map[prod_id]["subscribers"].append(sub)
        except Exception as e:
            logger.error(f"Error fetching active subscriptions for scrape-all: {e}")
    else:
        for sub in _LOCAL_SUBSCRIPTIONS.values():
            if not sub.get("is_archived"):
                prod_id = sub.get("product_id")
                if prod_id not in active_map:
                    canon = _LOCAL_CANONICAL.get(prod_id)
                    if canon:
                        c = dict(canon)
                        c["subscribers"] = []
                        active_map[prod_id] = c
                if prod_id in active_map:
                    active_map[prod_id]["subscribers"].append(sub)

    return list(active_map.values())


def get_all_active_products_all_users() -> List[Dict[str, Any]]:
    """Returns all active product subscriptions merged with their canonical products across all users."""
    items = []
    canon_items = get_active_subscriptions_and_canonical_products()
    for c in canon_items:
        for sub in c.get("subscribers", []):
            items.append(merge_canonical_and_subscription(c, sub))
    return items


# ---------------------------------------------------------------------------
# User & Alert Channels Management
# ---------------------------------------------------------------------------

def get_or_create_user(user_info: Dict[str, Any]) -> Dict[str, Any]:
    """Retrieves or provisions a user profile with personal alert channel settings."""
    client = get_firestore_client()
    now = datetime.now(timezone.utc)
    user_id = str(user_info.get("id") or user_info.get("sub") or user_info.get("email"))
    email = user_info.get("email", "")
    name = user_info.get("name") or (email.split("@")[0] if email else "User")
    picture = user_info.get("picture", "")

    default_channels = {
        "google_chat": {
            "enabled": False,
            "webhook_url": "",
        },
        "email": {
            "enabled": False,
            "address": email,
        },
        "slack": {
            "enabled": False,
            "webhook_url": "",
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
                    "alert_channels": default_channels,
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
            "alert_channels": default_channels,
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
    
    return {
        "google_chat": {
            "enabled": False,
            "webhook_url": "",
        },
        "email": {
            "enabled": False,
            "address": user.get("email", "") if user else "",
        }
    }


def update_user_alert_channels(user_id: str, alert_channels: Dict[str, Any]) -> bool:
    """Updates the alert channels for a specific user."""
    client = get_firestore_client()
    if client:
        try:
            client.collection("users").document(user_id).set({
                "alert_channels": alert_channels
            }, merge=True)
            return True
        except Exception as e:
            logger.error(f"Error updating alert channels for {user_id}: {e}")
            return False
    else:
        if user_id not in _LOCAL_USERS:
            _LOCAL_USERS[user_id] = {"id": user_id}
        _LOCAL_USERS[user_id]["alert_channels"] = alert_channels
        return True


# ---------------------------------------------------------------------------
# Global Settings & OAuth Configuration
# ---------------------------------------------------------------------------

def get_global_settings() -> Dict[str, Any]:
    """Fetches global settings from Firestore."""
    client = get_firestore_client()
    default_webhook = os.environ.get("GOOGLE_CHAT_WEBHOOK", "")
    if client:
        try:
            doc = client.collection("settings").document("app_config").get()
            if doc.exists:
                data = doc.to_dict()
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
    """Retrieves Google OAuth client credentials."""
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
    """Saves Google OAuth client credentials to Firestore."""
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
