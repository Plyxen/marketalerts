"""Delivery to a Discord webhook, as cards (embeds)."""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

ALLOWED_PREFIXES = (
    "https://discord.com/api/webhooks/",
    "https://discordapp.com/api/webhooks/",
)
CARDS_PER_MESSAGE = 5  # Discord allows 10
CHARACTERS_PER_MESSAGE = 5000  # Discord allows 6000 across all cards
AGENT = "marketalerts/0.1"


class DeliveryError(RuntimeError):
    """Raised without the webhook address in the text, so logs stay safe to share."""


def card_size(card: dict) -> int:
    size = len(card.get("title", "")) + len(card.get("description", ""))
    size += len(card.get("author", {}).get("name", "")) + len(card.get("footer", {}).get("text", ""))
    return size + sum(len(f["name"]) + len(f["value"]) for f in card.get("fields", []))


def pack(cards: list[dict]) -> list[list[dict]]:
    """Group cards into as few messages as Discord's limits allow."""
    messages, current, used = [], [], 0
    for item in cards:
        size = card_size(item)
        if current and (len(current) >= CARDS_PER_MESSAGE or used + size > CHARACTERS_PER_MESSAGE):
            messages.append(current)
            current, used = [], 0
        current.append(item)
        used += size
    if current:
        messages.append(current)
    return messages


def send(webhook_url: str, messages: list[list[dict]], *, username: str = "Market Alerts",
         check_host: bool = True, sleep=time.sleep) -> None:
    if not webhook_url:
        raise DeliveryError("no Discord webhook is configured")
    if check_host and not webhook_url.startswith(ALLOWED_PREFIXES):
        raise DeliveryError("the configured webhook is not a Discord webhook address")
    for cards in messages:
        # allowed_mentions: a headline containing @everyone must never ping anyone.
        body = json.dumps(
            {"username": username[:80], "embeds": cards, "allowed_mentions": {"parse": []}}
        ).encode("utf-8")
        for attempt in range(4):
            request = urllib.request.Request(
                webhook_url,
                data=body,
                method="POST",
                headers={"Content-Type": "application/json", "User-Agent": AGENT},
            )
            try:
                with urllib.request.urlopen(request, timeout=20) as response:
                    response.read()
                break
            except urllib.error.HTTPError as error:
                if error.code == 429 and attempt < 3:
                    sleep(min(max(_retry_after(error), 0.5), 30.0))
                    continue
                raise DeliveryError(f"Discord refused the message (HTTP {error.code})") from None
            except (urllib.error.URLError, TimeoutError, OSError) as error:
                raise DeliveryError(f"could not reach Discord ({type(error).__name__})") from None


def _retry_after(error: urllib.error.HTTPError) -> float:
    try:
        return float(json.loads(error.read()).get("retry_after", 1.0))
    except (ValueError, AttributeError, TypeError):
        try:
            return float(error.headers.get("Retry-After", 1.0))
        except (ValueError, TypeError):
            return 1.0
