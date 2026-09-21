"""
Alert Notification Engine.
Supports multi-channel alerts (Google Chat Spaces Card v2, Email, Slack, etc.) segmented per user.
"""

import logging
from typing import Dict, Any, Optional, List
import requests

logger = logging.getLogger(__name__)


def send_google_chat_alert(
    webhook_url: str,
    product: Dict[str, Any],
    app_base_url: Optional[str] = "https://price-tracker-370743893608.us-central1.run.app"
) -> bool:
    """
    Dispatches an interactive Card v2 alert to a Google Chat Space webhook.
    """
    if not webhook_url or not webhook_url.startswith("https://chat.googleapis.com/"):
        logger.warning(f"Invalid or missing Google Chat webhook URL: {webhook_url}")
        return False

    name = product.get("name", "Tracked Product")
    currency = product.get("currency", "$")
    current_price = product.get("current_price", 0.0)
    prev_price = product.get("previous_price") or product.get("initial_price") or current_price
    recent_change = product.get("recent_change", 0.0)
    recent_pct = product.get("recent_change_pct", 0.0)
    site_name = product.get("site_name", "Retailer")
    image_url = product.get("image_url")
    product_url = product.get("url")
    product_id = product.get("id")

    detail_url = f"{app_base_url}/product/{product_id}" if app_base_url and product_id else product_url

    # Format change display
    drop_text = f"▼ {currency}{abs(recent_change):.2f} (-{abs(recent_pct):.1f}%)" if recent_change < 0 else f"{currency}{current_price:.2f}"

    # Build widgets
    widgets = []

    # Product image widget if available
    if image_url and image_url.startswith("http"):
        widgets.append({
            "image": {
                "imageUrl": image_url,
                "altText": name
            }
        })

    # Pricing details widget
    widgets.append({
        "decoratedText": {
            "topLabel": f"PRICE DROP ON {site_name.upper()}",
            "text": f"<b><font color='#16a34a'>{currency}{current_price:,.2f}</font></b>  <font color='#15803d'>({drop_text})</font>",
            "bottomLabel": f"Previous: {currency}{prev_price:,.2f}  |  All-Time Floor: {currency}{product.get('lowest_price', current_price):,.2f}",
            "startIcon": {
                "knownIcon": "DOLLAR"
            }
        }
    })

    # Action buttons
    buttons = [
        {
            "text": f"Buy on {site_name}",
            "onClick": {
                "openLink": {
                    "url": product_url
                }
            }
        }
    ]

    if detail_url:
        buttons.append({
            "text": "View Price Chart",
            "onClick": {
                "openLink": {
                    "url": detail_url
                }
            }
        })

    widgets.append({
        "buttonList": {
            "buttons": buttons
        }
    })

    card_payload = {
        "cardsV2": [
            {
                "cardId": f"price-drop-{product_id}",
                "card": {
                    "header": {
                        "title": f"🔥 Price Drop: {name[:40]}{'...' if len(name) > 40 else ''}",
                        "subtitle": f"Store: {site_name} | Verified Alert",
                        "imageUrl": "https://fonts.gstatic.com/s/i/short-term/release/googlesymbols/trending_down/default/48px.svg",
                        "imageType": "CIRCLE"
                    },
                    "sections": [
                        {
                            "widgets": widgets
                        }
                    ]
                }
            }
        ]
    }

    try:
        resp = requests.post(webhook_url, json=card_payload, timeout=10)
        if resp.status_code == 200:
            logger.info(f"Successfully sent Google Chat alert for product {product_id}")
            return True
        else:
            logger.error(f"Google Chat webhook returned {resp.status_code}: {resp.text}")
            return False
    except Exception as e:
        logger.error(f"Failed to send Google Chat alert: {e}")
        return False


def send_test_google_chat_alert(webhook_url: str) -> bool:
    """Sends a sample test alert card to verify the Google Chat Space connection."""
    sample_product = {
        "id": "test-item-sample",
        "name": "Kirkland Signature Ultra Clean Detergent (Sample)",
        "currency": "$",
        "current_price": 19.99,
        "previous_price": 24.99,
        "lowest_price": 19.99,
        "recent_change": -5.00,
        "recent_change_pct": -20.0,
        "site_name": "Costco",
        "url": "https://www.costco.com",
        "image_url": "https://upload.wikimedia.org/wikipedia/commons/5/53/Costco_Wholesale_logo.svg",
    }
    return send_google_chat_alert(webhook_url, sample_product)


def dispatch_user_alerts(
    user_alert_channels: Dict[str, Any],
    product: Dict[str, Any],
    app_base_url: Optional[str] = "https://price-tracker-370743893608.us-central1.run.app"
) -> Dict[str, Any]:
    """
    Dispatches price drop alerts to all enabled alert channels configured for a user.
    Extensible for Google Chat, Email, Slack, Discord, SMS, RCS, etc.
    """
    results = {
        "dispatched": [],
        "failed": [],
        "skipped": []
    }

    if not user_alert_channels:
        return results

    # Channel 1: Google Chat Spaces
    chat_cfg = user_alert_channels.get("google_chat", {})
    if chat_cfg.get("enabled", True):
        webhook = chat_cfg.get("webhook_url", "").strip()
        if webhook:
            success = send_google_chat_alert(webhook, product, app_base_url=app_base_url)
            if success:
                results["dispatched"].append("google_chat")
            else:
                results["failed"].append("google_chat")
        else:
            results["skipped"].append("google_chat_no_webhook")
    else:
        results["skipped"].append("google_chat_disabled")

    # Channel 2: Email (Extensible placeholder)
    email_cfg = user_alert_channels.get("email", {})
    if email_cfg.get("enabled", False) and email_cfg.get("address"):
        # Placeholder for future SMTP / SendGrid / Cloud Tasks dispatch
        logger.info(f"Email channel enabled for {email_cfg.get('address')} (Ready for provider integration)")
        results["skipped"].append("email_provider_not_configured")

    # Channel 3: Slack (Extensible placeholder)
    slack_cfg = user_alert_channels.get("slack", {})
    if slack_cfg.get("enabled", False) and slack_cfg.get("webhook_url"):
        # Placeholder for future Slack webhook integration
        logger.info(f"Slack channel enabled (Ready for provider integration)")
        results["skipped"].append("slack_provider_not_configured")

    return results
