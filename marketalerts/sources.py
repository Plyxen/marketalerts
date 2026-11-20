"""Where alerts come from: SEC filings, news headlines and prices.

Everything fetched here is untrusted text. It is cleaned before it is shown
and never interpreted as anything but data. Each alert is a Discord card
(an "embed"): a linked title plus labelled fields.
"""
from __future__ import annotations

import json
import re
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ElementTree
import zlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

from .config import Watch

MAX_BYTES = 8_000_000
TICKER_CACHE_DAYS = 30

RED, BLUE, GREY, GREEN, AMBER = 0xE5484D, 0x3B82F6, 0x8B949E, 0x30A46C, 0xF5A524

# 8-K item numbers in plain words. True marks the ones worth reading at once.
FILING_ITEMS = {
    "1.01": ("New major agreement", False),
    "1.02": ("Major agreement ended", False),
    "1.03": ("Bankruptcy or receivership", True),
    "1.05": ("Serious cybersecurity incident", True),
    "2.01": ("Acquisition or sale of assets completed", False),
    "2.02": ("Financial results published", False),
    "2.03": ("New debt or financial obligation", False),
    "2.04": ("Debt repayment demanded early", True),
    "2.05": ("Restructuring or closure costs", False),
    "2.06": ("Large write-down of assets", True),
    "3.01": ("Delisting notice or listing rule failure", True),
    "3.02": ("Shares sold privately", False),
    "3.03": ("Shareholder rights changed", False),
    "4.01": ("Auditor changed", True),
    "4.02": ("Earlier financial statements can no longer be relied on", True),
    "5.01": ("Change of control of the company", True),
    "5.02": ("Director or top executive leaving or joining", True),
    "5.03": ("Company rules amended", False),
    "5.07": ("Shareholder vote results", False),
    "7.01": ("Public disclosure", False),
    "8.01": ("Other event", False),
    "9.01": ("Financial statements and exhibits", False),
}

FORM_NOTES = {
    "8-K": "major event report",
    "6-K": "news report from a non-US company",
    "10-Q": "quarterly report",
    "10-K": "annual report",
    "20-F": "annual report from a non-US company",
    "4": "insider trade",
}

KINDS = {"EQUITY": "Stock", "ETF": "Fund", "MUTUALFUND": "Fund", "INDEX": "Index"}

IMPORTANT_NEWS = re.compile(
    r"\b(ceo|cfo|chief executive|resign\w*|steps? down|stepping down|oust\w*|fired|"
    r"dismiss\w*|departure|investigat\w*|probe|fraud|lawsuit|sued|sues|subpoena\w*|"
    r"indict\w*|arrest\w*|scandal|recall\w*|bankrupt\w*|hack\w*|breach\w*|guidance|"
    r"profit warning|downgrad\w*|delist\w*|trading halt\w*|acqui\w*|merger|takeover|"
    r"buyout|antitrust|ban|bans|banned|sanction\w*|restat\w*|layoffs?)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Alert:
    feed: str  # e.g. "filing:NVDA"
    key: str  # identifies this item within its feed
    when: datetime
    card: dict  # Discord embed

    @property
    def text(self) -> str:
        return as_text(self.card)


@dataclass(frozen=True)
class Quote:
    price: float
    previous_close: float
    market_date: str
    currency: str = ""
    exchange: str = ""
    kind: str = ""
    day_low: float | None = None
    day_high: float | None = None
    year_low: float | None = None
    year_high: float | None = None

    @property
    def change(self) -> float:
        return self.price / self.previous_close - 1.0


def fetch(url: str, agent: str, timeout: float = 20.0) -> bytes:
    if not url.startswith("https://"):
        raise ValueError("only https sources are allowed")
    request = urllib.request.Request(
        url, headers={"User-Agent": agent, "Accept-Encoding": "gzip"}
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read(MAX_BYTES + 1)
        if response.headers.get("Content-Encoding") == "gzip":
            body = zlib.decompressobj(wbits=31).decompress(body, MAX_BYTES + 1)
    if len(body) > MAX_BYTES:
        raise ValueError("response too large")
    return body


def clean(text: str, limit: int = 300) -> str:
    """One safe line of plain text: no markdown, no pings, bounded length."""
    text = " ".join(str(text).split())
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    text = re.sub(r"([\\*_~`|>\[\]()#])", r"\\\1", text)
    return text.replace("@", "@​")


# ---- cards -------------------------------------------------------------------

def card(*, label: str, title: str, colour: int, when: datetime, footer: str,
         url: str = "", description: str = "", fields: list[tuple[str, str]] | None = None) -> dict:
    result = {
        "author": {"name": label},
        "title": title[:256],
        "color": colour,
        "footer": {"text": footer},
        "timestamp": when.astimezone(timezone.utc).isoformat(),
    }
    if _is_link(url):
        result["url"] = url
    if description:
        result["description"] = description[:2000]
    shown = [(name, value) for name, value in (fields or []) if value]
    if shown:
        result["fields"] = [
            {"name": name, "value": value[:1024], "inline": name != "Links"}
            for name, value in shown
        ]
    return result


def note_card(text: str, colour: int, when: datetime) -> dict:
    return card(label="Market alerts", title=text, colour=colour, when=when, footer="marketalerts")


def as_text(embed: dict) -> str:
    """The card as plain text, for the console."""
    lines = [f"[{embed['author']['name']}] {embed['title']}"]
    if embed.get("url"):
        lines.append(f"  {embed['url']}")
    if embed.get("description"):
        lines += [f"  {line}" for line in embed["description"].splitlines()]
    for field in embed.get("fields", []):
        lines.append(f"  {field['name']}: {field['value']}")
    return re.sub(r"\\(.)", r"\1", "\n".join(lines))


def _is_link(url: str) -> bool:
    return bool(re.fullmatch(r"https?://[^\s<>()\[\]]+", str(url).strip()))


def _stamp(when: datetime) -> str:
    """Discord shows this in the reader's own time zone, plus how long ago."""
    unix = int(when.timestamp())
    return f"<t:{unix}:f> (<t:{unix}:R>)"


def _money(value: float, currency: str) -> str:
    return f"{value:,.2f} {clean(currency, 8)}".strip()


def chart_url(watch: Watch) -> str:
    return "https://finance.yahoo.com/quote/" + urllib.parse.quote(watch.symbol, safe="")


def base_fields(watch: Watch, quote: Quote | None) -> list[tuple[str, str]]:
    """What it is, where it trades and how it is doing today."""
    kind = KINDS.get(quote.kind if quote else "", "Stock")
    fields = [(kind, f"{clean(watch.name)} ({clean(watch.symbol, 20)})")]
    if quote:
        market = " · ".join(p for p in (clean(quote.exchange, 40), clean(quote.currency, 8)) if p)
        fields.append(("Market", market))
        fields.append(("Price today", f"{_money(quote.price, quote.currency)} ({quote.change:+.1%})"))
    return fields


# ---- SEC filings -------------------------------------------------------------

def sec_cik(watch: Watch, cache_dir: Path, agent: str, fetcher=fetch) -> int | None:
    """The SEC's company number for a ticker, or None for funds and indices."""
    if watch.cik:
        return watch.cik
    cache = cache_dir / "sec_tickers.json"
    fresh = cache.exists() and time.time() - cache.stat().st_mtime < TICKER_CACHE_DAYS * 86400
    if not fresh:
        payload = fetcher("https://www.sec.gov/files/company_tickers.json", agent)
        json.loads(payload)  # refuse to cache something that is not JSON
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache.write_bytes(payload)
    rows = json.loads(cache.read_bytes())
    wanted = (watch.sec_ticker or watch.symbol).upper()
    for row in rows.values():
        if str(row.get("ticker", "")).upper() == wanted:
            return int(row["cik_str"])
    return None


def sec_url(cik: int) -> str:
    return f"https://data.sec.gov/submissions/CIK{cik:010d}.json"


def parse_filings(payload: bytes, watch: Watch, forms: tuple[str, ...],
                  quote: Quote | None = None) -> list[Alert]:
    data = json.loads(payload)
    cik = int(data["cik"])
    recent = data["filings"]["recent"]
    alerts = []
    for i, form in enumerate(recent["form"]):
        if form not in forms:
            continue
        accession = str(recent["accessionNumber"][i])
        if not re.fullmatch(r"[0-9-]+", accession):
            continue
        when = datetime.fromisoformat(str(recent["acceptanceDateTime"][i]).replace("Z", "+00:00"))
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        codes = [c.strip() for c in str(recent["items"][i]).split(",") if c.strip()]
        known = [(code, *FILING_ITEMS.get(code, ("Item " + clean(code, 12), False))) for code in codes]
        urgent = [label for _, label, important in known if important]
        bullets = "\n".join(
            f"• {'**' + label + '**' if important else label} (item {clean(code, 12)})"
            for code, label, important in known
        )
        note = FORM_NOTES.get(form, "")
        if urgent:
            headline = " and ".join(urgent[:2])
        elif known:
            headline = known[0][1]
        else:
            headline = f"new form {clean(form, 12)}" + (f" ({note})" if note else "")
            bullets = "This form has no summary lines. Open the document to see what it covers."

        folder = f"https://www.sec.gov/Archives/edgar/data/{cik}/{accession.replace('-', '')}/"
        document = str(recent["primaryDocument"][i])
        link = folder + document if re.fullmatch(r"[\w.\-/]+", document) else folder
        history = f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}&type={urllib.parse.quote(form)}"
        fields = base_fields(watch, quote) + [
            ("Form", f"{clean(form, 12)}" + (f" · {note}" if note else "")),
            ("Filed", _stamp(when)),
            ("Links", f"[Filing document]({link}) · [Earlier filings]({history}) · [Chart]({chart_url(watch)})"),
        ]
        embed = card(
            label="🔴 Important company filing" if urgent else "📄 Company filing",
            title=f"{clean(watch.name)}: {headline}",
            url=link,
            colour=RED if urgent else GREY,
            when=when,
            description=bullets,
            fields=fields,
            footer="SEC EDGAR · official filing",
        )
        alerts.append(Alert(f"filing:{watch.symbol}", accession, when, embed))
    return alerts


# ---- news --------------------------------------------------------------------

def news_url(watch: Watch) -> str:
    symbol = urllib.parse.quote(watch.news_symbol or watch.symbol, safe="")
    return f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={symbol}&region=US&lang=en-US"


def parse_rss(payload: bytes) -> list[dict]:
    head = payload[:4096].lower()
    if b"<!doctype" in head or b"<!entity" in payload.lower():
        raise ValueError("feed contains a document type declaration; refused")
    root = ElementTree.fromstring(payload)
    items = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        published = item.findtext("pubDate") or ""
        if not title or not published:
            continue
        try:
            when = parsedate_to_datetime(published)
        except (TypeError, ValueError):
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        link = (item.findtext("link") or "").strip()
        key = (item.findtext("guid") or link or title).strip()
        summary = (item.findtext("description") or "").strip()
        items.append({"title": title, "link": link, "key": key, "when": when, "summary": summary})
    return items


def _mentions(watch: Watch, text: str) -> bool:
    """A company's feed also carries stories about its rivals; keep the ones naming it."""
    terms = {watch.name.split("(")[0].strip().lower()}
    for symbol in (watch.symbol, watch.news_symbol, watch.sec_ticker):
        if symbol:
            terms.add(symbol.split(".")[0].lstrip("^").lower())
    text = text.lower()
    return any(re.search(rf"\b{re.escape(term)}\b", text) for term in terms if len(term) >= 2)


def parse_news(payload: bytes, watch: Watch, mode: str, now: datetime,
               max_age_hours: float = 24.0, quote: Quote | None = None) -> list[Alert]:
    oldest = now - timedelta(hours=max_age_hours)
    alerts = []
    for item in parse_rss(payload):
        if item["when"] < oldest:
            continue
        if mode == "important" and not (
            IMPORTANT_NEWS.search(item["title"])
            and _mentions(watch, item["title"] + " " + item["summary"])
        ):
            continue
        link = item["link"] if _is_link(item["link"]) else ""
        source = urllib.parse.urlsplit(link).hostname or "" if link else ""
        links = f"[Chart]({chart_url(watch)})"
        if link:
            links = f"[Article]({link}) · " + links
        fields = base_fields(watch, quote) + [
            ("Source", clean(source.removeprefix("www."), 60)),
            ("Published", _stamp(item["when"])),
            ("Links", links),
        ]
        embed = card(
            label="📰 News",
            title=clean(item["title"], 240),
            url=link,
            colour=BLUE,
            when=item["when"],
            description=clean(re.sub(r"<[^>]+>", " ", item["summary"]), 280),
            fields=fields,
            footer="Yahoo Finance news feed",
        )
        alerts.append(Alert(f"news:{watch.symbol}", item["key"][:200], item["when"], embed))
    return sorted(alerts, key=lambda a: a.when)


# ---- economic calendar -------------------------------------------------------

CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
CALENDAR_CACHE_HOURS = 6
CALENDAR_LINES = 15
IMPACT_MARKS = {"High": "🔴", "Medium": "🟠", "Low": "🟡"}


def calendar_events(cache_dir: Path, agent: str, currencies, impacts, fetcher=fetch) -> list[dict]:
    """This week's events from Forex Factory. A local copy keeps requests to a few a day."""
    cache = cache_dir / "ff_calendar.json"
    fresh = cache.exists() and time.time() - cache.stat().st_mtime < CALENDAR_CACHE_HOURS * 3600
    if not fresh:
        payload = fetcher(CALENDAR_URL, agent)
        if not isinstance(json.loads(payload), list):
            raise ValueError("calendar feed is not a list of events")
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache.write_bytes(payload)
    return parse_calendar(cache.read_bytes(), currencies, impacts)


def parse_calendar(payload: bytes, currencies, impacts) -> list[dict]:
    events = []
    for row in json.loads(payload):
        try:
            when = datetime.fromisoformat(str(row["date"]))
            currency, impact, title = str(row["country"]), str(row["impact"]), str(row["title"])
        except (KeyError, TypeError, ValueError):
            continue
        if when.tzinfo is None or currency not in currencies or impact not in impacts:
            continue
        events.append({
            "when": when, "currency": currency, "impact": impact, "title": title,
            "forecast": str(row.get("forecast") or ""), "previous": str(row.get("previous") or ""),
        })
    return sorted(events, key=lambda e: e["when"])


def calendar_card(events: list[dict], now: datetime, period: str, currencies, impacts,
                  sample: bool = False) -> dict:
    """`period` finishes the title, e.g. "in the next 24 hours"."""
    soon = period.endswith("hours")
    lines = []
    for event in events[:CALENDAR_LINES]:
        numbers = ", ".join(
            f"{name} {clean(event[name], 20)}" for name in ("forecast", "previous") if event[name]
        )
        unix = int(event["when"].timestamp())
        moment = f"<t:{unix}:t> (<t:{unix}:R>)" if soon else f"<t:{unix}:f>"
        lines.append(
            f"{IMPACT_MARKS.get(event['impact'], '•')} {moment}"
            f" · **{clean(event['currency'], 6)}** · {clean(event['title'], 80)}"
            + (f" · {numbers}" if numbers else "")
        )
    if len(events) > CALENDAR_LINES:
        lines.append(f"… and {len(events) - CALENDAR_LINES} more")
    level = " and ".join(i.lower() for i in impacts)
    count = len(events)
    return card(
        label=("Sample · " if sample else "") + "🗓️ Economic calendar",
        title=f"{count} {level}-impact event{'s' if count != 1 else ''} {period}",
        url="https://www.forexfactory.com/calendar",
        colour=AMBER,
        when=now,
        description="\n".join(lines),
        fields=[
            ("Covers", ", ".join(clean(c, 6) for c in currencies)),
            ("Why it matters", "Rate decisions, inflation and jobs data often move index funds and gold."),
            ("Links", "[Full calendar](https://www.forexfactory.com/calendar)"),
        ],
        footer="Forex Factory calendar · times are shown in your time zone",
    )


# ---- prices ------------------------------------------------------------------

def quote_url(watch: Watch) -> str:
    symbol = urllib.parse.quote(watch.symbol, safe="")
    return f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range=1d&interval=1d"


def parse_quote(payload: bytes) -> Quote:
    meta = json.loads(payload)["chart"]["result"][0]["meta"]
    price = float(meta["regularMarketPrice"])
    previous = float(meta.get("chartPreviousClose") or meta.get("previousClose") or 0.0)
    if price <= 0 or previous <= 0:
        raise ValueError("quote has no usable price")
    moment = datetime.fromtimestamp(int(meta["regularMarketTime"]), timezone.utc)
    local = moment + timedelta(seconds=int(meta.get("gmtoffset") or 0))

    def number(name):
        value = meta.get(name)
        return float(value) if isinstance(value, (int, float)) and value > 0 else None

    return Quote(
        price,
        previous,
        local.date().isoformat(),
        str(meta.get("currency") or ""),
        str(meta.get("fullExchangeName") or meta.get("exchangeName") or ""),
        str(meta.get("instrumentType") or ""),
        number("regularMarketDayLow"),
        number("regularMarketDayHigh"),
        number("fiftyTwoWeekLow"),
        number("fiftyTwoWeekHigh"),
    )


def price_card(watch: Watch, quote: Quote, now: datetime, sample: bool = False) -> dict:
    change = quote.change * 100.0
    down = change < 0
    label = "📉 Price drop" if down else "📈 Price rise"
    fields = base_fields(watch, quote)[:2] + [
        ("Now", _money(quote.price, quote.currency)),
        ("Previous close", _money(quote.previous_close, quote.currency)),
        ("Change today", f"{change:+.1f}%"),
    ]
    if quote.day_low and quote.day_high:
        fields.append(("Day range", f"{quote.day_low:,.2f} to {quote.day_high:,.2f}"))
    if quote.year_low and quote.year_high:
        fields.append(("52-week range", f"{quote.year_low:,.2f} to {quote.year_high:,.2f}"))
    fields.append(("Links", f"[Chart and details]({chart_url(watch)})"))
    return card(
        label=("Sample · " if sample else "") + label,
        title=f"{clean(watch.name)} is {'down' if down else 'up'} {abs(change):.1f}% today",
        url=chart_url(watch),
        colour=RED if down else GREEN,
        when=now,
        fields=fields,
        footer="Yahoo Finance prices · can lag a few minutes",
    )


def price_alert(
    watch: Watch, quote: Quote, memory: dict, drop_percent: float, rise_percent: float, now: datetime
) -> tuple[Alert | None, dict]:
    """Alert when the day's move crosses a new multiple of the threshold.

    `memory` remembers the furthest level already reported for that market day,
    so a price that wobbles around the threshold is reported once.
    """
    if memory.get("date") != quote.market_date:
        memory = {"date": quote.market_date, "down": 0, "up": 0}
    change = quote.change * 100.0
    for side, threshold, moved in (("down", drop_percent, -change), ("up", rise_percent, change)):
        if threshold <= 0 or moved < threshold:
            continue
        level = int(moved // threshold)
        if level <= memory.get(side, 0):
            continue
        memory = {**memory, side: level}
        key = f"{quote.market_date}:{side}:{level}"
        return Alert(f"price:{watch.symbol}", key, now, price_card(watch, quote, now)), memory
    return None, memory
