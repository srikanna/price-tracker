"""
Web scraper module for extracting product names and prices from e-commerce URLs.
Uses a multi-tiered extraction strategy:
1. JSON-LD structured data (Schema.org Product)
2. Open Graph & Twitter meta tags
3. Microdata (itemprop attributes)
4. Common CSS selectors (generic & site-specific)
5. Fallback HTML heuristics with robust regex cleaning
"""

import json
import logging
import re
from typing import Dict, Any, Optional
import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# Realistic browser headers to prevent basic bot-blocking
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
    "DNT": "1",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
}


def clean_price(price_raw: Any) -> Optional[float]:
    """Clean a raw price string or number into a float."""
    if price_raw is None:
        return None

    if isinstance(price_raw, (int, float)):
        return float(price_raw)

    price_str = str(price_raw).strip()
    if not price_str:
        return None

    # If it's a range (e.g. "$19.99 - $29.99"), grab the first price
    if "-" in price_str:
        price_str = price_str.split("-")[0].strip()

    # Extract digits with optional decimal point
    # Supports formats like "$1,234.56" or "1.234,56 €"
    # Replace comma if it acts as a thousands separator
    match = re.search(r"(\d+(?:[,\.]\d+)*)", price_str)
    if not match:
        return None

    num_part = match.group(1)

    # Normalize European format 1.234,56 vs US 1,234.56
    if "," in num_part and "." in num_part:
        if num_part.find(",") < num_part.find("."):
            # US style: 1,234.56 -> remove commas
            num_part = num_part.replace(",", "")
        else:
            # European style: 1.234,56 -> remove dots, change comma to dot
            num_part = num_part.replace(".", "").replace(",", ".")
    elif "," in num_part:
        # Check if comma is decimal (e.g. 19,99) or thousands (e.g. 1,000)
        parts = num_part.split(",")
        if len(parts) == 2 and len(parts[1]) == 2:
            num_part = f"{parts[0]}.{parts[1]}"
        else:
            num_part = num_part.replace(",", "")

    try:
        val = float(num_part)
        return round(val, 2)
    except ValueError:
        return None


def detect_currency(text: str, default: str = "$") -> str:
    """Detect currency symbol or code from string."""
    currency_map = {
        "$": "$",
        "USD": "$",
        "€": "€",
        "EUR": "€",
        "£": "£",
        "GBP": "£",
        "¥": "¥",
        "JPY": "¥",
        "CNY": "¥",
        "₹": "₹",
        "INR": "₹",
        "CAD": "CAD $",
        "AUD": "AUD $",
    }
    for key, symbol in currency_map.items():
        if key in text.upper():
            return symbol
    return default


def clean_title(title: str) -> str:
    """Clean extra store branding or whitespace from product titles."""
    if not title:
        return "Unknown Product"
    title = re.sub(r"\s+", " ", title).strip()
    # Strip common site suffixes like " | Amazon.com", " - Walmart.com"
    title = re.sub(r"\s*[\-\|\:]\s*(Amazon|Walmart|eBay|Target|Best Buy|Etsy|AliExpress|Apple).*$", "", title, flags=re.IGNORECASE)
    return title.strip() or "Tracked Product"


def extract_from_json_ld(soup: BeautifulSoup) -> Dict[str, Any]:
    """Attempt to extract product name, price, currency, and image from JSON-LD."""
    scripts = soup.find_all("script", type="application/ld+json")
    for script in scripts:
        try:
            if not script.string:
                continue
            data = json.loads(script.string.strip())

            # Handle list of items or single item
            items = data if isinstance(data, list) else [data]
            # Handle @graph notation
            if isinstance(data, dict) and "@graph" in data:
                items = data["@graph"]

            for item in items:
                if not isinstance(item, dict):
                    continue
                type_val = item.get("@type", "")
                is_product = False
                if isinstance(type_val, str) and type_val.lower() == "product":
                    is_product = True
                elif isinstance(type_val, list) and any(t.lower() == "product" for t in type_val if isinstance(t, str)):
                    is_product = True

                if is_product:
                    name = item.get("name")
                    image = item.get("image")
                    if isinstance(image, list) and image:
                        image = image[0]
                    if isinstance(image, dict):
                        image = image.get("url")

                    price = None
                    currency = "$"
                    offers = item.get("offers")

                    if isinstance(offers, dict):
                        price = offers.get("price") or offers.get("lowPrice")
                        if "priceCurrency" in offers:
                            currency = detect_currency(offers["priceCurrency"])
                    elif isinstance(offers, list) and offers:
                        first_offer = offers[0]
                        if isinstance(first_offer, dict):
                            price = first_offer.get("price") or first_offer.get("lowPrice")
                            if "priceCurrency" in first_offer:
                                currency = detect_currency(first_offer["priceCurrency"])

                    cleaned_price = clean_price(price)
                    if name and cleaned_price is not None:
                        return {
                            "name": clean_title(name),
                            "price": cleaned_price,
                            "currency": currency,
                            "image_url": str(image) if image else None,
                        }
        except Exception as e:
            logger.debug("Error parsing JSON-LD: %s", e)
            continue
    return {}


def extract_from_meta_tags(soup: BeautifulSoup) -> Dict[str, Any]:
    """Extract metadata from OpenGraph and Twitter tags."""
    result: Dict[str, Any] = {}

    # Product Name
    og_title = (
        soup.find("meta", property="og:title")
        or soup.find("meta", attrs={"name": "twitter:title"})
        or soup.find("meta", attrs={"name": "title"})
    )
    if og_title and og_title.get("content"):
        result["name"] = clean_title(og_title["content"])

    # Product Price
    og_price = (
        soup.find("meta", property="product:price:amount")
        or soup.find("meta", property="og:price:amount")
        or soup.find("meta", attrs={"name": "price"})
        or soup.find("meta", attrs={"itemprop": "price"})
    )
    if og_price and og_price.get("content"):
        price = clean_price(og_price["content"])
        if price is not None:
            result["price"] = price

    # Currency
    og_currency = (
        soup.find("meta", property="product:price:currency")
        or soup.find("meta", property="og:price:currency")
    )
    if og_currency and og_currency.get("content"):
        result["currency"] = detect_currency(og_currency["content"])

    # Image
    og_image = (
        soup.find("meta", property="og:image")
        or soup.find("meta", attrs={"name": "twitter:image"})
    )
    if og_image and og_image.get("content"):
        result["image_url"] = og_image["content"]

    return result


def extract_from_selectors(soup: BeautifulSoup, base_url: str = "") -> Dict[str, Any]:
    """Extract product data using common e-commerce HTML selectors."""
    result: Dict[str, Any] = {}

    # Common Title Selectors
    title_selectors = [
        "#productTitle",                    # Amazon
        "h1#title",                         # Amazon variant
        "h1[itemprop='name']",              # Generic schema
        "h1.product-title",                 # Shopify / Generic
        "h1.product__title",                # Shopify
        "[data-test='product-title']",      # Target / modern stores
        ".x-item-title__mainTitle",         # eBay
        "h1.heading",
        "h1",
    ]

    for sel in title_selectors:
        el = soup.select_one(sel)
        if el and el.get_text(strip=True):
            result["name"] = clean_title(el.get_text(strip=True))
            break

    # Common Price Selectors
    price_selectors = [
        ".a-price .a-offscreen",            # Amazon main price
        "#priceblock_ourprice",             # Amazon older
        "#priceblock_dealprice",            # Amazon deal
        "#corePrice_feature_div .a-offscreen", # Amazon core price
        "[itemprop='price']",               # Schema.org microdata
        "[data-test='product-price']",      # Target
        ".price-current",                   # Newegg
        ".x-price-primary span",            # eBay
        ".price_color",                     # Books to scrape / WooCommerce
        ".product-price",                   # Generic
        ".price",                           # Generic
        ".current-price",                   # Generic
        ".price__current",                  # Generic
        "*[class*='price']:not(body):not(html)", # Generic price class on any tag
    ]

    for sel in price_selectors:
        elements = soup.select(sel)
        for el in elements:
            # Check content attribute first (e.g. <span itemprop="price" content="29.99">)
            content_val = el.get("content")
            raw_text = content_val if content_val else el.get_text(strip=True)
            price = clean_price(raw_text)
            if price is not None and price > 0:
                result["price"] = price
                result["currency"] = detect_currency(raw_text)
                break
        if "price" in result:
            break

    # Common Image Selectors
    img_selectors = [
        "#landingImage",                    # Amazon
        "#imgBlkFront",                     # Amazon Books
        "[data-test='product-image'] img",  # Target
        ".product__image img",              # Shopify
        "img[itemprop='image']",            # Schema.org
        ".item.active img",                 # Carousels
        ".thumbnail img",
        ".gallery-image",                   # Generic
        "#product_gallery img",
    ]
    for sel in img_selectors:
        el = soup.select_one(sel)
        if el:
            src = el.get("data-src") or el.get("src") or el.get("data-old-hires")
            if src:
                from urllib.parse import urljoin
                result["image_url"] = urljoin(base_url, src)
                break

    return result


def scrape_product(url: str, timeout: int = 15) -> Dict[str, Any]:
    """
    Scrapes the product name and current price from a given URL.
    Returns:
        dict with keys: 'name', 'price', 'currency', 'image_url', 'url', 'error'
    """
    if not url or not (url.startswith("http://") or url.startswith("https://")):
        return {
            "url": url,
            "name": None,
            "price": None,
            "currency": "$",
            "image_url": None,
            "error": "Invalid URL format. Please provide a valid HTTP or HTTPS URL.",
        }

    try:
        response = requests.get(
            url,
            headers=DEFAULT_HEADERS,
            timeout=timeout,
            allow_redirects=True,
        )
        response.raise_for_status()
    except requests.exceptions.Timeout:
        return {"url": url, "name": None, "price": None, "currency": "$", "image_url": None, "error": "Request timed out while reaching URL."}
    except requests.exceptions.RequestException as e:
        return {"url": url, "name": None, "price": None, "currency": "$", "image_url": None, "error": f"Failed to fetch page: {str(e)}"}

    final_url = response.url
    soup = BeautifulSoup(response.text, "html.parser")

    # Step 1: Check JSON-LD
    extracted = extract_from_json_ld(soup)

    # Step 2: Check Meta Tags for missing fields
    meta_data = extract_from_meta_tags(soup)
    for key, val in meta_data.items():
        if key not in extracted or extracted[key] is None:
            extracted[key] = val

    # Step 3: Check CSS Selectors for missing fields
    selector_data = extract_from_selectors(soup, base_url=final_url)
    for key, val in selector_data.items():
        if key not in extracted or extracted[key] is None:
            extracted[key] = val

    # Step 4: Fallback title if still missing
    if not extracted.get("name"):
        if soup.title and soup.title.string:
            extracted["name"] = clean_title(soup.title.string)
        else:
            extracted["name"] = "Tracked Product"

    # Step 5: Fallback price regex search across body if still missing
    if extracted.get("price") is None:
        # Look for patterns like $XX.XX or $X,XXX.XX
        dollar_patterns = re.findall(r"(\$\s*[0-9]{1,4}(?:,[0-9]{3})*(?:\.[0-9]{2})?)", response.text)
        for cand in dollar_patterns:
            p = clean_price(cand)
            if p is not None and 0.5 <= p <= 100000:
                extracted["price"] = p
                extracted["currency"] = "$"
                break

    # If price still couldn't be extracted
    if extracted.get("price") is None:
        return {
            "url": final_url,
            "name": extracted.get("name", "Unknown Product"),
            "price": None,
            "currency": extracted.get("currency", "$"),
            "image_url": extracted.get("image_url"),
            "error": "Could not automatically detect price on this page.",
        }

    return {
        "url": final_url,
        "name": extracted.get("name", "Unknown Product"),
        "price": extracted["price"],
        "currency": extracted.get("currency", "$"),
        "image_url": extracted.get("image_url"),
        "error": None,
    }
