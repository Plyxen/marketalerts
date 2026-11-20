"""Settings from config.toml."""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = ROOT / "config.toml"
WEBHOOK_ENV = "ALERTS_DISCORD_WEBHOOK"
NEWS_MODES = ("important", "all", "off")


@dataclass(frozen=True)
class Watch:
    symbol: str  # Yahoo Finance symbol; prices come from this listing
    name: str
    filings: bool = True
    news: bool = True
    price: bool = True
    drop_percent: float | None = None
    rise_percent: float | None = None
    sec_ticker: str | None = None  # US ticker for SEC filings, when the symbol is a non-US listing
    news_symbol: str | None = None  # symbol for headlines, when different
    forms: tuple[str, ...] | None = None  # filing types for this company only
    cik: int | None = None


@dataclass(frozen=True)
class Settings:
    check_minutes: float = 5.0
    drop_percent: float = 5.0
    rise_percent: float = 0.0
    news: str = "important"
    news_max_per_check: int = 5
    forms: tuple[str, ...] = ("8-K",)
    heartbeat: str = "off"
    user_agent: str = "marketalerts/0.1 (personal alert script)"


@dataclass(frozen=True)
class Calendar:
    """Daily card of scheduled economic events (rate decisions, inflation, jobs)."""

    enabled: bool = False
    hour_utc: int = 6  # post at or after this hour, once a day
    currencies: tuple[str, ...] = ("USD", "EUR", "CNY")
    impact: tuple[str, ...] = ("High",)


@dataclass(frozen=True)
class Config:
    webhook_url: str
    settings: Settings
    watches: tuple[Watch, ...]
    username: str = "Market Alerts"
    calendar: Calendar = Calendar()


def load_config(path: Path = DEFAULT_CONFIG) -> Config:
    raw = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    values = dict(raw.get("settings", {}))
    if "forms" in values:
        values["forms"] = tuple(values["forms"])
    settings = Settings(**values)
    if settings.news not in NEWS_MODES:
        raise ValueError(f"settings.news must be one of {NEWS_MODES}")
    if settings.heartbeat not in ("off", "daily"):
        raise ValueError("settings.heartbeat must be 'off' or 'daily'")
    if settings.check_minutes < 1:
        raise ValueError("settings.check_minutes must be at least 1")

    watches = []
    for entry in raw.get("watch", []):
        entry = dict(entry)
        if "forms" in entry:
            entry["forms"] = tuple(entry["forms"])
        watches.append(Watch(**entry))
    if not watches:
        raise ValueError("add at least one [[watch]] entry to the config")
    symbols = [w.symbol for w in watches]
    if len(set(symbols)) != len(symbols):
        raise ValueError("each [[watch]] symbol may appear only once")

    events = dict(raw.get("calendar", {}))
    for name in ("currencies", "impact"):
        if name in events:
            events[name] = tuple(str(v) for v in events[name])
    calendar = Calendar(**events)
    if not 0 <= calendar.hour_utc <= 23:
        raise ValueError("calendar.hour_utc must be between 0 and 23")

    discord = raw.get("discord", {})
    webhook = os.environ.get(WEBHOOK_ENV) or discord.get("webhook_url", "")
    return Config(webhook.strip(), settings, tuple(watches),
                  str(discord.get("username", "Market Alerts")), calendar)
