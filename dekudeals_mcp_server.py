#!/usr/bin/env python3
"""
Deku Deals MCP Server

MCP server for searching games, tracking prices, and managing wishlists
on dekudeals.com (Canadian Nintendo eShop focus).

Auth: Session cookie from MCP Auth Bridge extension at ~/.mcp-credentials/dekudeals.json
"""

import json
import os
import re
import time
import logging
from pathlib import Path
from typing import Optional
from datetime import datetime, timezone

import httpx
from bs4 import BeautifulSoup
from mcp.server.fastmcp import FastMCP

# ─── Configuration ──────────────────────────────────────────────────────────

CREDENTIALS_PATH = Path(
    os.environ.get("DEKUDEALS_CREDENTIALS", "~/.mcp-credentials/dekudeals.json")
).expanduser()

BASE_URL = "https://www.dekudeals.com"
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("dekudeals-mcp")

# ─── UUID Cache ─────────────────────────────────────────────────────────────

_uuid_cache: dict[str, str] = {}

# ─── Auth ───────────────────────────────────────────────────────────────────

def load_cookies() -> dict[str, str]:
    """Load session cookies from the credential file written by MCP Auth Bridge."""
    if not CREDENTIALS_PATH.exists():
        return {}
    try:
        data = json.loads(CREDENTIALS_PATH.read_text())
        return data.get("cookies", {})
    except (json.JSONDecodeError, KeyError):
        return {}


def get_client(require_auth: bool = False) -> httpx.Client:
    """Create an HTTP client with session cookies if available."""
    cookies = load_cookies()

    if require_auth and not cookies:
        raise ValueError(
            "No Deku Deals session found. Open dekudeals.com in Chrome, "
            "log in, and click 'Save Deku Deals' in the MCP Auth Bridge extension."
        )

    # Build cookie header string
    cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items()) if cookies else ""

    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-CA,en;q=0.9",
    }
    if cookie_str:
        headers["Cookie"] = cookie_str

    return httpx.Client(
        base_url=BASE_URL,
        headers=headers,
        follow_redirects=True,
        timeout=30.0
    )


def check_auth_redirect(response: httpx.Response) -> bool:
    """Check if we got redirected to a login page (session expired)."""
    return "/login" in str(response.url) or "/sign_in" in str(response.url)


# ─── HTML Parsing Helpers ───────────────────────────────────────────────────

def parse_price(text: str) -> Optional[float]:
    """Parse a price string like '$19.99' or 'C$19.99' into a float."""
    if not text:
        return None
    match = re.search(r"\$?([\d,]+\.?\d*)", text.strip())
    if match:
        return float(match.group(1).replace(",", ""))
    return None


def parse_search_results(html: str) -> list[dict]:
    """Parse game cards from search results HTML."""
    soup = BeautifulSoup(html, "html.parser")
    games = []

    # Search results are in .search-result-row or similar game card elements
    cards = soup.select(".main-content .row .col a[href^='/items/']")
    if not cards:
        # Try alternative selectors
        cards = soup.select("a.main-link[href^='/items/']")
    if not cards:
        # Broader: any link to /items/ that contains game info
        cards = soup.select("a[href^='/items/']")

    seen_slugs = set()
    for card in cards:
        href = card.get("href", "")
        slug = href.replace("/items/", "").strip("/")
        if not slug or slug in seen_slugs:
            continue
        seen_slugs.add(slug)

        # Try to find game name
        name_el = card.select_one("h2, h3, .name, strong")
        name = name_el.get_text(strip=True) if name_el else card.get_text(strip=True)[:80]

        if not name or len(name) < 2:
            continue

        # Try to find price info from the card's parent context
        parent = card.find_parent("div", class_=re.compile(r"col|card|item"))
        current_price = None
        regular_price = None
        discount_pct = None

        if parent:
            # Look for price elements
            price_els = parent.select(".price, .sale-price, [class*='price']")
            for pel in price_els:
                p = parse_price(pel.get_text())
                if p is not None:
                    if current_price is None:
                        current_price = p
                    elif regular_price is None and p > current_price:
                        regular_price = p

            # Look for discount badge
            discount_el = parent.select_one(".badge, [class*='discount'], [class*='percent']")
            if discount_el:
                dmatch = re.search(r"-?(\d+)%", discount_el.get_text())
                if dmatch:
                    discount_pct = int(dmatch.group(1))

        games.append({
            "name": name,
            "slug": slug,
            "current_price": current_price,
            "regular_price": regular_price,
            "discount_pct": discount_pct,
            "url": f"{BASE_URL}/items/{slug}"
        })

    return games


def parse_game_details(html: str, slug: str) -> dict:
    """Parse a game details page for prices, UUID, metadata."""
    soup = BeautifulSoup(html, "html.parser")
    details = {
        "slug": slug,
        "url": f"{BASE_URL}/items/{slug}",
        "name": None,
        "uuid": None,
        "prices": [],
        "msrp": None,
        "release_date": None,
        "metacritic_score": None,
        "on_wishlist": False,
        "on_collection": False,
        "description": None
    }

    # Game name
    title_el = soup.select_one("h1")
    if title_el:
        details["name"] = title_el.get_text(strip=True)

    # UUID from watch form action: /items/{uuid}/watch
    watch_form = soup.select_one("form[action*='/watch']")
    if watch_form:
        action = watch_form.get("action", "")
        uuid_match = re.search(r"/items/([a-f0-9-]+)/watch", action)
        if uuid_match:
            details["uuid"] = uuid_match.group(1)
            _uuid_cache[slug] = details["uuid"]

    # Also check for own form for UUID
    if not details["uuid"]:
        own_form = soup.select_one("form[action*='/own']")
        if own_form:
            action = own_form.get("action", "")
            uuid_match = re.search(r"/items/([a-f0-9-]+)/own", action)
            if uuid_match:
                details["uuid"] = uuid_match.group(1)
                _uuid_cache[slug] = details["uuid"]

    # Check wishlist/collection status
    # When on wishlist, the page shows "<span>On wishlist</span>" text
    page_text = soup.get_text().lower()
    if "on wishlist" in page_text:
        details["on_wishlist"] = True
    if "in collection" in page_text or "owned" in page_text:
        # Check more specifically for collection status
        for span in soup.select("span"):
            if span.get_text(strip=True).lower() in ("in collection", "owned"):
                details["on_collection"] = True
                break

    # Prices from various stores
    price_rows = soup.select(".price-table tr, .prices-row, [class*='store-price']")
    for row in price_rows:
        store_el = row.select_one("td:first-child, .store-name, [class*='store']")
        price_el = row.select_one("td:last-child, .price, [class*='price']")
        if store_el and price_el:
            store = store_el.get_text(strip=True)
            price = parse_price(price_el.get_text())
            if store and price is not None:
                details["prices"].append({"store": store, "price": price})

    # Also look for the main eShop price if price table wasn't found
    if not details["prices"]:
        price_els = soup.select(".price, [class*='current-price'], [class*='sale']")
        for pel in price_els:
            p = parse_price(pel.get_text())
            if p is not None:
                details["prices"].append({"store": "eShop", "price": p})
                break

    # MSRP
    msrp_el = soup.select_one("[class*='msrp'], [class*='regular-price']")
    if msrp_el:
        details["msrp"] = parse_price(msrp_el.get_text())

    # Release date
    for dt_el in soup.select("dt, th, .label, strong"):
        if "release" in dt_el.get_text(strip=True).lower():
            dd = dt_el.find_next_sibling("dd") or dt_el.find_next_sibling("td") or dt_el.find_next()
            if dd:
                details["release_date"] = dd.get_text(strip=True)
                break

    # Metacritic
    meta_el = soup.select_one("[class*='metacritic'], [class*='score']")
    if meta_el:
        score_match = re.search(r"\b(\d{1,3})\b", meta_el.get_text())
        if score_match:
            score = int(score_match.group(1))
            if 0 <= score <= 100:
                details["metacritic_score"] = score

    # Description
    desc_el = soup.select_one(".description, [class*='overview'], [class*='about']")
    if desc_el:
        details["description"] = desc_el.get_text(strip=True)[:500]

    return details


def resolve_uuid(slug: str) -> str:
    """Get UUID for a game slug, using cache or fetching the game page."""
    if slug in _uuid_cache:
        return _uuid_cache[slug]

    client = get_client(require_auth=True)
    try:
        resp = client.get(f"/items/{slug}")
        resp.raise_for_status()
        details = parse_game_details(resp.text, slug)
        if details["uuid"]:
            return details["uuid"]
        raise ValueError(f"Could not find UUID for '{slug}'. The game page may have changed structure.")
    finally:
        client.close()


# ─── MCP Server ─────────────────────────────────────────────────────────────

mcp = FastMCP("dekudeals", dependencies=["httpx", "beautifulsoup4"])


@mcp.tool()
def search_games(query: str, min_discount: int = 0) -> str:
    """
    Search for games on Deku Deals.

    Args:
        query: Search term (e.g., "mario kart", "sonic")
        min_discount: Optional minimum discount percentage to filter by (0-100)
    """
    client = get_client()
    try:
        resp = client.get("/search", params={"q": query})
        resp.raise_for_status()
        games = parse_search_results(resp.text)

        if min_discount > 0:
            games = [g for g in games if g.get("discount_pct") and g["discount_pct"] >= min_discount]

        if not games:
            return f"No games found for '{query}'" + (f" with {min_discount}%+ discount" if min_discount else "")

        lines = [f"Found {len(games)} result(s) for '{query}':"]
        for g in games[:20]:  # Cap at 20 results
            line = f"- {g['name']}"
            if g["current_price"] is not None:
                line += f" — ${g['current_price']:.2f}"
            if g["regular_price"] is not None:
                line += f" (was ${g['regular_price']:.2f})"
            if g["discount_pct"]:
                line += f" [{g['discount_pct']}% off]"
            line += f"\n  slug: {g['slug']}"
            lines.append(line)

        return "\n".join(lines)
    finally:
        client.close()


@mcp.tool()
def get_game_details(slug: str) -> str:
    """
    Get detailed info for a specific game including prices across stores.

    Args:
        slug: Game slug from search results (e.g., "mario-kart-8-deluxe")
    """
    client = get_client()
    try:
        resp = client.get(f"/items/{slug}")
        if resp.status_code == 404:
            return f"Game not found: '{slug}'. Try searching first with search_games."
        resp.raise_for_status()
        details = parse_game_details(resp.text, slug)

        lines = [f"# {details['name'] or slug}"]
        lines.append(f"URL: {details['url']}")

        if details["uuid"]:
            lines.append(f"UUID: {details['uuid']}")

        if details["msrp"] is not None:
            lines.append(f"MSRP: ${details['msrp']:.2f}")
        if details["release_date"]:
            lines.append(f"Release: {details['release_date']}")
        if details["metacritic_score"]:
            lines.append(f"Metacritic: {details['metacritic_score']}")

        if details["prices"]:
            lines.append("\nPrices:")
            for p in details["prices"]:
                lines.append(f"  {p['store']}: ${p['price']:.2f}")

        lines.append(f"\nOn wishlist: {'Yes' if details['on_wishlist'] else 'No'}")
        lines.append(f"In collection: {'Yes' if details['on_collection'] else 'No'}")

        if details["description"]:
            lines.append(f"\n{details['description']}")

        return "\n".join(lines)
    finally:
        client.close()


@mcp.tool()
def get_wishlist() -> str:
    """Get the user's current Deku Deals wishlist with prices."""
    client = get_client(require_auth=True)
    try:
        resp = client.get("/wishlist.json")

        if check_auth_redirect(resp):
            return ("Deku Deals session expired. Re-save credentials via the "
                    "MCP Auth Bridge extension.")

        resp.raise_for_status()

        try:
            data = resp.json()
        except json.JSONDecodeError:
            return "Could not parse wishlist response. Session may have expired."

        if not data:
            return "Your wishlist is empty."

        # Wishlist JSON format: {"list": [...], "default_desired_price": "drop"}
        items = data.get("list", data) if isinstance(data, dict) else data
        if not items:
            return "Your wishlist is empty."

        if isinstance(items, list):
            lines = [f"Wishlist ({len(items)} games):"]
            for item in items:
                name = item.get("name", "Unknown")
                link = item.get("link", "")
                desired = item.get("desired_price", "")
                added = item.get("added_at", "")

                # Extract slug from link
                slug = link.split("/items/")[-1].rstrip("/") if "/items/" in link else ""

                line = f"- {name}"
                if desired:
                    line += f" (alert: {desired})"
                if added:
                    line += f" — added {added[:10]}"
                if slug:
                    line += f"\n  slug: {slug}"
                lines.append(line)
            return "\n".join(lines)
        else:
            return json.dumps(data, indent=2)
    finally:
        client.close()


@mcp.tool()
def add_to_wishlist(slug: str, desired_price: str = "drop", specific_price: float = 0) -> str:
    """
    Add a game to the Deku Deals wishlist.

    Args:
        slug: Game slug (e.g., "mario-kart-8-deluxe")
        desired_price: Alert threshold — "drop" (any price drop), "lowest" (all-time low), or "specific" (set target price)
        specific_price: Target price in dollars (only used when desired_price is "specific")
    """
    uuid = resolve_uuid(slug)
    client = get_client(require_auth=True)
    try:
        form_data = {"to": "true", "desired_price": desired_price}
        if desired_price == "specific" and specific_price > 0:
            # Deku Deals expects price in cents
            form_data["specific_price"] = str(int(specific_price * 100))

        resp = client.post(
            f"/items/{uuid}/watch",
            data=form_data,
            headers={"Content-Type": "application/x-www-form-urlencoded"}
        )

        if check_auth_redirect(resp):
            return ("Deku Deals session expired. Re-save credentials via the "
                    "MCP Auth Bridge extension.")

        if resp.status_code in (200, 302, 303):
            return f"Added '{slug}' to wishlist (alert: {desired_price})"
        else:
            return f"Unexpected response ({resp.status_code}) when adding '{slug}' to wishlist."
    finally:
        client.close()


@mcp.tool()
def remove_from_wishlist(slug: str) -> str:
    """
    Remove a game from the Deku Deals wishlist.

    Args:
        slug: Game slug (e.g., "mario-kart-8-deluxe")
    """
    uuid = resolve_uuid(slug)
    client = get_client(require_auth=True)
    try:
        # First check if it's on the wishlist
        resp = client.get(f"/items/{slug}")
        resp.raise_for_status()
        details = parse_game_details(resp.text, slug)

        if not details["on_wishlist"]:
            return f"'{slug}' is not on your wishlist."

        # Remove: POST with to=false (the form uses to=true to add)
        resp = client.post(
            f"/items/{uuid}/watch",
            data={"to": "false"},
            headers={"Content-Type": "application/x-www-form-urlencoded"}
        )

        if check_auth_redirect(resp):
            return ("Deku Deals session expired. Re-save credentials via the "
                    "MCP Auth Bridge extension.")

        if resp.status_code in (200, 302, 303):
            return f"Removed '{slug}' from wishlist."
        else:
            return f"Unexpected response ({resp.status_code}) when removing '{slug}'."
    finally:
        client.close()


@mcp.tool()
def add_to_collection(slug: str) -> str:
    """
    Mark a game as owned in the Deku Deals collection.

    Args:
        slug: Game slug (e.g., "mario-kart-8-deluxe")
    """
    uuid = resolve_uuid(slug)
    client = get_client(require_auth=True)
    try:
        resp = client.post(
            f"/items/{uuid}/own",
            headers={"Content-Type": "application/x-www-form-urlencoded"}
        )

        if check_auth_redirect(resp):
            return ("Deku Deals session expired. Re-save credentials via the "
                    "MCP Auth Bridge extension.")

        if resp.status_code in (200, 302, 303):
            return f"Added '{slug}' to your collection."
        else:
            return f"Unexpected response ({resp.status_code})."
    finally:
        client.close()


@mcp.tool()
def get_current_sales(min_discount: int = 0, max_price: float = 0, platform: str = "switch") -> str:
    """
    Browse games currently on sale on the Nintendo eShop.

    Args:
        min_discount: Minimum discount percentage (0-100)
        max_price: Maximum price in dollars (0 for no limit)
        platform: Platform to browse ("switch" is default)
    """
    client = get_client()
    try:
        # Deku Deals has a sales page
        resp = client.get(f"/on-sale")
        resp.raise_for_status()
        games = parse_search_results(resp.text)

        # Apply filters
        if min_discount > 0:
            games = [g for g in games if g.get("discount_pct") and g["discount_pct"] >= min_discount]
        if max_price > 0:
            games = [g for g in games if g.get("current_price") is not None and g["current_price"] <= max_price]

        if not games:
            return "No games found matching your criteria."

        lines = [f"Currently on sale ({len(games)} games" +
                 (f", {min_discount}%+ off" if min_discount else "") +
                 (f", under ${max_price:.2f}" if max_price else "") + "):"]

        for g in games[:25]:
            line = f"- {g['name']}"
            if g["current_price"] is not None:
                line += f" — ${g['current_price']:.2f}"
            if g["regular_price"] is not None:
                line += f" (was ${g['regular_price']:.2f})"
            if g["discount_pct"]:
                line += f" [{g['discount_pct']}% off]"
            line += f"\n  slug: {g['slug']}"
            lines.append(line)

        return "\n".join(lines)
    finally:
        client.close()


# ─── Main ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    mcp.run()
