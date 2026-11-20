"""Run with: python -m unittest discover -s tests -t ."""
from __future__ import annotations

import json
import shutil
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from marketalerts import notify, sources
from marketalerts.config import ROOT, Calendar, Config, Settings, Watch, load_config
from marketalerts.core import FAILURE_LIMIT, State, check_once

NOW = datetime(2026, 11, 12, 14, 0, tzinfo=timezone.utc)
NVDA = Watch("NVDA", "Nvidia", cik=1045810)


def filings_payload(*rows):
    """rows: (form, accession, accepted, items)"""
    return json.dumps({
        "cik": "1045810",
        "filings": {"recent": {
            "form": [r[0] for r in rows],
            "accessionNumber": [r[1] for r in rows],
            "acceptanceDateTime": [r[2] for r in rows],
            "items": [r[3] for r in rows],
            "primaryDocument": ["doc.htm" for _ in rows],
            "primaryDocDescription": [r[0] for r in rows],
        }},
    }).encode()


def rss_payload(*items):
    """items: (title, guid, published)"""
    body = "".join(
        f"<item><title>{t}</title><guid>{g}</guid><link>https://www.example.com/{g}</link>"
        f"<description>Summary of {g}</description><pubDate>{p}</pubDate></item>"
        for t, g, p in items
    )
    return f"<?xml version='1.0'?><rss><channel>{body}</channel></rss>".encode()


def quote_payload(price, previous, moment=NOW):
    return json.dumps({"chart": {"result": [{"meta": {
        "regularMarketPrice": price, "chartPreviousClose": previous,
        "regularMarketTime": int(moment.timestamp()), "gmtoffset": -18000, "currency": "USD",
        "fullExchangeName": "NasdaqGS", "instrumentType": "EQUITY",
        "regularMarketDayLow": min(price, previous) - 1, "regularMarketDayHigh": previous + 1,
        "fiftyTwoWeekLow": 80.0, "fiftyTwoWeekHigh": 240.0,
    }}]}}).encode()


def fields(card):
    return {f["name"]: f["value"] for f in card.get("fields", [])}


RESIGNATION = ("8-K", "0001045810-26-000082", "2026-11-12T13:52:41.000Z", "5.02,9.01")
EARNINGS = ("8-K", "0001045810-26-000070", "2026-11-05T21:20:00.000Z", "2.02,9.01")
INSIDER = ("4", "0001197647-26-000013", "2026-11-06T21:12:02.000Z", "")
FOREIGN = ("6-K", "0000937966-26-000040", "2026-11-10T10:00:00.000Z", "")
QUOTE = sources.parse_quote(quote_payload(145.98, 150.96))


class FilingTests(unittest.TestCase):
    def test_executive_departure_is_a_red_card_with_links_and_market(self):
        alerts = sources.parse_filings(filings_payload(RESIGNATION, INSIDER), NVDA, ("8-K",), QUOTE)
        self.assertEqual(len(alerts), 1)
        card = alerts[0].card
        link = "https://www.sec.gov/Archives/edgar/data/1045810/000104581026000082/doc.htm"
        self.assertEqual(card["color"], sources.RED)
        self.assertEqual(card["title"], "Nvidia: Director or top executive leaving or joining")
        self.assertEqual(card["url"], link)
        self.assertIn("item 5.02", card["description"])
        shown = fields(card)
        self.assertEqual(shown["Stock"], "Nvidia (NVDA)")
        self.assertEqual(shown["Market"], "NasdaqGS · USD")
        self.assertEqual(shown["Price today"], "145.98 USD (-3.3%)")
        self.assertEqual(shown["Form"], "8-K · major event report")
        self.assertIn(f"[Filing document]({link})", shown["Links"])
        self.assertEqual(alerts[0].key, "0001045810-26-000082")

    def test_routine_filing_is_a_grey_card(self):
        card = sources.parse_filings(filings_payload(EARNINGS), NVDA, ("8-K",))[0].card
        self.assertEqual(card["color"], sources.GREY)
        self.assertEqual(card["title"], "Nvidia: Financial results published")
        self.assertNotIn("Market", fields(card))  # no quote, so no market shown

    def test_form_without_item_codes_still_gets_a_clear_title(self):
        asml = Watch("ASML.AS", "ASML", sec_ticker="ASML", forms=("6-K",), cik=937966)
        card = sources.parse_filings(filings_payload(FOREIGN), asml, asml.forms)[0].card
        self.assertEqual(card["title"], "ASML: new form 6-K (news report from a non-US company)")

    def test_odd_document_name_falls_back_to_the_folder(self):
        payload = json.loads(filings_payload(RESIGNATION))
        payload["filings"]["recent"]["primaryDocument"] = ["x> @everyone"]
        card = sources.parse_filings(json.dumps(payload).encode(), NVDA, ("8-K",))[0].card
        self.assertTrue(card["url"].endswith("/000104581026000082/"))
        self.assertNotIn("@everyone", json.dumps(card))


class NewsTests(unittest.TestCase):
    def setUp(self):
        fresh = "Thu, 12 Nov 2026 13:00:00 +0000"
        self.payload = rss_payload(
            ("Nvidia CEO resigns with immediate effect", "a", fresh),
            ("Five chip stocks to watch this week", "b", fresh),
            ("Nvidia faces antitrust probe", "c", "Mon, 02 Nov 2026 13:00:00 +0000"),
        )

    def test_important_mode_keeps_only_recent_big_news(self):
        alerts = sources.parse_news(self.payload, NVDA, "important", NOW)
        self.assertEqual([a.key for a in alerts], ["a"])

    def test_important_mode_skips_big_news_about_other_companies(self):
        payload = rss_payload(("Intel CEO resigns", "x", "Thu, 12 Nov 2026 13:00:00 +0000"))
        self.assertEqual(sources.parse_news(payload, NVDA, "important", NOW), [])
        self.assertEqual(len(sources.parse_news(payload, NVDA, "all", NOW)), 1)

    def test_all_mode_keeps_every_recent_headline(self):
        alerts = sources.parse_news(self.payload, NVDA, "all", NOW)
        self.assertEqual({a.key for a in alerts}, {"a", "b"})

    def test_news_card_names_the_source_and_links_the_article(self):
        card = sources.parse_news(self.payload, NVDA, "important", NOW, quote=QUOTE)[0].card
        self.assertEqual(card["title"], "Nvidia CEO resigns with immediate effect")
        self.assertEqual(card["url"], "https://www.example.com/a")
        self.assertEqual(card["description"], "Summary of a")
        shown = fields(card)
        self.assertEqual(shown["Source"], "example.com")
        self.assertEqual(shown["Market"], "NasdaqGS · USD")
        self.assertIn("<t:", shown["Published"])

    def test_feed_with_a_document_type_is_refused(self):
        bomb = b"<?xml version='1.0'?><!DOCTYPE x [<!ENTITY a 'aaaa'>]><rss></rss>"
        with self.assertRaises(ValueError):
            sources.parse_rss(bomb)

    def test_headlines_cannot_ping_or_format(self):
        text = sources.clean("@everyone **sell** `now` [x](http://evil)")
        self.assertNotIn("@everyone", text)
        self.assertNotIn("**sell**", text)
        self.assertIn("\\*\\*sell\\*\\*", text)

    def test_unsafe_link_is_dropped(self):
        payload = rss_payload(("Nvidia CEO resigns", "a", "Thu, 12 Nov 2026 13:00:00 +0000"))
        payload = payload.replace(b"https://www.example.com/a", b"javascript:alert(1)")
        card = sources.parse_news(payload, NVDA, "important", NOW)[0].card
        self.assertNotIn("url", card)
        self.assertNotIn("javascript", json.dumps(card))


class PriceTests(unittest.TestCase):
    def alert(self, price, memory, moment=NOW):
        quote = sources.parse_quote(quote_payload(price, 100.0, moment))
        return sources.price_alert(NVDA, quote, memory, 5.0, 0.0, NOW)

    def test_reports_each_new_level_once(self):
        alert, memory = self.alert(96.0, {})
        self.assertIsNone(alert)
        alert, memory = self.alert(94.7, memory)
        self.assertEqual(alert.card["title"], "Nvidia is down 5.3% today")
        alert, memory = self.alert(94.0, memory)
        self.assertIsNone(alert)
        alert, memory = self.alert(89.0, memory)
        self.assertEqual(alert.card["title"], "Nvidia is down 11.0% today")
        alert, memory = self.alert(94.0, memory)  # partial recovery is not news
        self.assertIsNone(alert)

    def test_price_card_shows_market_and_ranges(self):
        alert, _ = self.alert(94.7, {})
        shown = fields(alert.card)
        self.assertEqual(shown["Market"], "NasdaqGS · USD")
        self.assertEqual(shown["Now"], "94.70 USD")
        self.assertEqual(shown["Previous close"], "100.00 USD")
        self.assertEqual(shown["52-week range"], "80.00 to 240.00")
        self.assertEqual(alert.card["url"], "https://finance.yahoo.com/quote/NVDA")
        self.assertEqual(alert.card["color"], sources.RED)

    def test_a_new_day_starts_again(self):
        _, memory = self.alert(94.0, {})
        alert, _ = self.alert(94.0, memory, NOW + timedelta(days=1))
        self.assertIsNotNone(alert)

    def test_rises_are_off_by_default(self):
        alert, _ = self.alert(120.0, {})
        self.assertIsNone(alert)


class CheckTests(unittest.TestCase):
    def setUp(self):
        self.temp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.temp)
        self.config = Config("https://discord.com/api/webhooks/1/x", Settings(), (NVDA,))
        self.filings = [EARNINGS]
        self.news = []
        self.price = 100.0
        self.sent = []
        self.fail_delivery = False
        self.broken = set()

    def fetcher(self, url, agent):
        for part in self.broken:
            if part in url:
                raise OSError("down")
        if "data.sec.gov" in url:
            return filings_payload(*self.filings)
        if "feeds.finance.yahoo.com" in url:
            return rss_payload(*self.news)
        return quote_payload(self.price, 100.0)

    def sender(self, webhook, messages, username=""):
        if self.fail_delivery:
            raise notify.DeliveryError("offline")
        self.sent.extend(card for message in messages for card in message)

    def check(self, **extra):
        state = State(self.temp / "state.json")
        return check_once(self.config, state, cache_dir=self.temp, fetcher=self.fetcher,
                          sender=self.sender, now=NOW, log=lambda *_: None, **extra)

    def test_first_run_stays_quiet_then_new_filing_is_sent_once(self):
        self.assertEqual(self.check(), 0)
        self.filings = [RESIGNATION, EARNINGS]
        self.assertEqual(self.check(), 1)
        self.assertIn("Director or top executive", self.sent[0]["title"])
        self.assertEqual(fields(self.sent[0])["Market"], "NasdaqGS · USD")
        self.assertEqual(self.check(), 0)
        self.assertEqual(len(self.sent), 1)

    def test_failed_delivery_is_retried(self):
        self.check()
        self.filings = [RESIGNATION, EARNINGS]
        self.fail_delivery = True
        with self.assertRaises(notify.DeliveryError):
            self.check()
        self.fail_delivery = False
        self.assertEqual(self.check(), 1)

    def test_dry_run_sends_and_saves_nothing(self):
        self.check()
        self.price = 90.0
        self.assertEqual(self.check(dry_run=True), 1)
        self.assertEqual(self.sent, [])
        self.assertEqual(self.check(), 1)  # still unreported, so the real run sends it

    def test_price_drop_is_sent_even_on_the_first_run(self):
        self.price = 93.0
        self.assertEqual(self.check(), 1)
        self.assertEqual(self.sent[0]["title"], "Nvidia is down 7.0% today")

    def test_price_only_watch_ignores_filings_and_news(self):
        fund = Watch("SXR8.DE", "S&P 500 fund", filings=False, news=False, drop_percent=3)
        self.config = Config(self.config.webhook_url, Settings(), (fund,))
        self.broken = {"data.sec.gov", "feeds.finance.yahoo.com"}
        self.price = 96.5
        self.assertEqual(self.check(), 1)
        self.assertEqual(self.sent[0]["title"], "S&P 500 fund is down 3.5% today")

    def test_one_broken_source_does_not_stop_the_others(self):
        self.broken = {"data.sec.gov"}
        self.price = 93.0
        self.assertEqual(self.check(), 1)

    def test_a_source_that_stays_broken_raises_one_warning(self):
        self.broken = {"feeds.finance.yahoo.com"}
        for _ in range(FAILURE_LIMIT + 3):
            self.check()
        warnings = [c for c in self.sent if "Cannot read" in c["title"]]
        self.assertEqual(len(warnings), 1)

    def test_news_flood_is_capped(self):
        self.check()
        stamp = "Thu, 12 Nov 2026 13:30:00 +0000"
        self.news = [(f"Nvidia lawsuit number {i}", f"n{i}", stamp) for i in range(9)]
        self.check()
        news = [c for c in self.sent if c["author"]["name"] == "📰 News"]
        self.assertEqual(len(news), 5)
        self.assertTrue(any("4 more headlines" in c["title"] for c in self.sent))


def calendar_payload(*rows):
    """rows: (title, currency, date, impact, forecast, previous)"""
    names = ("title", "country", "date", "impact", "forecast", "previous")
    return json.dumps([dict(zip(names, row)) for row in rows]).encode()


WEEK = calendar_payload(
    ("FOMC Statement", "USD", "2026-11-12T14:00:00-05:00", "High", "", ""),
    ("Core CPI m/m", "USD", "2026-11-12T08:30:00-05:00", "High", "0.3%", "0.2%"),
    ("German ZEW", "EUR", "2026-11-12T05:00:00-05:00", "Medium", "", ""),
    ("Employment Change", "AUD", "2026-11-12T19:30:00-05:00", "High", "", ""),
    ("GDP q/q", "USD", "2026-11-14T08:30:00-05:00", "High", "0.5%", "0.4%"),
    ("Broken row", "USD", "not a date", "High", "", ""),
)


class CalendarTests(unittest.TestCase):
    def setUp(self):
        self.temp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.temp)
        plan = Calendar(enabled=True, hour_utc=6)
        fund = Watch("SXR8.DE", "S&P 500 fund", filings=False, news=False)
        self.config = Config("https://discord.com/api/webhooks/1/x", Settings(), (fund,),
                             calendar=plan)
        self.sent = []
        self.calendar_fetches = 0
        self.calendar_down = False

    def fetcher(self, url, agent):
        if "faireconomy" in url:
            self.calendar_fetches += 1
            if self.calendar_down:
                raise OSError("down")
            return WEEK
        return quote_payload(100.0, 100.0)

    def check(self, now):
        state = State(self.temp / "state.json")
        return check_once(
            self.config, state, cache_dir=self.temp, fetcher=self.fetcher, now=now,
            sender=lambda url, messages, username="": self.sent.extend(
                card for message in messages for card in message),
            log=lambda *_: None)

    def test_only_matching_currency_and_impact_are_kept_in_time_order(self):
        events = sources.parse_calendar(WEEK, ("USD", "EUR", "CNY"), ("High",))
        self.assertEqual([e["title"] for e in events], ["Core CPI m/m", "FOMC Statement", "GDP q/q"])

    def test_card_lists_the_next_24_hours_once_a_day(self):
        morning = datetime(2026, 11, 12, 6, 5, tzinfo=timezone.utc)
        self.assertEqual(self.check(morning - timedelta(hours=2)), 0)  # before the hour
        self.assertEqual(self.check(morning), 1)
        card = self.sent[0]
        self.assertEqual(card["title"], "2 high-impact events in the next 24 hours")
        self.assertEqual(card["url"], "https://www.forexfactory.com/calendar")
        first, second = card["description"].splitlines()
        self.assertIn("**USD** · Core CPI m/m · forecast 0.3%, previous 0.2%", first)
        self.assertIn(f"<t:{int(datetime(2026, 11, 12, 13, 30, tzinfo=timezone.utc).timestamp())}:t>", first)
        self.assertIn("FOMC Statement", second)
        self.assertEqual(self.check(morning + timedelta(hours=3)), 0)  # not again that day
        self.assertEqual(self.check(morning + timedelta(days=2)), 1)  # GDP day
        self.assertEqual(self.calendar_fetches, 1)  # the local copy was reused

    def test_quiet_day_sends_nothing(self):
        self.assertEqual(self.check(datetime(2026, 11, 15, 7, 0, tzinfo=timezone.utc)), 0)

    def test_failed_feed_is_retried_after_an_hour_not_every_check(self):
        self.calendar_down = True
        morning = datetime(2026, 11, 12, 6, 5, tzinfo=timezone.utc)
        self.check(morning)
        self.check(morning + timedelta(minutes=5))
        self.assertEqual(self.calendar_fetches, 1)
        self.calendar_down = False
        self.assertEqual(self.check(morning + timedelta(minutes=61)), 1)


class Recorder(BaseHTTPRequestHandler):
    bodies: list = []
    replies: list = []

    def do_POST(self):
        length = int(self.headers["Content-Length"])
        Recorder.bodies.append(json.loads(self.rfile.read(length)))
        status, body = Recorder.replies.pop(0) if Recorder.replies else (204, b"")
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        Recorder.bodies, Recorder.replies = [], []
        self.server = HTTPServer(("127.0.0.1", 0), Recorder)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_port}/api/webhooks/1/secret-token"
        self.card = sources.note_card("hello @everyone", sources.GREEN, NOW)

    def test_cards_are_posted_under_the_bot_name_without_pings(self):
        notify.send(self.url, [[self.card]], username="Market Alerts", check_host=False)
        body = Recorder.bodies[0]
        self.assertEqual(body["username"], "Market Alerts")
        self.assertEqual(body["embeds"], [self.card])
        self.assertEqual(body["allowed_mentions"], {"parse": []})

    def test_rate_limit_is_waited_out(self):
        Recorder.replies = [(429, b'{"retry_after": 0.7}'), (204, b"")]
        waits = []
        notify.send(self.url, [[self.card]], check_host=False, sleep=waits.append)
        self.assertEqual(waits, [0.7])
        self.assertEqual(len(Recorder.bodies), 2)

    def test_refusal_is_reported_without_the_webhook_address(self):
        Recorder.replies = [(404, b"{}")]
        with self.assertRaises(notify.DeliveryError) as caught:
            notify.send(self.url, [[self.card]], check_host=False)
        self.assertIn("404", str(caught.exception))
        self.assertNotIn("secret-token", str(caught.exception))

    def test_only_discord_addresses_are_accepted(self):
        with self.assertRaises(notify.DeliveryError):
            notify.send(self.url, [[self.card]])
        self.assertEqual(Recorder.bodies, [])

    def test_cards_are_grouped_within_discord_limits(self):
        self.assertEqual([len(m) for m in notify.pack([self.card] * 12)], [5, 5, 2])
        big = sources.card(label="x", title="t", colour=1, when=NOW, footer="f",
                           description="d" * 1900)
        self.assertEqual([len(m) for m in notify.pack([big] * 5)], [2, 2, 1])


class ConfigTests(unittest.TestCase):
    def test_example_config_loads(self):
        config = load_config(ROOT / "config.example.toml")
        by_symbol = {w.symbol: w for w in config.watches}
        self.assertEqual(config.username, "Market Alerts")
        self.assertEqual(by_symbol["MSF.DE"].sec_ticker, "MSFT")
        self.assertEqual(by_symbol["NOVO-B.CO"].forms, ("6-K", "20-F"))
        self.assertFalse(by_symbol["VWCE.DE"].filings)
        self.assertEqual(by_symbol["VWCE.DE"].drop_percent, 3)
        self.assertFalse(config.calendar.enabled)
        self.assertEqual(config.calendar.currencies, ("USD", "EUR"))


if __name__ == "__main__":
    unittest.main()
