"""
Automated Test Suite for Canonical Products + User Subscriptions Architecture.
Tests:
1. URL canonicalization and deterministic ID hashing
2. User 1 tracks a product (creates canonical record + subscription)
3. User 2 tracks same product with query params (instant match, inherits history)
4. Per-user data segmentation (alert toggles, archives, watchlist isolation)
5. Scheduled scrape deduplication (1 scrape, fans out to active subscribers)
6. Subscriber alert dispatching & spam prevention (last_alerted_price)
7. Deletion isolation (User 1 delete doesn't touch User 2 or canonical product)
"""

import os
import sys
from unittest.mock import patch, MagicMock

# Force offline/in-memory mode for fast local verification
os.environ["FLASK_SECRET_KEY"] = "test-secret"
os.environ["CRON_SECRET"] = "test-cron-secret"

import db
import scraper
from app import app


def test_url_canonicalization():
    print("\n--- Test 1: URL Canonicalization ---")
    raw_amazon = "https://www.amazon.com/Sony-WH-1000XM5-Canceling-Headphones-Hands-Free/dp/B09XS7JWHH/ref=sr_1_1?crid=123&keywords=sony&qid=456&tag=affil-20"
    canon_amazon = scraper.canonicalize_url(raw_amazon)
    assert canon_amazon == "https://amazon.com/dp/B09XS7JWHH", f"Unexpected Amazon canonical: {canon_amazon}"
    
    id1 = db.generate_product_id(raw_amazon)
    id2 = db.generate_product_id("https://amazon.com/dp/B09XS7JWHH?utm_source=email")
    assert id1 == id2, f"Product IDs do not match: {id1} vs {id2}"
    print(f"[PASS] Amazon URL successfully normalized to: {canon_amazon} (ID: {id1})")

    raw_costco = "https://www.costco.com/kirkland-signature-organic-peanut-butter.product.100381404.html?AID=123&PID=456"
    canon_costco = scraper.canonicalize_url(raw_costco)
    assert canon_costco == "https://costco.com/product.100381404.html", f"Unexpected Costco canonical: {canon_costco}"
    print(f"[PASS] Costco URL successfully normalized to: {canon_costco}")


def test_dual_user_tracking_and_instant_history():
    print("\n--- Test 2: Dual User Tracking & Instant History Sharing ---")
    
    # User 1 (Alice) tracks an item
    alice = db.get_or_create_user({"id": "user_alice", "email": "alice@example.com", "name": "Alice"})
    bob = db.get_or_create_user({"id": "user_bob", "email": "bob@example.com", "name": "Bob"})

    # Setup alert channels
    db.update_user_alert_channels("user_alice", {
        "google_chat": {"enabled": True, "webhook_url": "https://chat.googleapis.com/v1/spaces/ALICE_SPACE/messages"}
    })
    db.update_user_alert_channels("user_bob", {
        "google_chat": {"enabled": True, "webhook_url": "https://chat.googleapis.com/v1/spaces/BOB_SPACE/messages"}
    })

    test_url = "https://www.amazon.com/dp/B09XS7JWHH"
    prod_id = db.generate_product_id(test_url)

    # Alice tracks first at $399.99
    print("Alice tracking product at $399.99...")
    alice_prod = db.save_or_update_product(
        url=test_url,
        name="Sony WH-1000XM5",
        price=399.99,
        currency="$",
        image_url="https://images.amazon.com/sony.jpg",
        user_id="user_alice"
    )
    assert alice_prod["current_price"] == 399.99
    assert alice_prod["user_id"] == "user_alice"

    # Days later, price drops to $348.00 in canonical
    print("Price drops in canonical catalog to $348.00...")
    db.save_or_update_canonical_product(
        url=test_url,
        name="Sony WH-1000XM5",
        price=348.00,
        currency="$"
    )

    # Bob tracks the same item later with referral tags:
    bob_raw_url = "https://www.amazon.com/Sony-WH-1000XM5/dp/B09XS7JWHH?tag=myfriend-20&utm_source=twitter"
    bob_canon_url = scraper.canonicalize_url(bob_raw_url)
    bob_prod_id = db.generate_product_id(bob_canon_url)
    assert bob_prod_id == prod_id, "Bob did not generate identical canonical product ID!"

    # Bob subscribes directly
    existing_canonical = db.get_canonical_product(bob_prod_id)
    assert existing_canonical is not None, "Canonical product should exist!"
    
    bob_sub = db.subscribe_user_to_product(
        user_id="user_bob",
        product_id=bob_prod_id,
        user_initial_price=existing_canonical["current_price"]
    )

    # Check Bob's merged product
    bob_prod = db.get_product(prod_id, user_id="user_bob")
    assert bob_prod is not None
    assert bob_prod["current_price"] == 348.00
    assert bob_prod["user_initial_price"] == 348.00  # Bob's personal baseline
    assert bob_prod["lowest_price"] == 348.00
    assert bob_prod["highest_price"] == 399.99

    # Check master history contains both 399.99 and 348.00
    history = db.get_price_history(prod_id)
    prices = [h["price"] for h in history]
    assert 399.99 in prices and 348.00 in prices, f"History missing entries: {prices}"
    print(f"[PASS] Bob instantly inherits complete history with {len(history)} price checkpoints without re-scraping!")


def test_user_data_segmentation():
    print("\n--- Test 3: User Data Segmentation & Isolation ---")
    test_url = "https://www.amazon.com/dp/B09XS7JWHH"
    prod_id = db.generate_product_id(test_url)

    # Alice toggles alerts OFF
    db.toggle_product_alert(prod_id, enabled=False, user_id="user_alice")

    # Verify Alice has alerts disabled, but Bob STILL has alerts enabled
    alice_item = db.get_product(prod_id, user_id="user_alice")
    bob_item = db.get_product(prod_id, user_id="user_bob")

    assert alice_item["alerts_enabled"] is False, "Alice's alert should be disabled!"
    assert bob_item["alerts_enabled"] is True, "Bob's alert should remain enabled!"
    print("[PASS] Alert toggles are strictly isolated per-user.")

    # Alice archives the product
    db.toggle_product_archive(prod_id, is_archived=True, user_id="user_alice")
    alice_active = db.get_all_products(user_id="user_alice", include_archived=False)
    bob_active = db.get_all_products(user_id="user_bob", include_archived=False)

    assert len(alice_active) == 0, "Alice should have 0 active products!"
    assert len(bob_active) == 1, "Bob should still have 1 active product!"
    print("[PASS] Archiving product is strictly isolated per-user.")

    # Unarchive Alice for the next tests
    db.toggle_product_archive(prod_id, is_archived=False, user_id="user_alice")
    db.toggle_product_alert(prod_id, enabled=True, user_id="user_alice")


def test_scheduled_batch_scrape_and_fanout():
    print("\n--- Test 4: Scheduled Batch Scrape & Multi-User Alert Fanout ---")
    test_url = "https://www.amazon.com/dp/B09XS7JWHH"
    prod_id = db.generate_product_id(test_url)

    # Mock scraper returning a new price drop: $299.99 (down from $348.00)
    mock_scraped = {
        "url": test_url,
        "name": "Sony WH-1000XM5",
        "price": 299.99,
        "currency": "$",
        "image_url": "https://images.amazon.com/sony.jpg",
        "error": None
    }

    client = app.test_client()

    with patch("app.scrape_product", return_value=mock_scraped) as mock_scrape, \
         patch("app.dispatch_user_alerts") as mock_dispatch:
        
        mock_dispatch.return_value = {"dispatched": ["google_chat"]}

        resp = client.post("/api/scrape-all", headers={"X-Cron-Secret": "test-cron-secret"})
        assert resp.status_code == 200, f"Scrape all failed: {resp.data}"
        data = resp.get_json()

        # Retailer should be scraped ONCE
        assert mock_scrape.call_count == 1, f"Retailer scraped {mock_scrape.call_count} times instead of 1!"
        print("[PASS] Retailer URL was scraped exactly ONCE for canonical product.")

        # Dispatch should be called TWICE (once for Alice, once for Bob)
        assert mock_dispatch.call_count == 2, f"Alert dispatched {mock_dispatch.call_count} times instead of 2!"
        print(f"[PASS] Price drop alert fanned out to both Alice and Bob ({data['alerts_sent']} alerts recorded).")

        # Verify last_alerted_price is updated to prevent spam
        alice_sub = db._LOCAL_SUBSCRIPTIONS[db.generate_subscription_id("user_alice", prod_id)]
        bob_sub = db._LOCAL_SUBSCRIPTIONS[db.generate_subscription_id("user_bob", prod_id)]
        assert alice_sub["last_alerted_price"] == 299.99
        assert bob_sub["last_alerted_price"] == 299.99
        print("[PASS] last_alerted_price recorded on each subscription to prevent alert spam.")


def test_unsubscribe_and_catalog_preservation():
    print("\n--- Test 5: Unsubscribe / Delete Isolation ---")
    test_url = "https://www.amazon.com/dp/B09XS7JWHH"
    prod_id = db.generate_product_id(test_url)

    # Alice deletes the product
    db.delete_product(prod_id, user_id="user_alice")

    # Alice no longer has it
    alice_prod = db.get_product(prod_id, user_id="user_alice")
    assert alice_prod is None, "Alice should no longer have this product!"

    # Bob STILL has it
    bob_prod = db.get_product(prod_id, user_id="user_bob")
    assert bob_prod is not None, "Bob should still have this product!"

    # Master canonical product and full history STILL exist
    canonical = db.get_canonical_product(prod_id)
    assert canonical is not None, "Canonical catalog product should still exist!"
    history = db.get_price_history(prod_id)
    assert len(history) >= 2, "Canonical price history must remain preserved!"
    print("[PASS] Alice's deletion unsubscribed Alice without touching Bob or the canonical history!")


def test_flask_track_instant_match():
    print("\n--- Test 6: Flask /track Fast-Path (Instant Match without Scraping) ---")
    test_url = "https://www.amazon.com/dp/B09XS7JWHH"
    prod_id = db.generate_product_id(test_url)

    # Provision Charlie
    db.get_or_create_user({"id": "user_charlie", "email": "charlie@example.com", "name": "Charlie"})

    client = app.test_client()

    # Set Charlie's session
    with client.session_transaction() as sess:
        sess["user"] = {"id": "user_charlie", "name": "Charlie", "email": "charlie@example.com"}

    # Track URL with extra tracking query params
    incoming_url = "https://www.amazon.com/Sony-Headphones/dp/B09XS7JWHH?utm_source=newsletter&utm_medium=email"

    with patch("app.scrape_product") as mock_scrape:
        resp = client.post("/track", data={"url": incoming_url}, follow_redirects=False)
        assert resp.status_code == 302, f"Expected 302 redirect, got {resp.status_code}"
        assert f"/product/{prod_id}" in resp.headers["Location"], f"Expected redirect to product detail, got {resp.headers['Location']}"
        
        # Verify scraping was NEVER triggered because it was already canonical!
        assert mock_scrape.call_count == 0, f"Scrape should NOT have been called, but was called {mock_scrape.call_count} times!"
        print("[PASS] Flask /track detected canonical product and instantly subscribed Charlie in 0ms without scraping!")

    # Verify Charlie has the product and its price history
    charlie_prod = db.get_product(prod_id, user_id="user_charlie")
    assert charlie_prod is not None
    assert charlie_prod["name"] == "Sony WH-1000XM5"
    print("[PASS] Charlie's subscription is live and linked to canonical data!")


if __name__ == "__main__":
    test_url_canonicalization()
    test_dual_user_tracking_and_instant_history()
    test_user_data_segmentation()
    test_scheduled_batch_scrape_and_fanout()
    test_unsubscribe_and_catalog_preservation()
    test_flask_track_instant_match()
    print("\n=======================================================")
    print("  ALL ARCHITECTURAL INTEGRATION TESTS PASSED 100%! [SUCCESS]")
    print("=======================================================\n")
