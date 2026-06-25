"""
Historical baseball image lookup.

Primary source: Library of Congress Photographs API — free, no auth required,
deep pre-1970 archive of American baseball.
Secondary source: Wikimedia Commons — broader / more modern player coverage.

Usage:
    from mlb.images import search_player_images, random_historic_image

    imgs = search_player_images("Babe Ruth")
    img  = random_historic_image()
    # Each result: {url, title, date, description, source, source_url}
"""

import re
import random
import logging

import requests

logger = logging.getLogger(__name__)

LOC_SEARCH  = "https://www.loc.gov/photos/"
COMMONS_API = "https://commons.wikimedia.org/w/api.php"
TIMEOUT     = 15

# Curated search terms that reliably return excellent historic LOC baseball images.
# Used by random_historic_image() to pick a query at random.
_RANDOM_QUERIES = [
    "babe ruth baseball",
    "lou gehrig baseball",
    "baseball 1920s player",
    "baseball 1930s player",
    "baseball 1940s portrait",
    "baseball 1950s stadium",
    "world series 1940s",
    "world series 1950s",
    "negro leagues baseball",
    "baseball spring training 1950s",
    "baseball player portrait vintage",
    "yankee stadium 1920s",
    "polo grounds baseball",
    "baseball pitcher vintage",
    "baseball catcher vintage",
]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def search_player_images(player_name: str, limit: int = 5) -> list[dict]:
    """Return up to `limit` image dicts for a named baseball player.

    Tries Library of Congress first (best for pre-1970 players), falls back
    to Wikimedia Commons for more modern players (e.g. Pete Alonso, Tom Seaver).
    """
    results = _search_loc(player_name, limit)
    if not results:
        results = _search_wikimedia(player_name, limit)
    return results[:limit]


def random_historic_image() -> dict | None:
    """Return one random historic baseball image from the LOC collection."""
    query = random.choice(_RANDOM_QUERIES)
    try:
        results = _search_loc(query, limit=25)
        if results:
            return random.choice(results)
    except Exception as e:
        logger.warning("random_historic_image failed (query=%r): %s", query, e)
    return None


# ---------------------------------------------------------------------------
# Library of Congress
# ---------------------------------------------------------------------------

def _search_loc(query: str, limit: int = 5) -> list[dict]:
    """Search the LOC Photographs collection."""
    try:
        r = requests.get(
            LOC_SEARCH,
            params={
                "q":  f"baseball {query}" if "baseball" not in query.lower() else query,
                "fo": "json",
                "c":  min(limit * 3, 50),   # fetch extra since some lack usable image URLs
            },
            timeout=TIMEOUT,
        )
        r.raise_for_status()
    except Exception as e:
        logger.warning("LOC search '%s' failed: %s", query, e)
        return []

    results = []
    for item in r.json().get("results", []):
        img_url = _best_loc_image_url(item)
        if not img_url:
            continue

        desc_list = item.get("description") or []
        desc      = desc_list[0] if desc_list else ""
        # Strip any HTML tags from description
        desc = re.sub(r"<[^>]+>", "", desc)[:250]

        results.append({
            "url":         img_url,
            "title":       (item.get("title") or query).strip().rstrip("."),
            "date":        item.get("date", ""),
            "description": desc,
            "source":      "Library of Congress",
            "source_url":  item.get("url", "https://www.loc.gov/photos/"),
        })

        if len(results) >= limit:
            break

    return results


def _best_loc_image_url(item: dict) -> str | None:
    """Extract the best direct image URL from a LOC result item."""
    # `image_url` is a list; entries look like direct JPEG/GIF/PNG links
    # (e.g. https://tile.loc.gov/storage-services/service/pnp/ppmsca/00012/00012v.jpg)
    # or resource-page links (e.g. https://www.loc.gov/resource/ppmsca.00012/).
    for url in reversed(item.get("image_url") or []):
        if url and re.search(r"\.(jpg|jpeg|png|gif)(\?|$)", url, re.I):
            return url

    # Fallback: construct a thumbnail URL from the LOC resource ID
    item_id = item.get("id") or ""          # e.g. "/item/2014715275/"
    rid     = item.get("resources")         # list of resource objects
    if rid and isinstance(rid, list):
        for res in rid:
            url = res.get("url") or res.get("image") or ""
            if url and re.search(r"\.(jpg|jpeg|png)(\?|$)", url, re.I):
                return url
            # Some resources have a `url` that ends in '/' -- try appending format
            if url and url.endswith("/"):
                return f"{url}full/pct:25/0/default.jpg"

    # Cannot reliably construct a valid tile URL from only the item ID —
    # the IIIF image server (tile.loc.gov) requires the resource identifier,
    # not the item identifier. Returning None here is safer than returning a
    # URL that will 404 and render as a broken image in markdown.
    return None


# ---------------------------------------------------------------------------
# Wikimedia Commons  (fallback for modern players)
# ---------------------------------------------------------------------------

def _search_wikimedia(query: str, limit: int = 5) -> list[dict]:
    """Search Wikimedia Commons for baseball images."""
    # Step 1: full-text search in File namespace
    try:
        r = requests.get(
            COMMONS_API,
            params={
                "action":      "query",
                "list":        "search",
                "srsearch":    f"baseball {query}" if "baseball" not in query.lower() else query,
                "srnamespace": "6",           # File namespace only
                "srlimit":     limit * 2,
                "format":      "json",
            },
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        hits = r.json().get("query", {}).get("search", [])
    except Exception as e:
        logger.warning("Wikimedia search '%s' failed: %s", query, e)
        return []

    if not hits:
        return []

    # Step 2: resolve image URLs and metadata
    titles = "|".join(h["title"] for h in hits[:limit])
    try:
        r2 = requests.get(
            COMMONS_API,
            params={
                "action":  "query",
                "titles":  titles,
                "prop":    "imageinfo",
                "iiprop":  "url|extmetadata",
                "format":  "json",
            },
            timeout=TIMEOUT,
        )
        r2.raise_for_status()
        pages = r2.json().get("query", {}).get("pages", {})
    except Exception as e:
        logger.warning("Wikimedia imageinfo failed: %s", e)
        return []

    results = []
    for page in pages.values():
        info_list = page.get("imageinfo") or []
        if not info_list:
            continue
        info = info_list[0]
        url  = info.get("url", "")
        # Only direct image files; skip SVG/OGG/PDF
        if not url or not re.search(r"\.(jpg|jpeg|png)$", url, re.I):
            continue

        meta  = info.get("extmetadata", {})
        title = re.sub(r"^File:", "", page.get("title", "")).strip()
        desc  = re.sub(
            r"<[^>]+>", "",
            meta.get("ImageDescription", {}).get("value", "")
        )[:250]

        results.append({
            "url":         url,
            "title":       title,
            "date":        meta.get("DateTimeOriginal", {}).get("value", ""),
            "description": desc,
            "source":      "Wikimedia Commons",
            "source_url":  f"https://commons.wikimedia.org/wiki/{page.get('title','')}",
        })

    return results
