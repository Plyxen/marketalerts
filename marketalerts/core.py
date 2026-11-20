"""One check: read every source, send what is new, remember what was sent."""
from __future__ import annotations

import copy
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import notify, sources
from .config import Config, Watch

FAILURE_LIMIT = 12  # consecutive failed checks before a warning is sent
SEEN_LIMIT = 600
FILING_WINDOW_DAYS = 14


class State:
    """What has already been reported. A missing or damaged file starts fresh."""

    def __init__(self, path: Path):
        self.path = Path(path)
        try:
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.data = {}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.data, indent=1), encoding="utf-8")
        os.replace(temporary, self.path)


def read_filings(watch: Watch, config: Config, cache_dir: Path, fetcher, quote) -> list[sources.Alert]:
    agent = config.settings.user_agent
    cik = sources.sec_cik(watch, cache_dir, agent, fetcher)
    if cik is None:
        return []  # funds and indices do not file with the SEC
    forms = watch.forms or config.settings.forms
    return sources.parse_filings(fetcher(sources.sec_url(cik), agent), watch, forms, quote)


def upcoming_events(config: Config, cache_dir: Path, fetcher, now: datetime,
                    hours: float = 24.0) -> list[dict]:
    plan = config.calendar
    events = sources.calendar_events(
        cache_dir, config.settings.user_agent, plan.currencies, plan.impact, fetcher)
    return [e for e in events if now <= e["when"] < now + timedelta(hours=hours)]


def gather(watch: Watch, config: Config, cache_dir: Path, fetcher, now: datetime, attempt):
    """Current items per feed for one watched symbol. Failed feeds are left out."""
    settings, agent = config.settings, config.settings.user_agent
    # The quote comes first: every card shows the market and today's price.
    quote = attempt(
        f"price:{watch.symbol}",
        lambda: sources.parse_quote(fetcher(sources.quote_url(watch), agent)),
    )
    found = {}
    if watch.filings:
        def filings():
            oldest = now - timedelta(days=FILING_WINDOW_DAYS)
            alerts = read_filings(watch, config, cache_dir, fetcher, quote)
            return sorted((a for a in alerts if a.when >= oldest), key=lambda a: a.when)

        found[f"filing:{watch.symbol}"] = attempt(f"filing:{watch.symbol}", filings)
    if watch.news and settings.news != "off":
        found[f"news:{watch.symbol}"] = attempt(
            f"news:{watch.symbol}",
            lambda: sources.parse_news(
                fetcher(sources.news_url(watch), agent), watch, settings.news, now, quote=quote
            ),
        )
    return {feed: items for feed, items in found.items() if items is not None}, quote


def check_once(
    config: Config,
    state: State,
    *,
    cache_dir: Path,
    fetcher=sources.fetch,
    sender=notify.send,
    now: datetime | None = None,
    dry_run: bool = False,
    log=print,
) -> int:
    """Returns the number of cards produced. State is saved only after delivery."""
    now = now or datetime.now(timezone.utc)
    settings = config.settings
    data = copy.deepcopy(state.data)
    seen = data.setdefault("seen", {})
    prices = data.setdefault("price", {})
    failures = data.setdefault("failures", {})
    alerts: list[sources.Alert] = []
    notes: list[dict] = []

    def attempt(feed, work):
        try:
            result = work()
        except Exception as error:  # one broken source must not stop the others
            failures[feed] = failures.get(feed, 0) + 1
            log(f"{feed}: failed ({type(error).__name__}: {error})")
            if failures[feed] == FAILURE_LIMIT:
                notes.append(sources.note_card(
                    f"⚠️ Cannot read {sources.clean(feed)}: the last {FAILURE_LIMIT} checks "
                    f"failed ({type(error).__name__})", sources.AMBER, now))
            return None
        failures.pop(feed, None)
        return result

    for watch in config.watches:
        found, quote = gather(watch, config, cache_dir, fetcher, now, attempt)
        for feed, items in found.items():
            if feed not in seen:
                # First time this feed is read: remember what exists, send nothing.
                seen[feed] = [a.key for a in items][-SEEN_LIMIT:]
                continue
            known = set(seen[feed])
            fresh = [a for a in items if a.key not in known]
            shown = fresh
            if feed.startswith("news:") and len(fresh) > settings.news_max_per_check:
                shown = fresh[-settings.news_max_per_check :]
                notes.append(sources.note_card(
                    f"… and {len(fresh) - len(shown)} more headlines for {sources.clean(watch.name)}",
                    sources.GREY, now))
            alerts.extend(shown)
            seen[feed] = (seen[feed] + [a.key for a in fresh])[-SEEN_LIMIT:]
        if quote is not None and watch.price:
            drop = settings.drop_percent if watch.drop_percent is None else watch.drop_percent
            rise = settings.rise_percent if watch.rise_percent is None else watch.rise_percent
            alert, prices[watch.symbol] = sources.price_alert(
                watch, quote, prices.get(watch.symbol, {}), drop, rise, now
            )
            if alert:
                alerts.append(alert)

    today = now.date().isoformat()
    plan = config.calendar
    wait_until = data.get("calendar_retry", "")
    if (plan.enabled and data.get("calendar") != today and now.hour >= plan.hour_utc
            and now.isoformat() >= wait_until):
        events = attempt("calendar", lambda: upcoming_events(config, cache_dir, fetcher, now))
        if events is None:
            # Ask again in an hour, not every check: the feed is a free service.
            data["calendar_retry"] = (now + timedelta(hours=1)).isoformat()
        else:
            data["calendar"] = today
            data.pop("calendar_retry", None)
            if events:
                notes.append(sources.calendar_card(
                    events, now, "in the next 24 hours", plan.currencies, plan.impact))

    if settings.heartbeat == "daily" and data.get("heartbeat") != today and now.hour >= 7:
        data["heartbeat"] = today
        watching = ", ".join(sources.clean(w.symbol) for w in config.watches)
        notes.append(sources.note_card(f"✅ Running. Watching: {watching}", sources.GREEN, now))

    cards = [a.card for a in sorted(alerts, key=lambda a: a.when)] + notes
    if dry_run:
        for item in cards:
            log(sources.as_text(item) + "\n")
        return len(cards)
    if cards:
        sender(config.webhook_url, notify.pack(cards), username=config.username)
    state.data = data
    state.save()
    return len(cards)


def recent_items(config: Config, *, cache_dir: Path, days: float, fetcher=sources.fetch,
                 now: datetime | None = None, log=print, news_mode: str | None = None):
    """Per watched symbol: its quote and the filings and headlines of the last `days` days."""
    now = now or datetime.now(timezone.utc)
    settings, agent = config.settings, config.settings.user_agent
    mode = news_mode or settings.news
    oldest = now - timedelta(days=days)
    for watch in config.watches:
        quote, items = None, []
        try:
            quote = sources.parse_quote(fetcher(sources.quote_url(watch), agent))
        except Exception as error:
            log(f"{watch.symbol} price: failed ({type(error).__name__}: {error})")
        try:
            if watch.filings:
                items += read_filings(watch, config, cache_dir, fetcher, quote)
            if watch.news and mode != "off":
                payload = fetcher(sources.news_url(watch), agent)
                items += sources.parse_news(payload, watch, mode, now, days * 24, quote)
        except Exception as error:
            log(f"{watch.symbol}: failed ({type(error).__name__}: {error})")
        yield watch, quote, sorted((a for a in items if a.when >= oldest), key=lambda a: a.when)


def preview(config: Config, *, cache_dir: Path, days: float, log=print, **options) -> int:
    """Show what the last `days` days would have produced. Sends nothing, saves nothing."""
    count = 0
    for watch, quote, items in recent_items(config, cache_dir=cache_dir, days=days, log=log, **options):
        for alert in items:
            log(f"{alert.when:%Y-%m-%d %H:%M} UTC\n{alert.text}\n")
            count += 1
        if quote:
            log(f"{watch.symbol}: {quote.change:+.1%} on {quote.market_date} "
                f"({quote.price:,.2f} {quote.currency}, {quote.exchange})\n")
    plan = config.calendar
    if plan.enabled:
        now = options.get("now") or datetime.now(timezone.utc)
        fetcher = options.get("fetcher", sources.fetch)
        try:
            events = upcoming_events(config, cache_dir, fetcher, now)
            if events:
                log(sources.as_text(sources.calendar_card(
                    events, now, "in the next 24 hours", plan.currencies, plan.impact)) + "\n")
            else:
                log("Economic calendar: nothing matching in the next 24 hours.\n")
        except Exception as error:
            log(f"calendar: failed ({type(error).__name__}: {error})")
    return count


def sample_cards(config: Config, *, cache_dir: Path, now: datetime | None = None, **options) -> list[dict]:
    """One real example of each kind of card, marked as a sample."""
    now = now or datetime.now(timezone.utc)
    filing = news = biggest = None
    for watch, quote, items in recent_items(
        config, cache_dir=cache_dir, days=30, now=now, news_mode="all", **options
    ):
        for alert in items:
            if alert.feed.startswith("filing:") and (
                filing is None or alert.card["color"] == sources.RED
            ):
                filing = alert.card
            if alert.feed.startswith("news:"):
                news = news or alert.card
        if quote and (biggest is None or quote.change < biggest[1].change):
            biggest = (watch, quote)
    cards = []
    for item in (filing, news):
        if item:
            item = copy.deepcopy(item)
            item["author"]["name"] = "Sample · " + item["author"]["name"]
            cards.append(item)
    if biggest:
        cards.append(sources.price_card(*biggest, now, sample=True))
    plan = config.calendar
    if plan.enabled:
        events = sources.calendar_events(
            cache_dir, config.settings.user_agent, plan.currencies, plan.impact,
            options.get("fetcher", sources.fetch))
        if events:
            cards.append(sources.calendar_card(
                events, now, "this week", plan.currencies, plan.impact, sample=True))
    return cards
