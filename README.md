# wrss

An aggregated RSS reader. It pulls a fixed list of feeds on a schedule, and
translates foreign-language headlines into English. It serves both a web UI and
a combined feed you can point any reader app at.

## What it does

- Fetches all subscribed feeds in the background on a fixed interval
- Classifies each item as **native** (German or English, headline kept as-is) or
  **foreign** (headline translated on fetch, batched, with per-item fallback)
- Caches translations in SQLite so nothing is translated twice
- Groups feeds into world sections — Europe, Eurasia, Asia, South America,
  Africa — each with a country breakdown in the sidebar
- Translates an entire article on demand, when you click one
- Renders a dark-mode-by-default UI with search, unread tracking, and feed
  management
- Serves a combined feed for reader apps, with optional filters

Categories are seeded — Tech, World, Politics, and several regional ones. Feeds
added through the UI can take any category and any section; leave the category
blank and a keyword guesser picks one from the feed's content.

## Sections

The sidebar groups feeds by `region`, then by `country`. Both are free text on
the feed row, so a feed added through the UI lands wherever you put it.

| Section | Countries |
|---|---|
| Europe | Croatia, France, Spain, Belgium, Serbia, North Macedonia, Albania, Kosovo |
| Eurasia | Ukraine, Russia |
| Asia | Japan, China, South Korea, Taiwan, Singapore, India, Asia-Pacific |
| South America | Brazil, Argentina, Colombia, Chile, Ecuador, Peru |
| Africa | Kenya, Nigeria, Zimbabwe, Zambia, Namibia, and pan-African wires |

The original general-interest feeds (heise, Guardian, BBC, NYT, …) carry no
region and sit under "All news".

## Reading a foreign article

There are three ways, and all are offered because they fail in different places.

**Google Translate** (`G` on the card) wraps the article URL in
`translate.google.com/translate`. Free, instant, no model call. It fails when
the publisher will not serve its article pages to a proxy — Le Monde, De
Standaard and KOHA all do this.

**Translate this article** (`⇄` on the card) fetches the page here, extracts
the article body with trafilatura, and sends it through the LLM. That survives
a script wall or a paywall, because it is just another HTTP client. It costs
one model call, so it runs only when clicked and the result is cached in
`article_translations` — a second visit costs nothing.

**Translate this headline** (`⇄` next to an untranslated headline) does the one
line, without waiting for the queue. It shows on foreign headlines that have no
English yet, and costs one model call, cached in `translations` by the headline's
own hash — the same headline on another feed is then free.

Unlike the other two, this button keeps working while translation is paused. The
master switch pauses the *bulk queue*, because that is what spends quota
unattended; a person clicking one headline is spending deliberately, and a pause
that blocked this too would make the button lie. It is still bounded by the daily
budget and by any rate-limit cooldown, so a click can never be the thing that
walks into an exhausted quota.

Both are also reflected in the reader-app feed: `<link>` stays the publisher URL
so the reader's open button does the obvious thing, and the description carries
a "Read this article in English" link.

## Controlling what it pulls and translates

The `🛠 Admin` button in the sidebar opens a panel with four controls.

**A master switch** turns foreign-headline translation off entirely. Nothing new
is sent to the model. Already-translated headlines keep their English titles —
switching off means "stop spending", not "un-read what was already paid for" —
so turning it back on resumes where it left off.

The switch covers every path to the provider, not just the headline queue. The
on-demand `⇄` translation is gated by it too, so a paused service cannot spend
on a click; Google Translate is free and stays offered as the alternative. The
`⇄` button is hidden while paused rather than left to fail, and the sidebar
shows how many headlines are waiting.

**Per-section and per-country switches**, each with its own *Pull* and
*Translate* control, act on every feed in that group. *Pull* stops fetching new
articles for them; *Translate* stops translating their untranslated headlines.
Groups are the ones the sidebar already browses by (`region` and `country`),
including the "(no section)" group for feeds that have neither set. A group with
no saved rule is enabled, so a section that appears in the feeds table later
works without a migration.

**A daily call budget and an overnight work window** pace the queue. The budget
counts provider calls per UTC day, which is when OpenRouter's free tier resets;
the default is 800 of its 1000, leaving headroom. The window defaults to
22:00–07:00 local and may wrap past midnight; same start and end means always
on. A background worker drains the backlog only inside the window, and only up
to the budget. Turning translation off stops it immediately regardless.

**Translate queued now** drains the whole backlog immediately, ignoring both the
window and the budget. This is the switch for "I have repointed the proxy key at
a model I am paying for". It is one-shot: it switches itself off once the queue
empties or the budget is genuinely spent.

**Delete untranslated backlog** removes every untranslated foreign article.
Already-translated articles and all native articles are kept. It is
irreversible, so the button first asks the server for the exact count and states
it in the confirmation dialog.

### Rate limiting

OpenRouter's free tier is a hard 1000 calls/day. Two things used to make hitting
that limit much worse than it needed to be, and both are fixed:

- The OpenAI SDK's default `max_retries=2` silently attempted every call three
  times. The client is now constructed with `max_retries=0`.
- A failed batch used to retry each of its headlines individually, on top of
  that. The per-item fallback now runs only when the model failed to *return* a
  headline, never when the *call* failed — so an exhausted quota costs one
  attempt instead of up to 32.

On a 429 the worker pauses until the provider's own `X-RateLimit-Reset`, falls
back to one hour if the header is missing, and caps any honoured cooldown at 24h
so a malformed header cannot park translation indefinitely.

Settings live in the `settings` table and survive restarts. `WRSS_DB` overrides
the database path, which is how the test suites run against a copy.

## Stack

Python, Flask, `feedparser`, `requests`, `trafilatura`, and the OpenAI SDK
pointed at an internal LLM gateway rather than a provider directly. SQLite for
everything. Templates and static assets are separate files.

## Installation

```bash
git clone <repo-url> wrss
cd wrss

python3 -m venv venv
./venv/bin/pip install -r requirements.txt

cp .env.example .env 2>/dev/null || printf 'LLMPROXY_KEY=\nLLMPROXY_BASE_URL=http://127.0.0.1:[REDACTED:loopback-port]\n' > .env
chmod 600 .env
# LLMPROXY_KEY required — translation runs through the internal LLM gateway
```

The database and schema are created on first run.

## Running it

    ./venv/bin/python wrss.py

It is served at the root of its own hostname, `https://rss.william.hr/`, behind
nginx on `127.0.0.1:[REDACTED:loopback-port]`. It used to be mounted at a path prefix
(`https://no.inbox.li/wrss`) under a hostname shared with twenty other services;
that path now answers 410 Gone.

## Configuration

`.env`, mode `0600`, owned by the service account:

- `LLMPROXY_KEY`, `LLMPROXY_BASE_URL` — the internal LLM gateway credential
- `MONOCLE_TRACE`, `MONOCLE_TRACE_OUTPUT_PATH` — tracing on/off and where
- `WRSS_DB` — override the database path (used for testing against a copy)
- `WRSS_PORT` — override the listen port (default [REDACTED:loopback-port])
- the tuning knobs: entries kept per feed, feed refresh interval, batch sizes,
  and the per-headline token cap

Everything the admin panel changes lives in the `settings` table rather than
`.env`, so it can be changed at runtime without a restart.

`MONOCLE_TRACE` defaults to on, which means every translation call is recorded
to disk. Set it to `0` if you do not want that.

## Reader-app feed

The app serves a combined feed at `/feed`. Fetching
it from anything that does not carry a valid session gets redirected to the
login page. Subscribe with a cookie-capable reader, or just use the web UI.

Optional filters combine: `kind`, `cat`, `region`, `country`, `source`, and
`limit` (capped). `region=Europe&country=Serbia` gives you one country's feeds
in a reader app.

### Images and article HTML

Each item carries its lead image in three dialects — enclosure, media thumbnail,
and media content — plus the full article markup in a namespaced element and a
lead image inside the description, so that readers which ignore any one of them
still show a picture.

Feeds disagree about where the picture is, so the extractor tries each location
in turn and finally falls back to the first image in the article HTML. The
cleaner then makes URLs absolute, drops script and iframe elements, inline
event handlers, and `data:` URIs, and caps the result at a character limit.

Around 80% of items have a picture. The rest are publishers that put no image in
their feed at all; filling those would mean scraping the article pages, which
this deliberately does not do.

## Things worth knowing

**No authentication, and no session at all.** The app is stateless by design —
there is nothing to sign, because it keeps no per-user state. Access control is
entirely the reverse proxy's job, and it binds loopback so that job has to be
done properly. Read the app's own comment before changing the bind address: the
absence of a session is a deliberate consequence of there being no sessions, not
an oversight.

**This is a server-side request forgery risk if you extend it.** The feed fetch
rejects loopback, private ranges, and the cloud metadata address, and refuses
embedded credentials. The on-demand article translation reuses that exact guard,
re-validating every redirect hop, because it also fetches a URL taken from the
database. Keep it if you add anything else that fetches a stored URL — the
metadata address in particular is the one people forget.

**Article fetches use an honest User-Agent.** Publishers that serve a feed
happily to `wrss/1.0` often refuse the same client's article page, so article
requests go out as `Mozilla/5.0 (compatible; wrss/1.0; +https://rss.william.hr/)`.
Measured over 12 publishers that extracted the body 9 times out of 12, exactly
matching a full Chrome impersonation — so there is no reason to pretend to be
Chrome, and a real reason not to.

**Translation is the expensive part.** Batching and the cache keep headline
translation down, and the per-headline token cap stops one long headline from
eating the budget. `TRANSLATE_PENDING_CAP` is 240 per cycle, raised from the
original 80 when the feed roster grew from two foreign feeds to about sixty — at
80 a single seed would have taken hours to become readable. Article translation
is separate again: one call per article, on click, cached.

**Not every country has a feed, because most do not publish one.** Around half
the obvious candidates for these countries are dead — NXDOMAIN, parked domains,
or a 403 on every path. Every URL in `SEED_FEEDS` was fetched and parsed before
being added. Notable gaps: Kosovo has no dedicated feed at all (every outlet is
dead or parked), so `albinfo-ch` and `shtypi-al` cover it from outside; Le
Soir, VRT and Izvestia 403 every path; Ukraine's `.gov.ua` ministries have no
surviving feeds.

**Russian and Ukrainian state outlets are listed deliberately.** TASS, RIA, RT
and 5-TV sit beside the independent and exile outlets, not instead of them. The
point of including them is to see the framing, and that only works if it is
visible next to something to compare it against.

**Tracing is on by default and writes prompt and completion content to a shared
directory.** That is user-visible content — headlines, translated articles — so
treat those files as data you would rather not keep forever. Turn tracing off,
or prune, deliberately.
## License

MIT — see [LICENSE](LICENSE).
