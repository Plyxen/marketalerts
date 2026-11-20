# marketalerts

Discord alerts for the stocks and funds you hold. Python standard library only:
nothing to install, runs happily on a Raspberry Pi.

- **Official filings.** US-listed companies must file a form 8-K with the
  regulator (the SEC) for major events. A CEO or director leaving, bankruptcy, a
  delisting notice and similar items are marked 🔴.
- **News headlines** from Yahoo Finance. By default only headlines that name the
  company and are about things like executive changes, lawsuits, investigations
  and takeovers.
- **Price drops.** When a holding is down 5% on the day, and again at 10%, 15%.

Each alert is a Discord card: a linked title, then fields for the stock or fund,
the market it trades on, today's price and change, the time, and links to the
source document and a chart.

It only reports. It never trades and never suggests a trade, and nothing here is
financial advice. An alert reaches you minutes after the event, long after
trading firms' machines have reacted, so use it to stay informed, not to beat
the price move.

## Setup

Needs Python 3.11 or newer.

```bash
git clone https://github.com/Plyxen/marketalerts.git
cd marketalerts
cp config.example.toml config.toml
```

1. Put your Discord webhook address in `config.toml` (channel settings >
   Integrations > Webhooks). Anyone who has that address can post to the
   channel. `config.toml` is ignored by git; keep it that way.
2. List what you hold, one `[[watch]]` block each, using Yahoo Finance symbols.
3. Try it:

```bash
python -m marketalerts preview --days 10
python -m marketalerts test
python -m marketalerts sample
```

`preview` prints what the last days would have sent and sends nothing. `test`
posts one card listing what is watched. `sample` posts one real example of each
kind of card, marked "Sample".

## Running

```bash
python -m marketalerts check
python -m marketalerts loop
```

`check` looks once. `loop` looks every `check_minutes` and keeps going.
`check --dry-run` prints instead of sending and remembers nothing.

The first check of a new symbol stays quiet: it notes what already exists so you
are not flooded with old items. Price drops are the exception and are sent at
once. If Discord cannot be reached, nothing is marked as sent and the next
check tries again.

## On a Raspberry Pi

```bash
python3 --version                # must be 3.11 or newer
cd ~/marketalerts
chmod 600 config.toml            # other users of the Pi cannot read the webhook
python3 -m marketalerts test
```

To keep it running and start it at boot, edit the user name and folder in
`deploy/marketalerts.service`, then:

```bash
sudo cp deploy/marketalerts.service /etc/systemd/system/
sudo systemctl enable --now marketalerts
systemctl status marketalerts
```

Update later with `git pull` and `sudo systemctl restart marketalerts`.

Set `heartbeat = "daily"` in the config if you want one "still running" message
each morning, so silence never means the tool has stopped.

## Settings

All in `config.toml`; the example file explains each one. Per watched symbol you
can turn `filings`, `news` and `price` off and set its own `drop_percent`.

`symbol` is the listing you hold, so prices match your account. For a company
held through a European listing, add `sec_ticker` and `news_symbol` with its US
ticker so filings and headlines are still found. Non-US companies report with
forms 6-K and 20-F, set with `forms`.

**Optional economic calendar.** Set `enabled = true` under `[calendar]` for one
card each morning listing the high-impact scheduled events of the next 24 hours
(rate decisions, inflation, jobs data) from Forex Factory. It is off by default.

## Limits

- Filings cover companies that report to the US SEC. Funds, indices and most
  non-US companies have none, so watch their price only.
- News and prices come from Yahoo Finance's public feeds. They are unofficial,
  can lag by some minutes and can change without notice. If a source fails for
  12 checks in a row, the tool posts one warning.
- Headlines are filtered by keywords, so an important story with unusual
  wording can be missed and a trivial one can get through.
- The calendar uses Forex Factory's public weekly export, which publishes no
  terms or request limits. The tool keeps a local copy and asks for it at most
  every six hours.

## Tests

```bash
python -m unittest discover -s tests -t .
```
