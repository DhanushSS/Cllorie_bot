import logging

import requests

import config

logger = logging.getLogger("calorie-bot.nutrition_api")


def lookup(query: str):
    """Query the API Ninjas Nutrition API for real nutrition data.

    Returns (calories, protein_g) as floats if it recognized at least one item with
    non-zero calories, or None if it found nothing / isn't configured / the call failed.
    This is deterministic - the same query returns the same numbers every time, unlike
    an LLM estimate.
    """
    if not config.NUTRITION_API_KEY:
        return None
    try:
        resp = requests.get(
            config.NUTRITION_API_URL,
            params={"query": query},
            headers={"X-Api-Key": config.NUTRITION_API_KEY},
            timeout=8,
        )
        resp.raise_for_status()
        items = resp.json()
    except Exception as e:
        logger.warning("Nutrition API lookup failed for %r: %s", query, e)
        return None

    if not items:
        return None

    total_cal = sum(i.get("calories", 0) or 0 for i in items)
    total_protein = sum(i.get("protein_g", 0) or 0 for i in items)
    if total_cal <= 0:
        return None
    return round(total_cal, 1), round(total_protein, 1)