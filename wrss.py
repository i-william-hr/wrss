#!/usr/bin/env python3
"""wrss - aggregated RSS reader with foreign headline translation (OpenRouter).

Feeds are grouped into world sections (Europe, Asia, South America, Africa,
Eurasia). Native feeds (German/English) are kept as-is; foreign feeds get their
headlines translated to TRANSLATE_LANG via an OpenAI-compatible API (OpenRouter).

Two ways to read a foreign article, because neither alone is enough:
  - a Google Translate URL, free and instant but useless when the publisher
    bot-walls its own article pages;
  - an on-demand LLM translation of the extracted article body, which costs one
    model call and only happens when somebody actually asks for it.

Run:  ./run.sh   ->  http://127.0.0.1:[REDACTED:loopback-port]/  (nginx: https://rss.william.hr/)
Auth: none of its own. nginx gates the whole vhost with the SSO cookie; see
      [REDACTED:server-path] There is no login form
      and no ?auth= token, which also means /feed is reachable from a signed-
      in browser only -- a reader app that cannot hold an SSO cookie can no
      longer subscribe to it.

It used to be a path under a shared hostname, at https://no.inbox.li/wrss, and
carried the PrefixMiddleware below so it could be mounted under that prefix.
It has its own hostname now, so it serves from the root and the middleware is
gone. nginx passes the request path through untouched either way.
"""

import calendar
import hashlib
import html
import logging
import math
import os
import re
import sqlite3
import threading
import time
import ipaddress
import socket
from contextlib import contextmanager
from email.utils import formatdate
from html.parser import HTMLParser
from urllib.parse import urlparse, urljoin, quote

import feedparser
import requests
from flask import (Flask, Response, jsonify, render_template, request)
from openai import APIStatusError, OpenAI, RateLimitError
from werkzeug.middleware.proxy_fix import ProxyFix

# --- MONOCLE LLM TRACING (monocle2ai / monocle-apptrace) ---
# Records every OpenAI-SDK LLM call as OTel spans, written as JSON files to
# [REDACTED:server-path] and browsable at /llmtrace/ .
# Disable at runtime with MONOCLE_TRACE=0 in the environment.
if os.environ.get("MONOCLE_TRACE", "1") != "0":
    try:
        os.environ.setdefault("MONOCLE_TRACE_OUTPUT_PATH", "[REDACTED:server-path]")
        from monocle_apptrace.instrumentation.common.instrumentor import setup_monocle_telemetry
        setup_monocle_telemetry(workflow_name="wrss", monocle_exporters_list="file")
    except Exception as _monocle_err:  # never let tracing break the app
        logging.getLogger(__name__).warning("monocle tracing unavailable: %s", _monocle_err)

# ----------------------------------------------------------------- config ---
HOST = "127.0.0.1"
# WRSS_PORT exists so a second instance can run against a copy of the database
# without editing this file: PORT was hardcoded, which made "just test it
# locally" a code change. Same intent as WRSS_DB below.
PORT = int(os.environ.get("WRSS_PORT") or [REDACTED:loopback-port])
# Served at the root of its own hostname (https://rss.william.hr/), not under a
# path prefix. Empty is the correct value here and is passed to the template as
# the prefix the frontend prepends to every API call, so "no prefix" is what
# the browser wants rather than a leftover from the no.inbox.li/wrss days.
PREFIX = ""
DB_PATH = os.environ.get("WRSS_DB") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "wrss.db")

# Two LLM lanes, both OpenAI-compatible. Secrets stay in .env and arrive via
# wrss.service's EnvironmentFile; everything else is a runtime setting edited
# in the admin panel.
#
# The ongoing lane serves the steady trickle: worker ticks inside the window,
# per-headline clicks, on-demand article bodies. The batch lane serves only
# the forced drain behind "Translate queued now".
#
# Routed through llmproxy, the key label (XAI_MODEL_*) is what routes -- the
# proxy ignores the model field, so the real model (or a :floor variant) is
# retargeted per lane from the llmproxy panel without restarting this service.
# Pointed direct at OpenRouter instead, the model field is the model id and a
# :floor suffix there pins floor pricing with default provider routing.
#
# The XAI_* names are a historical misnomer and always were, but renaming them
# would be a wider diff than the change is worth.
XAI_API_KEY = os.environ.get("LLMPROXY_KEY", "")
XAI_BASE_URL = os.environ.get("LLMPROXY_BASE_URL", "https://llm.inbox.li/v1")
XAI_API_KEY_BATCH = os.environ.get("LLMPROXY_KEY_BATCH", "")
XAI_BASE_URL_BATCH = os.environ.get("LLMPROXY_BASE_URL_BATCH", "")
MAX_TOKENS_PER_HEADLINE = 120   # translated headlines are one line; the longest
                                # pending headline on record is 233 chars, so this
                                # still leaves 2x headroom while capping what a
                                # reasoning model can spend thinking per headline
TRANSLATE_LANG = "English"      # target language for foreign headlines
TRANSLATE_BATCH = 24            # headlines per API call
# Max backlog translated per refresh cycle. The original 80 was sized for a
# reader with two foreign feeds; at the current roster a cycle brings in
# several hundred new foreign headlines, and 80 per 10 minutes would have taken
# hours to work through a single seed. Raising it to 240 (30 batched calls) means
# a new feed list is readable within a cycle or two instead of the better part
# of a day, and the steady-state cost is unchanged because in steady state the
# backlog per cycle is much smaller than the cap.
TRANSLATE_PENDING_CAP = 240

REFRESH_INTERVAL = 600          # seconds between fetch cycles
ENTRIES_PER_FEED = 40           # entries stored per fetch
KEEP_PER_FEED = 150             # articles kept per feed

# ------------------------------------------------------- translation pacing ---
# Both lanes route through llmproxy to paid OpenRouter (google/gemini-2.5-
# flash-lite; the free Google key is only a fallback), so nothing here rations
# a free allowance any more. The budget below is a runaway guard, and the day
# is keyed in UTC to match the provider's quota day. A cycle that runs into an
# exhausted quota used to turn into a retry storm, which is why the worker
# still paces itself and why _create() no longer lets the SDK retry on its own.
SETTINGS_DEFAULTS = {
    "translate_enabled":       "1",
    "translate_daily_budget":  "2500",  # runaway guard, not a ration
    "translate_window_start":  "0",     # local hour; window may wrap past midnight
    "translate_window_end":    "24",    # 0/24 = always open: paid routing needs
                                        # no nightly window to stretch a free tier
    "translate_per_tick":      "240",   # headlines per aggregated automatic
                                        # drain, i.e. full TRANSLATE_BATCH chunks
    # Per-lane model: key label when proxied (proxy ignores it and routes by
    # key), model id when pointed direct -- where a :floor suffix pins floor
    # pricing while OpenRouter still picks the provider.
    "ongoing_model":           "wrss/news",
    "batch_model":             "wrss/news",
    # List prices per 1M tokens, used only to display spend. Edit when the
    # lane's model changes; the provider bill is the source of truth.
    "ongoing_price_in":        "0.10",
    "ongoing_price_out":       "0.40",
    "batch_price_in":          "0.10",
    "batch_price_out":         "0.40",
}
# How often the translation worker looks for something to do. Short enough that
# the forced drain starts promptly and works a backlog one full cycle per tick;
# the automatic lane runs at most one aggregated drain per AUTO_TRANSLATE_INTERVAL
# inside this loop (see translate_tick), so a small tick is not more calls.
TRANSLATE_TICK = 30
# Minimum spacing between automatic (non-forced) drains. Each run gathers the
# whole pending backlog and translates it in full TRANSLATE_BATCH chunks, so
# one aggregated press every five minutes replaces the old 12-headlines-per-30s
# trickle that paid for half-empty calls in free-tier call count. Page-load
# translation covers fresh headlines while someone is actually reading.
AUTO_TRANSLATE_INTERVAL = 300
# Pause between provider chunks inside any multi-chunk run (page runs, the
# 5-min aggregated drains). The :free models wrss translates with sit under
# OpenRouter's account-wide 20 requests/min cap, and a full 240-headline drain
# is 10 back-to-back calls -- exactly the burst shape that trips it. Two
# seconds puts a 10-call drain at ~26 calls/min peak, under the cap; wrss's
# own daily budget is what bounds total spend, not this pacing. Single
# headline clicks and single article bodies are untouched.
AUTO_TRANSLATE_CHUNK_DELAY = 2.0
# Ceiling on how long a rate-limit cooldown may be honoured when the provider
# does not send a usable reset header. Never longer than one quota day.
COOLDOWN_MAX = 24 * 3600

# Master kill-switch for AUTOMATIC translation only (2026-10-14): while True, the
# background worker (including the forced drain) and the new-feed trickle refuse
# -- nothing is spent unattended. Deliberate per-click paths stay open: the
# single-headline button, the visible-page bulk run, the on-demand article body
# and the free Google Translate links. Cached translations keep displaying;
# nothing is deleted. Re-enable automatic by setting False and restarting.
# The translate_enabled admin setting underneath is left untouched.
TRANSLATE_AUTO_DISABLED = False

MAX_CONTENT_CHARS = 8000        # cap on stored HTML per article
MEDIA_NS = "http://search.yahoo.com/mrss/"
CONTENT_NS = "http://purl.org/rss/1.0/modules/content/"
IMG_EXT_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
                 ".gif": "image/gif", ".webp": "image/webp", ".avif": "image/avif",
                 ".svg": "image/svg+xml", ".bmp": "image/bmp"}

# On-demand article translation. The headline translation above runs for every
# foreign headline on every fetch; this runs only when a reader asks for a
# specific article, and the result is cached, so the cost is bounded by
# how many articles people actually open rather than by the feed volume.
ARTICLE_EXTRACT_CHARS = 5000    # input cap for one article translation
ARTICLE_FETCH_TIMEOUT = 25      # seconds to fetch one article page
ARTICLE_EXTRACT_MIN = 400       # below this the extractor found no real body

# trafilatura is a hard dependency of article extraction; import it lazily so a
# broken install degrades to "no on-demand translation" instead of a dead app.
_trafilatura = None
_trafilatura_failed = False


def _get_trafilatura():
    global _trafilatura, _trafilatura_failed
    if _trafilatura is None and not _trafilatura_failed:
        try:
            import trafilatura
            _trafilatura = trafilatura
        except Exception as exc:
            _trafilatura_failed = True
            log.warning("article extraction unavailable: %s", exc)
    return _trafilatura

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("wrss")

# --------------------------------------------------------------- seed data ---
# name, url, kind, category, region, country
#
# region is the section the sidebar groups by; country is the sub-group inside
# it. Both are free text on purpose -- category still exists and still drives
# the /feed?cat= filters, but the browse UI is organised by section.
#
# Every URL below was fetched and parsed with the app's own User-Agent before
# being added, because roughly half the obvious candidates for these countries
# are dead (NXDOMAIN, parked, or 403) and a feed that 404s forever just shows up
# as a permanently broken row in the Feeds panel. region="" means "not part of a
# world section" -- that is the original general-interest list, unchanged.
SEED_FEEDS = [
    # -- general interest, no section ------------------------------------
    ("heise",          "https://www.heise.de/rss/heise-atom.xml",                          "native",  "Tech",     "",    ""),
    ("guardian",       "https://www.theguardian.com/world/rss",                            "native",  "World",    "",    ""),
    ("bbc",            "http://feeds.bbci.co.uk/news/world/rss.xml",                       "native",  "World",    "",    ""),
    ("tagesschau",     "https://www.tagesschau.de/xml/rss2",                               "native",  "World",    "",    ""),
    ("derstandard",    "https://www.derstandard.at/rss",                                   "native",  "World",    "",    ""),
    ("nytimes",        "https://rss.nytimes.com/services/xml/rss/nyt/World.xml",           "native",  "World",    "",    ""),
    ("wapo",           "https://feeds.washingtonpost.com/rss/world",                       "native",  "World",    "",    ""),
    ("foxnews",        "https://moxie.foxnews.com/google-publisher/latest.xml",            "native",  "World",    "",    ""),
    ("jungefreiheit",  "https://jungefreiheit.de/feed/",                                   "native",  "Politics", "",    ""),
    ("aljazeera",      "https://www.aljazeera.com/xml/rss/all.xml",                        "native",  "World",    "",    ""),

    # -- Europe: Croatia -------------------------------------------------
    ("index.hr",       "https://www.index.hr/rss/vijesti",                                 "foreign", "Croatia",  "Europe", "Croatia"),
    ("24sata",         "https://www.24sata.hr/feeds/aktualno.xml",                         "foreign", "Croatia",  "Europe", "Croatia"),

    # -- Europe: France --------------------------------------------------
    ("lemonde-intl",   "https://www.lemonde.fr/international/rss_full.xml",               "foreign", "World",    "Europe", "France"),
    ("lemonde-pol",    "https://www.lemonde.fr/politique/rss_full.xml",                    "foreign", "Politics", "Europe", "France"),
    ("france24-fr",    "https://www.france24.com/fr/rss",                                  "foreign", "World",    "Europe", "France"),
    ("euronews",       "https://www.euronews.com/rss?level=theme&name=news",               "native",  "World",    "Europe", "France"),

    # -- Europe: Spain ---------------------------------------------------
    ("elpais",         "https://feeds.elpais.com/mrss-s/pages/ep/site/elpais.com/portada", "foreign", "World",    "Europe", "Spain"),
    ("lavanguardia",   "https://www.lavanguardia.com/rss/home.xml",                       "foreign", "World",    "Europe", "Spain"),
    ("abc-intl",       "https://www.abc.es/rss/feeds/abc_Internacional.xml",              "foreign", "World",    "Europe", "Spain"),
    ("eldiario",       "https://www.eldiario.es/rss/",                                     "foreign", "World",    "Europe", "Spain"),
    ("elmundo-intl",   "https://e00-elmundo.uecdn.es/elmundo/rss/internacional.xml",      "foreign", "World",    "Europe", "Spain"),

    # -- Europe: Belgium -------------------------------------------------
    # RTBF serves no feeds on www.rtbf.be at all; they live on rss.rtbf.be,
    # per the help article linked from their own site.
    ("rtbf-info",      "https://rss.rtbf.be/article/rss/highlight_rtbf_info.xml",          "foreign", "World",    "Europe", "Belgium"),
    ("rtbf-belgique",  "https://rss.rtbf.be/article/rss/highlight_rtbf_info-belgique.xml", "foreign", "World",    "Europe", "Belgium"),
    ("lalibre",        "https://www.lalibre.be/arc/outboundfeeds/rss/?outputType=xml",     "foreign", "World",    "Europe", "Belgium"),
    ("dhnet",          "https://www.dhnet.be/arc/outboundfeeds/rss/section/actu/?outputType=xml", "foreign", "World", "Europe", "Belgium"),
    ("bruzz",          "https://www.bruzz.be/rss.xml",                                     "foreign", "World",    "Europe", "Belgium"),
    ("nieuwsblad",     "https://www.nieuwsblad.be/rss",                                    "foreign", "World",    "Europe", "Belgium"),
    ("standaard",      "https://www.standaard.be/rss/",                                    "foreign", "World",    "Europe", "Belgium"),

    # -- Europe: Balkans -- Serbia ----------------------------------------
    ("nova-rs",        "https://nova.rs/feed/",                                           "foreign", "World",    "Europe", "Serbia"),
    ("blic-rs",        "https://www.blic.rs/rss/feed",                                    "foreign", "World",    "Europe", "Serbia"),
    ("danas-rs",       "https://danas.rs/rss",                                             "foreign", "World",    "Europe", "Serbia"),

    # -- Europe: Balkans -- North Macedonia ------------------------------
    ("lokalno-mk",     "https://lokalno.mk/feed/",                                         "foreign", "World",    "Europe", "North Macedonia"),
    ("a1on-mk",        "https://a1on.mk/rss/",                                            "foreign", "World",    "Europe", "North Macedonia"),
    ("expres-mk",      "https://expres.mk/feed/",                                          "foreign", "World",    "Europe", "North Macedonia"),
    ("skopjeinfo",     "https://skopjeinfo.mk/feed/",                                      "foreign", "World",    "Europe", "North Macedonia"),

    # -- Europe: Balkans -- Albania --------------------------------------
    ("telegrafi-al",   "https://telegrafi.com/feed/",                                      "foreign", "World",    "Europe", "Albania"),
    ("koha-al",        "https://koha.net/rss/",                                            "foreign", "World",    "Europe", "Albania"),
    ("lajmi-al",       "https://www.lajmi.net/feed/",                                      "foreign", "World",    "Europe", "Albania"),
    ("standard-al",    "https://standard.al/feed/",                                        "foreign", "World",    "Europe", "Albania"),
    ("reporter-al",    "https://www.reporter.al/feed/",                                    "foreign", "Politics", "Europe", "Albania"),
    ("rtsh-al",        "https://rtsh.al/feed/",                                           "foreign", "World",    "Europe", "Albania"),
    ("news24-al",      "https://www.news24.al/feed",                                       "foreign", "World",    "Europe", "Albania"),
    ("shtypi-al",      "https://shtypi.net/feed/",                                         "foreign", "World",    "Europe", "Albania"),

    # -- Europe: Balkans -- Kosovo ---------------------------------------
    # There is no working dedicated Kosovo feed: every outlet tried was either
    # NXDOMAIN or a parked domain with an empty feed. These two carry Kosovo
    # coverage from outside. See README.
    ("albinfo-ch",     "https://albinfo.ch/feed/",                                         "foreign", "World",    "Europe", "Kosovo"),
    ("vecernji",       "https://www.vecernji.hr/feed/",                                   "foreign", "World",    "Europe", "Croatia"),

    # -- Eurasia: Ukraine -----------------------------------------------
    ("pravda-ua",      "https://www.pravda.com.ua/rss/",                                   "foreign", "World",    "Eurasia", "Ukraine"),
    ("pravda-ua-en",   "https://www.pravda.com.ua/eng/rss/",                               "foreign", "World",    "Eurasia", "Ukraine"),
    ("ukrinform",      "https://www.ukrinform.net/rss/block-lastnews",                     "foreign", "World",    "Eurasia", "Ukraine"),
    ("nv-ua",          "https://nv.ua/rss/all.xml",                                        "foreign", "World",    "Eurasia", "Ukraine"),
    ("kyivpost",       "https://www.kyivpost.com/feed",                                    "foreign", "World",    "Eurasia", "Ukraine"),
    ("interfax-ua",    "https://en.interfax.com.ua/news/last.rss",                          "foreign", "World",    "Eurasia", "Ukraine"),

    # -- Eurasia: Russia ------------------------------------------------
    # Both sides are here on purpose. TASS/RIA/RT/5-TV are state outlets and
    # their framing is not the app's to filter -- the point of listing them is
    # to see it. They sit beside the independent and exile outlets, not instead
    # of them, so a reader can compare.
    ("ria",            "https://ria.ru/export/rss2/archive/index.xml",                     "foreign", "World",    "Eurasia", "Russia"),
    ("tass-ru",        "https://tass.ru/rss/v2.xml",                                       "foreign", "World",    "Eurasia", "Russia"),
    ("tass-en",        "https://tass.com/rss/v2.xml",                                      "native",  "World",    "Eurasia", "Russia"),
    ("rt-ru",          "https://russian.rt.com/rss",                                       "foreign", "World",    "Eurasia", "Russia"),
    ("5tv",            "https://www.5-tv.ru/news/rss",                                    "foreign", "World",    "Eurasia", "Russia"),
    ("vedomosti",      "https://www.vedomosti.ru/rss/news",                                "foreign", "World",    "Eurasia", "Russia"),
    # kommersant / mk-ru / lenta-ru removed 2026-11-14: read-timeout on every
    # fetch for days (per-host throttling of this box's IP; tass/ria/rt still
    # answer), agreed with the user -- rows deleted from the DB alongside.
    ("meduza",         "https://meduza.io/rss/en/all",                                     "native",  "World",    "Eurasia", "Russia"),
    ("novaya-eurasia", "https://novayagazeta.eu/feed/rss/en",                              "native",  "World",    "Eurasia", "Russia"),
    ("insider-ru",     "https://theinsider.ru/feed",                                       "foreign", "World",    "Eurasia", "Russia"),
    ("rferl",          "https://www.rferl.org/api/",                                       "native",  "World",    "Eurasia", "Russia"),
    ("bbc-ru",         "https://feeds.bbci.co.uk/russian/rss.xml",                        "native",  "World",    "Eurasia", "Russia"),

    # -- Asia -----------------------------------------------------------
    ("nhk-intl",       "https://www3.nhk.or.jp/rss/news/cat6.xml",                        "foreign", "World",    "Asia", "Japan"),
    ("scmp",           "https://www.scmp.com/rss/91/feed",                                 "native",  "World",    "Asia", "China"),
    ("scmp-china",     "https://www.scmp.com/rss/4/feed",                                  "native",  "World",    "Asia", "China"),
    ("nikkei-asia",    "https://asia.nikkei.com/rss/feed/nar",                            "native",  "Business", "Asia", "Japan"),
    ("cna",            "https://www.channelnewsasia.com/api/v1/rss-outbound-feed?_format=xml", "native", "World", "Asia", "Singapore"),
    ("yonhap",         "https://en.yna.co.kr/RSS/news.xml",                                "native",  "World",    "Asia", "South Korea"),
    ("taipeitimes",    "https://www.taipeitimes.com/xml/index.rss",                        "native",  "World",    "Asia", "Taiwan"),
    ("straitstimes",   "https://www.straitstimes.com/news/world/rss.xml",                  "native",  "World",    "Asia", "Singapore"),
    ("thehindu",       "https://www.thehindu.com/news/international/feeder/default.rss",    "native",  "World",    "Asia", "India"),
    ("france24-ap",    "https://www.france24.com/en/asia-pacific/rss",                     "native",  "World",    "Asia", "Asia-Pacific"),
    ("rfa",            "https://www.rfa.org/english/rss2.xml",                             "native",  "World",    "Asia", "Asia-Pacific"),
    ("japantimes",     "https://www.japantimes.co.jp/feed/",                               "native",  "World",    "Asia", "Japan"),
    ("peopledaily-cn", "http://www.people.com.cn/rss/politics.xml",                       "foreign", "Politics", "Asia", "China"),
    ("xinhua-cn",      "http://www.xinhuanet.com/politics/news_politics.xml",              "foreign", "Politics", "Asia", "China"),
    ("chosun-kr",      "https://www.chosun.com/arc/outboundfeeds/rss/?outputType=xml",     "foreign", "World",    "Asia", "South Korea"),

    # -- South America ---------------------------------------------------
    ("g1-mundo",       "https://g1.globo.com/rss/g1/mundo/",                              "foreign", "World",    "South America", "Brazil"),
    ("folha-mundo",    "https://feeds.folha.uol.com.br/mundo/rss091.xml",                 "foreign", "World",    "South America", "Brazil"),
    ("agenciabrasil",  "https://agenciabrasil.ebc.com.br/rss/ultimasnoticias/feed.xml",    "foreign", "World",    "South America", "Brazil"),
    ("clarin-mundo",   "https://www.clarin.com/rss/mundo/",                                "foreign", "World",    "South America", "Argentina"),
    ("lanacion",       "https://www.lanacion.com.ar/arc/outboundfeeds/rss/?outputType=xml", "foreign", "World", "South America", "Argentina"),
    ("eltiempo-mundo", "https://www.eltiempo.com/rss/mundo.xml",                          "foreign", "World",    "South America", "Colombia"),
    ("eltiempo-col",   "https://www.eltiempo.com/rss/colombia.xml",                       "foreign", "World",    "South America", "Colombia"),
    ("eluniverso",     "https://www.eluniverso.com/arc/outboundfeeds/rss/category/noticias/?outputType=xml", "foreign", "World", "South America", "Ecuador"),
    ("latercera-cl",   "https://www.latercera.com/rss",                                    "foreign", "World",    "South America", "Chile"),
    ("andina-pe",      "https://andina.pe/agencia/rss",                                    "foreign", "World",    "South America", "Peru"),

    # -- Africa ----------------------------------------------------------
    ("voa-africa",     "https://www.voanews.com/api/zq$omekvi_",                           "native",  "World",    "Africa", "Africa"),
    ("rfi-africa",     "https://www.rfi.fr/en/africa/rss",                                "native",  "World",    "Africa", "Africa"),
    ("africanews",     "https://www.africanews.com/feed/rss",                              "native",  "World",    "Africa", "Africa"),
    ("france24-africa","https://www.france24.com/en/africa/rss",                          "native",  "World",    "Africa", "Africa"),
    ("allafrica-biz",  "https://allafrica.com/tools/headlines/rdf/business/headlines.rdf", "native",  "Business", "Africa", "Africa"),
    ("namibian-na",    "https://www.namibian.com.na/feed/",                                "native",  "World",    "Africa", "Namibia"),
    ("nation-ke",      "https://nation.africa/kenya/rss.xml",                             "native",  "World",    "Africa", "Kenya"),
    ("standard-ke",    "https://www.standardmedia.co.ke/rss/headlines.php",                "native",  "World",    "Africa", "Kenya"),
    # eastafrican removed 2026-11-14: the URL 404s on every fetch (feed moved
    # or retired upstream); agreed with the user -- row deleted from the DB.
    ("premiumtimes-ng","https://www.premiumtimesng.com/feed/",                             "native",  "World",    "Africa", "Nigeria"),
    ("dailytrust-ng",  "https://dailytrust.com/feed/",                                     "native",  "World",    "Africa", "Nigeria"),
    ("tribune-ng",     "https://tribuneonlineng.com/feed/",                                "native",  "World",    "Africa", "Nigeria"),
    ("vanguard-ng",    "https://www.vanguardngr.com/feed/",                                "native",  "World",    "Africa", "Nigeria"),
    ("thisday-ng",     "https://www.thisdaylive.com/index.php?/feed/",                     "native",  "World",    "Africa", "Nigeria"),
    ("zimindep",       "https://www.theindependent.co.zw/feed/",                          "native",  "World",    "Africa", "Zimbabwe"),
    ("zambianeye",     "https://www.zambianeye.com/feed/",                                 "native",  "World",    "Africa", "Zambia"),
]

# Source language per country, used only to build Google Translate URLs. An
# unknown country gets "auto", which makes Google detect the language itself --
# that is the right default and it is why this does not need to be exact.
COUNTRY_LANG = {
    "Croatia": "hr", "France": "fr", "Spain": "es", "Belgium": "nl",
    "Serbia": "sr", "North Macedonia": "mk", "Albania": "sq", "Kosovo": "sq",
    "Ukraine": "uk", "Russia": "ru", "Japan": "ja", "China": "zh",
    "South Korea": "ko", "Brazil": "pt", "Argentina": "es", "Colombia": "es",
    "Ecuador": "es", "Chile": "es", "Peru": "es",
}

# Display order of the world sections in the sidebar. Anything not listed sorts
# after these, alphabetically, so a feed added through the UI with a novel
# region still shows up somewhere sensible rather than disappearing.
SECTION_ORDER = ["Europe", "Eurasia", "Asia", "South America", "Africa"]

CATEGORY_KEYWORDS = {
    "Tech":     ["tech", "technik", "software", "hardware", "gadget", "digital", "science",
                 "computer", "cyber", "developer", "linux", "smartphone", "it-", "ai "],
    "Sport":    ["sport", "football", "fußball", "soccer", "tennis", "nba", "f1", "olympic"],
    "Business": ["business", "wirtschaft", "finance", "finanz", "market", "börse", "economy",
                 "stock", "geld", "money"],
    "Politics": ["politic", "politik", "election", "wahl", "government", "regierung", "parlament"],
}

# ------------------------------------------------------------------ flask ---
app = Flask(__name__)
# No app.secret_key and no session: the app keeps no state between requests,
# so there is nothing for one to sign. The gate in front of it is nginx's.

HTTP = requests.Session()
HTTP.headers["User-Agent"] = "wrss/1.0 (+RSS reader)"

# Article pages get a separate session because publishers that happily serve an
# RSS feed to a plain agent will refuse the same agent's article fetch. The UA is
# the browser-compatible form rather than a Chrome impersonation: measured over
# 12 publishers, both extracted the body 9 times out of 12, so pretending to be
# Chrome buys nothing and hides who is making the request.
ARTICLE_HTTP = requests.Session()
ARTICLE_HTTP.headers["User-Agent"] = (
    "Mozilla/5.0 (compatible; wrss/1.0; +https://rss.william.hr/)")
ARTICLE_HTTP.headers["Accept"] = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"


# ProxyFix stays: the app builds absolute URLs from Host and X-Forwarded-Proto
# (the RSS <link> and atom:self above), so it has to believe nginx's headers.
# PrefixMiddleware is gone, because the app is no longer mounted under a path.
# Nothing else here depended on it -- with no prefix, every route is already at
# the root of PATH_INFO.
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

REFRESH_LOCK = threading.Lock()
# Held for the duration of a translate_many() so the worker and the one-shot
# "translate a newly added feed" path cannot run overlapping drains against the
# same budget.
TRANSLATE_LOCK = threading.Lock()
LAST_CYCLE = 0.0


@contextmanager
def db():
    con = sqlite3.connect(DB_PATH, timeout=20)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    try:
        yield con
        con.commit()
    finally:
        con.close()


# ------------------------------------------------------------------ settings ---
# Read on every refresh cycle and every worker tick, so get_setting() caches in
# process and set_setting() updates that cache in place rather than re-reading
# the row. Single-process service, so there is no second writer to fall out of
# step with.
_SETTINGS_CACHE = None


def all_settings():
    global _SETTINGS_CACHE
    with db() as con:
        found = {r["key"]: r["value"]
                 for r in con.execute("SELECT key,value FROM settings")}
    merged = dict(SETTINGS_DEFAULTS)
    merged.update(found)
    _SETTINGS_CACHE = merged
    return merged


def get_setting(key, default=None):
    if _SETTINGS_CACHE is None:
        all_settings()
    if default is None:
        default = SETTINGS_DEFAULTS.get(key, "")
    return _SETTINGS_CACHE.get(key, default)


def get_setting_int(key, default=0):
    try:
        return int(get_setting(key, str(default)))
    except (TypeError, ValueError):
        return default


def set_setting(key, value):
    with db() as con:
        con.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)",
                    (key, str(value)))
    if _SETTINGS_CACHE is not None:
        _SETTINGS_CACHE[key] = str(value)


def group_rules_map():
    """{(axis, name): (pull, translate)} for every explicit rule.

    Only overrides are stored, so a missing key means the group is fully
    enabled -- which is what lets a section that appears in the feeds table
    tomorrow work without a migration or a seeding step.
    """
    with db() as con:
        rows = con.execute("SELECT axis,name,pull,translate FROM group_rules").fetchall()
    return {(r["axis"], r["name"]): (bool(r["pull"]), bool(r["translate"])) for r in rows}


def group_allowed(row, rules, axis, what):
    """True when this feed's section/country group allows `what`.

    `rules` is a group_rules_map() built once by the caller. It is a parameter
    rather than an internal lookup because this runs per feed on every refresh
    cycle, and one query per feed turns a cheap check into a hundred of them.
    """
    pull_ok, translate_ok = rules.get((axis, row[axis]), (True, True))
    return pull_ok if what == "pull" else translate_ok


def strip_html(text):
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


# --------------------------------------------------------- images & html ---
# Feeds carry their lead image in four different places, and which one is used
# is a property of the publisher, not of the format: media:thumbnail (BBC,
# Japan Times), media:content (Guardian), <enclosure> (24sata), or nothing but
# an <img> inside the content HTML (heise, tagesschau, index.hr). Anything that
# only keeps the stripped-text summary throws all four away, which is why reader
# apps showed bare headlines with no pictures.

_IMG_RE = re.compile(r"""<img[^>]+src\s*=\s*["']?([^"'\s>]+)""", re.I)
_DROP_TAGS = {"script", "style", "iframe", "noscript", "object", "embed",
              "form", "base", "meta", "svg", "math", "template"}
_URL_ATTRS = ("src", "href", "poster", "cite", "data-src", "data-original")
# The cleaned HTML is stored and the /feed XML output hands it to whatever
# reader consumes it, so URL schemes are allowlisted: the UI never renders
# this as HTML, but javascript:/vbscript: hrefs would ride the XML verbatim
# (audit finding, 2026-11-03). Relative URLs with no base were already
# broken in feeds; they now drop too.
_ALLOWED_SCHEMES = {"http", "https", "mailto"}


def _abs_url(url, base=""):
    """Resolve a feed-supplied URL against the article link."""
    if not url:
        return ""
    u = url.strip()
    if not u or u.lower().startswith("data:"):
        return ""                      # inline data URIs are trackers or bloat
    if u.startswith("//"):
        u = "https:" + u
    out = urljoin(base, u) if base else u
    if urlparse(out).scheme.lower() not in _ALLOWED_SCHEMES:
        return ""
    return out


def entry_image(entry, base=""):
    """Best available lead image for a feed entry, or "" if it has none."""
    for thumb in entry.get("media_thumbnail") or []:
        url = _abs_url(thumb.get("url", ""), base)
        if url:
            return url
    for media in entry.get("media_content") or []:
        url = _abs_url(media.get("url", ""), base)
        if url:
            return url
    for enc in entry.get("enclosures") or []:
        if (enc.get("type") or "").lower().startswith("image/"):
            url = _abs_url(enc.get("href", ""), base)
            if url:
                return url
    blobs = [c.get("value", "") for c in (entry.get("content") or [])]
    blobs.append(entry.get("summary", "") or "")
    for blob in blobs:
        found = _IMG_RE.search(blob or "")
        if found:
            url = _abs_url(found.group(1), base)
            if url:
                return url
    return ""


def guess_image_type(url):
    path = urlparse(url).path.lower()
    for ext, mime in IMG_EXT_TYPES.items():
        if path.endswith(ext):
            return mime
    return "image/jpeg"


def entry_content_html(entry, base=""):
    """Cleaned article HTML: <content> when the feed has it, else <summary>."""
    for part in entry.get("content") or []:
        value = part.get("value") or ""
        if value.strip():
            return clean_html(value, base)
    return clean_html(entry.get("summary", "") or "", base)


class _HTMLCleaner(HTMLParser):
    """Rewrites feed HTML for safe use in a reader app: drops scripting and
    embedding tags and on* handlers, makes URLs absolute, caps the length."""

    def __init__(self, base="", limit=MAX_CONTENT_CHARS):
        super().__init__(convert_charrefs=True)
        self.base = base
        self.limit = limit
        self.parts = []
        self.size = 0
        self.drop = 0                  # nesting depth inside a dropped tag

    def _room(self, n=1):
        if self.size >= self.limit:
            return False
        self.size += n
        return True

    def _attrs(self, tag, attrs):
        out = []
        has_src = False
        for key, val in attrs:
            if key.startswith("on") or key in ("srcset", "formaction"):
                continue              # event handlers and multi-src candidates
            if val is None:
                out.append(key)
                continue
            if key in _URL_ATTRS:
                val = _abs_url(val, self.base)
                if not val:
                    continue
                has_src = has_src or key == "src"
            out.append('%s="%s"' % (key, html.escape(val, quote=True)))
        if tag == "img" and not has_src:
            return None                # e.g. a data: URI we refuse to carry
        return " ".join(out)

    def handle_starttag(self, tag, attrs):
        if tag in _DROP_TAGS:
            self.drop += 1
            return
        if self.drop or not self._room():
            return
        rendered = self._attrs(tag, attrs)
        if rendered is None:
            return
        self.parts.append("<%s%s>" % (tag, (" " + rendered) if rendered else ""))

    def handle_startendtag(self, tag, attrs):
        if tag in _DROP_TAGS or self.drop or not self._room():
            return
        rendered = self._attrs(tag, attrs)
        if rendered is None:
            return
        self.parts.append("<%s%s/>" % (tag, (" " + rendered) if rendered else ""))

    def handle_endtag(self, tag):
        if tag in _DROP_TAGS:
            self.drop = max(0, self.drop - 1)
            return
        if self.drop or not self._room():
            return
        self.parts.append("</%s>" % tag)

    def handle_data(self, data):
        # A single text node can be the whole document, so clamp it to whatever
        # room is left rather than just refusing it.
        if self.drop or self.size >= self.limit:
            return
        data = data[:self.limit - self.size]
        self.size += len(data)
        self.parts.append(html.escape(data))

    def result(self):
        return "".join(self.parts)


def clean_html(raw, base=""):
    if not raw:
        return ""
    parser = _HTMLCleaner(base or "")
    try:
        parser.feed(raw)
        parser.close()
    except Exception as exc:
        log.debug("html clean failed, falling back to text: %s", exc)
        return strip_html(raw)
    return parser.result()


# ------------------------------------------------------- reading articles ---
def google_translate_url(link, country=""):
    """Google Translate proxy URL for an article, or "" if there is no link.

    Free and instant, and it is the primary path: the headline is already
    translated, and this gets the body. Its limit is that a publisher which
    bot-walls its own article pages also walls this -- which is why the
    on-demand LLM translation below exists as a second way in.
    """
    if not link:
        return ""
    return "https://translate.google.com/translate?sl=%s&tl=en&u=%s" % (
        COUNTRY_LANG.get(country or "", "auto"), quote(link, safe=""))


def fetch_article(url):
    """Download one article page, re-validating every redirect hop.

    Same SSRF posture as fetch_url() and for the same reason: this takes a URL
    out of the database and fetches it server-side, so a poisoned article link
    would otherwise be a way to reach loopback, the private network or the cloud
    metadata address. validate_feed_url is reused rather than reimplemented.
    """
    target = validate_feed_url(url)
    for _ in range(MAX_REDIRECTS + 1):
        r = ARTICLE_HTTP.get(target, timeout=ARTICLE_FETCH_TIMEOUT, allow_redirects=False)
        if r.is_redirect or r.is_permanent_redirect:
            loc = r.headers.get("Location", "")
            if not loc:
                break
            target = validate_feed_url(urljoin(target, loc))
            continue
        break
    else:
        raise ValueError("Too many redirects.")
    r.raise_for_status()
    return r.text


def extract_article_text(html_text):
    """Article body as plain text, or "" if the page has no extractable body.

    Extraction failures are the expected case, not the exceptional one: a
    decent share of publishers serve the reader a JavaScript wall or a consent
    interstitial to exactly this kind of client. Those pages have no body to
    find, and the caller falls back to the Google Translate link.
    """
    extractor = _get_trafilatura()
    if not extractor:
        return ""
    try:
        text = extractor.extract(html_text, include_comments=False, include_tables=False)
    except Exception as exc:
        log.debug("article extraction failed: %s", exc)
        return ""
    text = (text or "").strip()
    if len(text) < ARTICLE_EXTRACT_MIN:
        return ""
    return text[:ARTICLE_EXTRACT_CHARS]


def fetch_and_extract_article(url):
    """Full pipeline for one article: guarded fetch, then body extraction."""
    return extract_article_text(fetch_article(url))


# ------------------------------------------------------------------- init ---
def init_db():
    with db() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS feeds(
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT NOT NULL,
            url         TEXT NOT NULL UNIQUE,
            kind        TEXT NOT NULL DEFAULT 'native',
            category    TEXT NOT NULL DEFAULT 'News',
            active      INTEGER NOT NULL DEFAULT 1,
            last_fetch  INTEGER DEFAULT 0,
            last_status TEXT DEFAULT '',
            created_at  INTEGER
        );
        CREATE TABLE IF NOT EXISTS articles(
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            feed_id   INTEGER NOT NULL REFERENCES feeds(id) ON DELETE CASCADE,
            guid      TEXT NOT NULL,
            title     TEXT NOT NULL,
            title_en  TEXT,
            link      TEXT,
            summary   TEXT,
            author    TEXT,
            published INTEGER DEFAULT 0,
            fetched_at INTEGER,
            read      INTEGER DEFAULT 0,
            image      TEXT,
            content_html TEXT,
            UNIQUE(feed_id, guid)
        );
        CREATE TABLE IF NOT EXISTS translations(
            hash       TEXT PRIMARY KEY,
            translated TEXT
        );
        -- Runtime-editable configuration. wrss used to have none at all: every
        -- knob was a constant at the top of this file, so changing one meant a
        -- code edit and a restart. The admin panel writes here instead.
        CREATE TABLE IF NOT EXISTS settings(
            key   TEXT PRIMARY KEY,
            value TEXT
        );
        -- Per-group on/off switches, on the two axes the sidebar actually
        -- browses by. Rows are overrides only -- a group with no row is enabled.
        -- That is deliberate: seeding one row per group on every boot is the
        -- trap SEED_FEEDS already fell into, where deleting a row in the UI just
        -- resurrects it on the next restart (see the seed loop below).
        CREATE TABLE IF NOT EXISTS group_rules(
            axis      TEXT NOT NULL,       -- 'region' | 'country'
            name      TEXT NOT NULL,       -- '' is the legitimate "no section" group
            pull      INTEGER NOT NULL DEFAULT 1,
            translate INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY(axis, name)
        );
        -- Provider call counter, one row per UTC day. OpenRouter resets at
        -- 00:00 UTC, so the day boundary here is UTC rather than local.
        CREATE TABLE IF NOT EXISTS translate_quota(
            day   TEXT PRIMARY KEY,
            calls INTEGER NOT NULL DEFAULT 0
        );
        -- Per-lane token usage per UTC day, for the spend display. Written
        -- best-effort from each response's usage block; a provider that omits
        -- it leaves zeros, which the UI shows as "n/a" rather than $0.
        CREATE TABLE IF NOT EXISTS translate_usage(
            day               TEXT NOT NULL,
            lane              TEXT NOT NULL,
            calls             INTEGER NOT NULL DEFAULT 0,
            prompt_tokens     INTEGER NOT NULL DEFAULT 0,
            completion_tokens INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(day, lane)
        );
        CREATE TABLE IF NOT EXISTS article_translations(
            article_id INTEGER PRIMARY KEY REFERENCES articles(id) ON DELETE CASCADE,
            lang       TEXT NOT NULL,
            text       TEXT NOT NULL,
            created_at INTEGER
        );
        CREATE INDEX IF NOT EXISTS idx_articles_pub ON articles(published DESC);
        CREATE INDEX IF NOT EXISTS idx_articles_feed ON articles(feed_id);
        """)
        # CREATE TABLE IF NOT EXISTS is a no-op on an existing database, so
        # columns added after the first release have to be backfilled here.
        have = {r["name"] for r in con.execute("PRAGMA table_info(articles)")}
        for column in ("image", "content_html"):
            if column not in have:
                con.execute(f"ALTER TABLE articles ADD COLUMN {column} TEXT")
                log.info("migrated articles.%s into existing database", column)
        have_feeds = {r["name"] for r in con.execute("PRAGMA table_info(feeds)")}
        for column in ("region", "country"):
            if column not in have_feeds:
                con.execute(f"ALTER TABLE feeds ADD COLUMN {column} TEXT NOT NULL DEFAULT ''")
                log.info("migrated feeds.%s into existing database", column)

        now = int(time.time())
        if con.execute("SELECT COUNT(*) c FROM feeds").fetchone()["c"] == 0:
            con.executemany(
                "INSERT INTO feeds(name,url,kind,category,region,country,created_at)"
                " VALUES(?,?,?,?,?,?,?)",
                [(n, u, k, c, r, co, now) for n, u, k, c, r, co in SEED_FEEDS])
            log.info("seeded %d feeds", len(SEED_FEEDS))
            return

        # Existing install: add any feed the seed list has grown since, and
        # backfill section/country onto the rows that were already there. Both
        # are keyed on the url UNIQUE index, so running this every boot is
        # harmless -- an existing row is left completely alone.
        added = 0
        for n, u, k, c, r, co in SEED_FEEDS:
            cur = con.execute(
                "INSERT OR IGNORE INTO feeds(name,url,kind,category,region,country,created_at)"
                " VALUES(?,?,?,?,?,?,?)", (n, u, k, c, r, co, now))
            added += cur.rowcount
        backfilled = 0
        for n, u, k, c, r, co in SEED_FEEDS:
            if not (r or co):
                continue
            cur = con.execute(
                "UPDATE feeds SET region=?, country=? WHERE url=? AND region='' AND country=''",
                (r, co, u))
            backfilled += cur.rowcount
        if added or backfilled:
            log.info("feeds: +%d added, %d backfilled with region/country", added, backfilled)


def guess_category(name, feed_title, entry_titles):
    blob = " ".join([name or "", feed_title or ""] + (entry_titles or [])).lower()
    for cat, words in CATEGORY_KEYWORDS.items():
        if any(w in blob for w in words):
            return cat
    return "News"


# ------------------------------------------------------------- translator ---
class Translator:
    def __init__(self, lane="ongoing"):
        # max_retries=0 is load-bearing, not a style choice. The SDK's default is
        # 2, so every call was being attempted three times with backoff before it
        # raised -- invisible in the code and pure waste against a provider that
        # is already rate-limiting us. Retry policy belongs to the caller here,
        # and the caller is the worker below, which knows the quota.
        #
        # lane selects the credential pair: "ongoing" for the steady trickle,
        # "batch" for the forced drain. Two lanes so a cheap batch key/model
        # can drain the backlog without touching the ongoing lane's budget or
        # quota, and each lane cools down independently when its provider
        # rate-limits it.
        self.lane = lane
        if lane == "batch" and XAI_API_KEY_BATCH:
            self.client = OpenAI(api_key=XAI_API_KEY_BATCH,
                                 base_url=XAI_BASE_URL_BATCH or XAI_BASE_URL,
                                 max_retries=0)
        else:
            self.client = OpenAI(api_key=XAI_API_KEY, base_url=XAI_BASE_URL, max_retries=0)
        self.ok = True
        self.last_error = None
        # Wall-clock instant before which no further calls are attempted, set
        # from the provider's own reset header when it rate-limits us.
        self.cooldown_until = 0.0
        self.calls_today = 0
        self.on_call = None      # set to a callable the worker uses to count calls

    @staticmethod
    def _hash(text):
        return hashlib.sha1(text.strip().encode("utf-8")).hexdigest()

    def _cache_get(self, texts):
        out = {}
        with db() as con:
            for t in texts:
                h = self._hash(t)
                row = con.execute("SELECT translated FROM translations WHERE hash=?", (h,)).fetchone()
                if row:
                    out[h] = row["translated"]
        return out

    def _cache_put(self, pairs):
        with db() as con:
            con.executemany("INSERT OR REPLACE INTO translations(hash,translated) VALUES(?,?)",
                            [(self._hash(o), t) for o, t in pairs])

    def _note_call(self):
        if self.on_call:
            try:
                self.on_call()
            except Exception as exc:                  # never let accounting break translation
                log.warning("call accounting failed: %s", exc)

    def _register_rate_limit(self, exc):
        """Record a cooldown from the 429's own headers, if it sent usable ones.

        Two spellings matter: llmproxy sends Retry-After (a delay in seconds)
        on every rate-limit reply it generates, and OpenRouter sends
        X-RateLimit-Reset as an epoch, ms or s. The fixed hour this used to
        fall back to was sized for the free tier's daily quota; paid providers
        limit per minute, so the no-header fallback is 60s now. Still bounded
        by COOLDOWN_MAX so a garbage header cannot park translation for a week.
        """
        headers = getattr(getattr(exc, "response", None), "headers", None) or {}
        wait = None
        try:
            raw = headers.get("retry-after")            # httpx: case-insensitive
            if raw:
                wait = max(1, int(float(raw)))
        except (TypeError, ValueError):
            wait = None
        if wait is None:
            try:
                raw = headers.get("x-ratelimit-reset")
                if raw:
                    reset = float(raw)
                    if reset > 1e11:      # epoch milliseconds
                        reset /= 1000.0
                    if reset > 1e9:       # epoch seconds: an instant, not a delay
                        wait = max(1, int(reset - time.time()))
                    else:                 # small value: a delay in seconds
                        wait = max(1, int(reset))
            except (TypeError, ValueError):
                wait = None
        until = time.time() + (wait if wait and wait > 0 else 60)
        self.cooldown_until = min(until, time.time() + COOLDOWN_MAX)
        mins = max(1, int((self.cooldown_until - time.time()) / 60))
        log.warning("provider rate limit hit; pausing translation for %d min", mins)

    def _model(self):
        """Model for this lane: setting when present, ongoing default otherwise."""
        key = "batch_model" if self.lane == "batch" else "ongoing_model"
        return get_setting(key, "wrss/news") or "wrss/news"

    def _create(self, messages, max_tokens):
        """One API call. Returns the visible translated text (or None if empty/truncated)."""
        self._note_call()
        try:
            resp = self.client.chat.completions.create(
                model=self._model(),
                messages=messages,
                max_tokens=max_tokens,
                timeout=120,
                extra_body={"reasoning": {"exclude": True}},
            )
        except RateLimitError as exc:
            self.ok = False
            self.last_error = str(exc)[:200]
            self._register_rate_limit(exc)
            raise
        except APIStatusError as exc:
            self.ok = False
            self.last_error = str(exc)[:200]
            raise
        _record_usage(self.lane, resp)
        return (resp.choices[0].message.content or "").strip()

    def _call(self, texts, max_tokens):
        numbered = "\n".join(f"{i+1}. {t}" for i, t in enumerate(texts))
        return self._create([
            {"role": "system",
             "content": f"You are a direct translator. Translate each numbered news headline "
                        f"into {TRANSLATE_LANG}. Respond ONLY with the same numbered list, "
                        f"one translated headline per line, nothing else."},
            {"role": "user", "content": numbered},
        ], max_tokens)

    def translate_many(self, texts, chunk_delay=0.0):
        """Translate a list of headlines; returns list with None for failures.

        chunk_delay paces consecutive provider chunks (seconds, slept after
        each chunk except the last). The auto-translate page run passes a few
        seconds so a 50-headline page spreads its ~3 calls instead of bursting
        against a free-tier per-minute limit; every other caller leaves the
        default 0 and is unaffected.
        """
        results = [None] * len(texts)
        if not texts:
            return results
        cached = self._cache_get(texts)
        todo = []
        for i, t in enumerate(texts):
            h = self._hash(t)
            if h in cached:
                results[i] = cached[h]
            else:
                todo.append((i, t))
        if not todo:
            return results

        # Dedup identical titles before calling: one wire story often lands on
        # several feeds, and translating it once then fanning out saves a call
        # per duplicate instead of paying once per article row.
        uniq_texts = []
        uniq_members = []            # uniq position -> [todo positions]
        uniq_by_hash = {}
        for pos, (_, text) in enumerate(todo):
            h = self._hash(text)
            if h in uniq_by_hash:
                uniq_members[uniq_by_hash[h]].append(pos)
            else:
                uniq_by_hash[h] = len(uniq_texts)
                uniq_texts.append(text)
                uniq_members.append([pos])

        for start in range(0, len(uniq_texts), TRANSLATE_BATCH):
            chunk_pos = list(range(start, min(start + TRANSLATE_BATCH, len(uniq_texts))))
            chunk_texts = [uniq_texts[p] for p in chunk_pos]
            if chunk_delay > 0 and start > 0:
                # Paced path only (auto-translate page run). Slept before the
                # call rather than after, so a run cancelled mid-way has already
                # waited rather than having just spent.
                time.sleep(chunk_delay)
            try:
                reply = self._call(chunk_texts, max_tokens=min(
                    MAX_TOKENS_PER_HEADLINE * len(chunk_texts),
                    MAX_TOKENS_PER_HEADLINE * TRANSLATE_BATCH))
                got = {}
                for line in reply.splitlines():
                    m = re.match(r"^\s*(\d+)[.):]\s+(.+)$", line.strip())
                    if m:
                        got[int(m.group(1))] = m.group(2).strip()
                missing = []
                for j, upos in enumerate(chunk_pos):
                    if j + 1 in got and got[j + 1]:
                        for pos in uniq_members[upos]:
                            results[todo[pos][0]] = got[j + 1]
                    else:
                        missing.append(upos)
                self.ok = True
                self.last_error = None
            except RateLimitError as exc:               # quota exhausted: stop
                self.ok = False
                self.last_error = str(exc)[:200]
                log.warning("translation batch rate limited: %s", str(exc)[:120])
                return results                           # no per-item fallback, ever
            except Exception as e:                      # API down / other error
                self.ok = False
                self.last_error = str(e)[:200]
                log.warning("translation batch failed: %s", e)
                return results                           # same: do not amplify
            # Per-item fallback runs ONLY for lines the model failed to return,
            # never because the call itself failed. Conflating those two was what
            # turned one exhausted quota into 8x the calls in the same cycle.
            for upos in missing:
                try:
                    single = self._create([
                        {"role": "system",
                         "content": f"You are a direct translator. Translate the given text into "
                                    f"{TRANSLATE_LANG}. Respond ONLY with the translated text, nothing else."},
                        {"role": "user", "content": uniq_texts[upos]},
                    ], max_tokens=MAX_TOKENS_PER_HEADLINE)
                    if single:
                        for pos in uniq_members[upos]:
                            results[todo[pos][0]] = single
                    self.ok = True
                    self.last_error = None
                except Exception as e:
                    self.ok = False
                    self.last_error = str(e)[:200]
                    return results
            done = [(texts[i], results[i]) for i, _ in todo if results[i]]
            if done:
                self._cache_put(done)
        return results

    def translate_article(self, text, title=""):
        """Translate one article body to TRANSLATE_LANG. Returns text or None.

        Deliberately separate from translate_many(): headlines are short,
        numbered, and batched two dozen at a time, while this is one long
        document that must not be split or merged with anything. It runs only
        when a reader asks for a specific article, and the caller caches the
        result.
        """
        if not text:
            return None
        header = f"Headline: {title}\n\n" if title else ""
        # Output is roughly the input length in tokens (~4 chars/token), so
        # half the char count leaves 2x headroom; the ceiling stops one long
        # article from eating the budget and the floor leaves room for short
        # ones to come back complete.
        return self._create([
            {"role": "system",
             "content": f"You are a professional news translator. Translate the article "
                        f"below into {TRANSLATE_LANG}. Keep the structure: render paragraphs "
                        f"separated by blank lines, keep headlines as standalone lines, and "
                        f"preserve every name, number and date exactly. Output only the "
                        f"translation -- no preamble, no notes, no markdown fences."},
            {"role": "user", "content": header + text},
        ], max_tokens=min(3000, max(800, int(len(text) * 0.5))))


translator = Translator("ongoing")
translator_batch = Translator("batch")


def _record_usage(lane, resp):
    """Store prompt/completion tokens per lane per UTC day for spend display.

    Best-effort: some providers omit usage, and accounting must never break
    translation, so anything missing records as zero.
    """
    try:
        usage = getattr(resp, "usage", None)
        prompt = int(getattr(usage, "prompt_tokens", 0) or 0)
        completion = int(getattr(usage, "completion_tokens", 0) or 0)
    except (TypeError, ValueError):
        prompt, completion = 0, 0
    try:
        day = quota_day()
        with db() as con:
            con.execute("INSERT OR IGNORE INTO translate_usage(day,lane,calls,prompt_tokens,completion_tokens)"
                        " VALUES(?,?,0,0,0)", (day, lane))
            con.execute("UPDATE translate_usage SET calls=calls+1, prompt_tokens=prompt_tokens+?,"
                        " completion_tokens=completion_tokens+? WHERE day=? AND lane=?",
                        (prompt, completion, day, lane))
    except Exception as exc:
        log.warning("usage accounting failed: %s", exc)


def _count_call():
    """One row per UTC day in translate_quota, upserted on every provider call.

    Counts attempts rather than successes deliberately: a rate-limited call still
    costs quota, and a budget that only counted successes would keep spending
    into a wall it cannot see.
    """
    day = quota_day()
    with db() as con:
        con.execute("INSERT OR IGNORE INTO translate_quota(day,calls) VALUES(?,0)", (day,))
        con.execute("UPDATE translate_quota SET calls=calls+1 WHERE day=?", (day,))
        row = con.execute("SELECT calls FROM translate_quota WHERE day=?", (day,)).fetchone()
    translator.calls_today = row["calls"]


def _count_call_batch():
    """Same quota counting for the batch lane, kept separate per lane."""
    day = quota_day()
    with db() as con:
        con.execute("INSERT OR IGNORE INTO translate_quota(day,calls) VALUES(?,0)", (day,))
        con.execute("UPDATE translate_quota SET calls=calls+1 WHERE day=?", (day,))
        row = con.execute("SELECT calls FROM translate_quota WHERE day=?", (day,)).fetchone()
    translator_batch.calls_today = row["calls"]


translator.on_call = _count_call
translator_batch.on_call = _count_call_batch

# --------------------------------------------------------------- fetching ---
MAX_REDIRECTS = 5


def _is_public_ip(ip) -> bool:
    return not (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified)


def validate_feed_url(url: str) -> str:
    """Reject anything that is not a plain, publicly-resolving http(s) URL.

    Feed URLs are fetched server-side, so without this an authenticated user
    could point a feed at 127.0.0.1, 10.x, 192.168.x or 169.254.169.254
    (cloud instance metadata) and use this box as a proxy into the network.
    Raises ValueError with a user-facing reason.
    """
    p = urlparse(url)
    if p.scheme not in ("http", "https"):
        raise ValueError("Only http:// and https:// URLs are allowed.")
    if p.username or p.password:
        raise ValueError("URLs with embedded credentials are not allowed.")
    host = p.hostname
    if not host:
        raise ValueError("URL is missing a host.")
    port = p.port or (443 if p.scheme == "https" else 80)
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise ValueError(f"Cannot resolve host '{host}'.") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not _is_public_ip(ip):
            raise ValueError(
                f"Host '{host}' resolves to a non-public address ({ip}). "
                f"Only public internet addresses are allowed."
            )
    return url


def fetch_url(url):
    """Fetch a feed, re-validating every redirect hop.

    requests follows redirects automatically, which would let a public URL
    bounce to 169.254.169.254 and bypass a check done only on the first URL.
    """
    target = validate_feed_url(url)
    for _ in range(MAX_REDIRECTS + 1):
        r = HTTP.get(target, timeout=20, allow_redirects=False)
        if r.is_redirect or r.is_permanent_redirect:
            loc = r.headers.get("Location", "")
            if not loc:
                break
            target = validate_feed_url(urljoin(target, loc))
            continue
        break
    else:
        raise ValueError("Too many redirects.")
    r.raise_for_status()
    return feedparser.parse(r.content)


def refresh_feed(feed):
    parsed = fetch_url(feed["url"])
    entries = parsed.entries[:ENTRIES_PER_FEED]
    new_count = 0
    filled = 0
    feed_title = parsed.feed.get("title", "")
    with db() as con:
        for e in entries:
            guid = e.get("id") or e.get("link")
            if not guid:
                continue
            link = e.get("link", "")
            existing = con.execute("SELECT id,image,content_html FROM articles "
                                   "WHERE feed_id=? AND guid=?",
                                   (feed["id"], guid)).fetchone()
            if existing is not None:
                # Stored by an older build that ran the summary through
                # strip_html(): recover the markup while the feed still serves
                # the item. This is also the normal path for repeat fetches.
                tops = {}
                if not existing["image"]:
                    tops["image"] = entry_image(e, link)
                if not existing["content_html"]:
                    tops["content_html"] = entry_content_html(e, link)
                tops = {k: v for k, v in tops.items() if v}
                if tops:
                    con.execute("UPDATE articles SET %s WHERE id=?" %
                                ",".join(f"{k}=?" for k in tops),
                                list(tops.values()) + [existing["id"]])
                    filled += 1
                continue
            title = strip_html(e.get("title", "")) or "(untitled)"
            ts = e.get("published_parsed") or e.get("updated_parsed")
            ts = calendar.timegm(ts) if ts else int(time.time())
            con.execute(
                "INSERT INTO articles(feed_id,guid,title,link,summary,author,published,fetched_at,"
                "image,content_html) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (feed["id"], guid, title, link,
                 strip_html(e.get("summary", ""))[:500], strip_html(e.get("author", "")) or None,
                 ts, int(time.time()),
                 entry_image(e, link) or None, entry_content_html(e, link) or None))
            new_count += 1
        status = f"OK · {len(entries)} items · {new_count} new"
        if filled:
            status += f" · {filled} enriched"
        con.execute("UPDATE feeds SET last_fetch=?, last_status=? WHERE id=?",
                    (int(time.time()), status, feed["id"]))
    # auto-category for freshly added feeds that have no real category yet
    if feed["category"] in ("", "News"):
        titles = [strip_html(e.get("title", "")) for e in entries[:10]]
        cat = guess_category(feed["name"], feed_title, titles)
        if cat != "News":
            with db() as con:
                con.execute("UPDATE feeds SET category=? WHERE id=?", (cat, feed["id"]))
    return new_count


def disabled_group_names(axis):
    """Names disabled for `axis`, as a list ready to drop into a NOT IN clause.

    '' is a real member of these lists: it is the "no section" group, and it is
    what lets an admin switch off ungrouped feeds without a special case in SQL.
    """
    return [name for (ax, name), (pull_ok, translate_ok) in group_rules_map().items()
            if ax == axis and not translate_ok]


def translate_pending(limit=None, lane="ongoing"):
    """Translate up to `limit` pending foreign headlines. Returns the count done.

    Runs through translate_many() with the silent 1s chunk pace (an aggregated
    or forced drain bursts TRANSLATE_PENDING_CAP / TRANSLATE_BATCH calls, the
    exact shape that trips OpenRouter's shared upstream pool). The forced
    drain intentionally does not pass one -- the worker releases the lock
    between ticks anyway, and the shared pool is the same one every lane hits.

    The exclusions are in SQL rather than a filter over the returned rows, and
    that ordering matters. Disabled groups are never translated, so their
    headlines sit at the head of `published DESC` forever; filtering after
    LIMIT would let a disabled section permanently starve the enabled ones and
    the queue would never move.

    lane selects the credential: the steady trickle uses "ongoing", the forced
    drain uses "batch" so a cheap batch key drains the backlog without touching
    the ongoing lane's budget or quota.
    """
    cap = min(limit if limit is not None else TRANSLATE_PENDING_CAP, TRANSLATE_PENDING_CAP)
    if cap <= 0:
        return 0
    where = ["f.kind='foreign'", "a.title_en IS NULL", "f.active=1"]
    params = []
    for axis in ("region", "country"):
        off = disabled_group_names(axis)
        if off:
            where.append("f.%s NOT IN (%s)" % (axis, ",".join("?" * len(off))))
            params += off
    with db() as con:
        rows = con.execute(
            "SELECT a.id, a.title FROM articles a JOIN feeds f ON f.id=a.feed_id "
            f"WHERE {' AND '.join(where)} ORDER BY a.published DESC LIMIT ?",
            params + [cap]).fetchall()
    if not rows:
        return 0
    texts = [r["title"] for r in rows]
    tr = translator_batch if lane == "batch" else translator
    translations = tr.translate_many(texts, chunk_delay=AUTO_TRANSLATE_CHUNK_DELAY)
    done = 0
    with db() as con:
        for r, t in zip(rows, translations):
            if t:
                con.execute("UPDATE articles SET title_en=? WHERE id=?", (t, r["id"]))
                done += 1
    log.info("translated %d/%d foreign headlines", done, len(rows))
    return done


def prune():
    with db() as con:
        ids = [r["id"] for r in con.execute("SELECT id FROM feeds").fetchall()]
        for fid in ids:
            con.execute(
                "DELETE FROM articles WHERE feed_id=? AND id NOT IN "
                "(SELECT id FROM articles WHERE feed_id=? ORDER BY published DESC, id DESC LIMIT ?)",
                (fid, fid, KEEP_PER_FEED))


def remove_oldest_xx(xx):
    if xx <= 0:
        return 0
    with db() as con:
        rows = con.execute(
            "SELECT id FROM articles ORDER BY published ASC, id ASC LIMIT ?",
            (xx,)
        ).fetchall()
        ids = [r["id"] for r in rows]
        if not ids:
            return 0
        placeholders = ",".join(["?"] * len(ids))
        con.execute(
            f"DELETE FROM articles WHERE id IN ({placeholders})",
            ids
        )
        return len(ids)


def run_refresh():
    global LAST_CYCLE
    if not REFRESH_LOCK.acquire(blocking=False):
        return False
    try:
        rules = group_rules_map()
        with db() as con:
            feeds = con.execute("SELECT * FROM feeds WHERE active=1 ORDER BY id").fetchall()
        skipped = 0
        for f in feeds:
            if not (group_allowed(f, rules, "region", "pull")
                    and group_allowed(f, rules, "country", "pull")):
                skipped += 1
                continue
            try:
                n = refresh_feed(f)
                if n:
                    log.info("%s: %d new articles", f["name"], n)
            except Exception as e:
                with db() as con:
                    con.execute("UPDATE feeds SET last_fetch=?, last_status=? WHERE id=?",
                                (int(time.time()), f"ERROR: {str(e)[:150]}", f["id"]))
                log.warning("%s: fetch failed: %s", f["name"], e)
        if skipped:
            log.info("skipped %d active feeds: their section/country group is disabled", skipped)
        prune()
        LAST_CYCLE = time.time()
        return True
    finally:
        REFRESH_LOCK.release()


def fetcher_loop():
    time.sleep(2)
    while True:
        try:
            run_refresh()
        except Exception as e:
            log.error("refresh cycle failed: %s", e)
        time.sleep(REFRESH_INTERVAL)


# ------------------------------------------------------ translation worker ---
# Set by the admin panel's "translate queued now" switch. While this is True the
# worker drains the backlog regardless of the window, the daily budget and the
# master switch, which is what you want for a deliberate press against a
# backlog that piled up while translation was paused. It clears itself once
# the backlog reaches zero. The remaining safety stop is the provider's own
# rate limit, which pauses the worker via its cooldown.
TRANSLATE_NOW = threading.Event()
# Wall-clock of the last automatic (non-forced) drain; gates the
# AUTO_TRANSLATE_INTERVAL spacing in translate_tick.
_last_auto_tick = 0.0


def quota_day():
    return time.strftime("%Y-%m-%d", time.gmtime())


def calls_today():
    with db() as con:
        row = con.execute("SELECT calls FROM translate_quota WHERE day=?",
                          (quota_day(),)).fetchone()
    return row["calls"] if row else 0


def budget_left():
    return max(0, get_setting_int("translate_daily_budget", 800) - calls_today())


def in_window(now=None):
    """True when the local hour is inside the configured work window.

    A window whose end is earlier than its start wraps past midnight, which is
    the normal case for a window meant to run overnight.
    """
    start = get_setting_int("translate_window_start", 22) % 24
    end = get_setting_int("translate_window_end", 7) % 24
    hour = (now if now is not None else time.localtime()).tm_hour
    if start == end:
        return True                       # zero-length window means "always"
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


def translate_tick():
    """One worker pass. Returns how many headlines were translated.

    Split out from the loop so the decision -- should we run, and how much may
    we do this time -- can be tested without a thread and a clock. Returns 0 for
    every "nothing to do" case, which is why the forced-drain bookkeeping below
    cannot use that number alone to tell "queue is empty" from "provider said
    no".
    """
    global _last_auto_tick
    if TRANSLATE_AUTO_DISABLED:
        return 0                                    # automatic kill-switch engaged
    forced = TRANSLATE_NOW.is_set()
    if get_setting_int("translate_enabled", 1) <= 0 and not forced:
        return 0                                    # master switch off
    # Each lane cools down independently: a rate-limited batch key must not
    # park the ongoing trickle, and vice versa.
    tr = translator_batch if forced else translator
    if tr.cooldown_until > time.time():
        return 0                                    # provider rate-limited us
    if not forced and not in_window():
        return 0                                    # outside the work window
    if not forced and time.time() - _last_auto_tick < AUTO_TRANSLATE_INTERVAL:
        return 0            # aggregated automatic drain: next one not due yet

    if forced:
        # Drain hard on the batch lane, one full cycle's worth per tick so the
        # UI and fetcher stay responsive while a backlog works itself off.
        cap = TRANSLATE_PENDING_CAP
    else:
        cap = min(get_setting_int("translate_per_tick", 240),
                  TRANSLATE_PENDING_CAP, budget_left())
    if cap <= 0:
        return 0

    if not TRANSLATE_LOCK.acquire(blocking=False):
        return 0                                    # another drain is running
    try:
        done = translate_pending(cap, lane="batch" if forced else "ongoing")
        tr.calls_today = calls_today()
    finally:
        TRANSLATE_LOCK.release()

    if not forced:
        _last_auto_tick = time.time()

    if forced:
        # Stop only on a genuine finish. done==0 alone is ambiguous: it also
        # means the provider refused the batch, and cancelling a forced drain
        # because of a 429 is exactly the wrong thing to do when the reason the
        # user pressed the button was that they just fixed the quota. The daily
        # budget does not stop a forced drain either -- the button promises to
        # ignore it, the backlog bounds the spend, and a deliberate press is not
        # the unattended spending the budget exists to cap.
        refused = not tr.ok
        if done == 0 and not refused:
            log.info("forced drain finished: backlog empty")
            TRANSLATE_NOW.clear()
        else:
            log.info("forced drain: %d translated, %d calls used today",
                     done, tr.calls_today)
    return done


def translate_worker():
    """Drain the translation backlog inside the work window and daily budget.

    Replaces translating inline at the end of every fetch cycle. Two reasons:
    the window is a time-of-day notion and has to be checked more often than
    every ten minutes, and the budget has to be consulted before every call,
    which a once-per-cycle drain cannot do.
    """
    time.sleep(5)
    while True:
        try:
            translate_tick()
        except Exception as e:
            log.error("translation worker tick failed: %s", e)
        time.sleep(TRANSLATE_TICK)


# ------------------------------------------------------------------ access ---
# There is deliberately no auth code in this file. nginx gates the rss.william.hr
# vhost with an auth_request subrequest against the SSO app, so anything that
# reaches the socket -- documents, /static/*, /api/* and /feed alike -- has
# already been authenticated. Adding a second check here would only reintroduce
# the per-app secret this setup exists to avoid.


# ------------------------------------------------------------------- api ---
ARTICLE_SELECT = ("SELECT a.*, f.name feed_name, f.kind feed_kind, f.category, "
                  "f.region, f.country FROM articles a JOIN feeds f ON f.id=a.feed_id")


def article_filters(args, params_only=False):
    where, params = ["1=1"], []
    if args.get("cat"):
        where.append("f.category=?"); params.append(args["cat"])
    if args.get("region"):
        where.append("f.region=?"); params.append(args["region"])
    if args.get("country"):
        where.append("f.country=?"); params.append(args["country"])
    if args.get("kind") in ("native", "foreign"):
        where.append("f.kind=?"); params.append(args["kind"])
    if args.get("source"):
        where.append("f.name=?"); params.append(args["source"])
    if args.get("q"):
        like = f"%{args['q']}%"
        where.append("(a.title LIKE ? OR a.title_en LIKE ? OR a.summary LIKE ?)")
        params += [like, like, like]
    if args.get("unread") in ("1", "true"):
        where.append("a.read=0")
    return " AND ".join(where), params


def section_tree(con):
    """World sections and their countries, with unread/total counts.

    Drives the sidebar tree. Built from the feeds table rather than a static
    list so a feed added through the UI with a region shows up without a code
    change.
    """
    rows = con.execute(
        "SELECT f.region, f.country, COUNT(a.id) total, "
        "SUM(CASE WHEN a.read=0 THEN 1 ELSE 0 END) unread "
        "FROM feeds f LEFT JOIN articles a ON a.feed_id=f.id "
        "WHERE f.region<>'' GROUP BY f.region, f.country").fetchall()
    sections = {}
    for r in rows:
        sec = sections.setdefault(r["region"], {"name": r["region"], "total": 0,
                                                "unread": 0, "countries": []})
        sec["total"] += r["total"] or 0
        sec["unread"] += r["unread"] or 0
        if r["country"]:
            sec["countries"].append({"name": r["country"], "total": r["total"] or 0,
                                     "unread": r["unread"] or 0})
    ordered = sorted(sections.values(),
                     key=lambda s: (SECTION_ORDER.index(s["name"])
                                    if s["name"] in SECTION_ORDER else len(SECTION_ORDER),
                                    s["name"]))
    for s in ordered:
        s["countries"].sort(key=lambda c: (SECTION_ORDER and 0, c["name"]))
    return ordered


@app.route("/")
def index():
    return render_template("index.html", prefix=PREFIX)


@app.route("/api/bootstrap")
def api_bootstrap():
    with db() as con:
        cats = con.execute(
            "SELECT f.category name, COUNT(a.id) total, "
            "SUM(CASE WHEN a.read=0 THEN 1 ELSE 0 END) unread "
            "FROM feeds f LEFT JOIN articles a ON a.feed_id=f.id "
            "GROUP BY f.category ORDER BY f.category").fetchall()
        kinds = {"all": {"total": 0, "unread": 0}, "native": {"total": 0, "unread": 0},
                 "foreign": {"total": 0, "unread": 0}}
        for r in con.execute(
                "SELECT f.kind, COUNT(a.id) total, "
                "SUM(CASE WHEN a.read=0 THEN 1 ELSE 0 END) unread "
                "FROM feeds f LEFT JOIN articles a ON a.feed_id=f.id GROUP BY f.kind"):
            kinds[r["kind"]] = {"total": r["total"], "unread": r["unread"] or 0}
            kinds["all"]["total"] += r["total"]
            kinds["all"]["unread"] += r["unread"] or 0
        cat_names = [r["name"] for r in con.execute(
            "SELECT DISTINCT category name FROM feeds ORDER BY category")]
        sections = section_tree(con)
    return jsonify(categories=[dict(r) for r in cats], kinds=kinds,
                   cat_names=cat_names, sections=sections)


def pending_headlines():
    with db() as con:
        return con.execute(
            "SELECT COUNT(*) c FROM articles a JOIN feeds f ON f.id=a.feed_id "
            "WHERE f.kind='foreign' AND a.title_en IS NULL").fetchone()["c"]


@app.route("/api/status")
def api_status():
    with db() as con:
        total = con.execute("SELECT COUNT(*) c FROM articles").fetchone()["c"]
        unread = con.execute("SELECT COUNT(*) c FROM articles WHERE read=0").fetchone()["c"]
        last = con.execute("SELECT MAX(last_fetch) m FROM feeds").fetchone()["m"] or 0
    nxt = int(max(0, REFRESH_INTERVAL - (time.time() - LAST_CYCLE))) if LAST_CYCLE else None
    return jsonify(articles=total, unread=unread, last_refresh=last,
                   next_refresh_in=nxt, translator_ok=translator.ok,
                   translator_error=translator.last_error,
                   translate_enabled=(get_setting_int("translate_enabled", 1) > 0),
                   pending=pending_headlines(),
                   extractor_ok=_get_trafilatura() is not None)


@app.route("/api/articles")
def api_articles():
    where, params = article_filters(request.args)
    limit = min(int(request.args.get("limit", 50)), 200)
    offset = max(int(request.args.get("offset", 0)), 0)
    with db() as con:
        rows = con.execute(f"{ARTICLE_SELECT} WHERE {where} "
                           "ORDER BY a.published DESC, a.id DESC LIMIT ? OFFSET ?",
                           params + [limit, offset]).fetchall()
        total = con.execute(f"SELECT COUNT(*) c FROM articles a JOIN feeds f ON f.id=a.feed_id "
                            f"WHERE {where}", params).fetchone()["c"]
    arts = []
    for r in rows:
        d = dict(r)
        d["read"] = bool(d["read"])
        # Drives the per-headline translate button: only a foreign headline with
        # nothing in title_en yet. native articles never get a title_en, so
        # without the kind check every native card would offer a translation
        # that has no meaning.
        d["needs_translation"] = bool(d["feed_kind"] == "foreign"
                                      and not d["title_en"])
        if d["feed_kind"] == "foreign" and d["link"]:
            url = google_translate_url(d["link"], d["country"])
            if url:
                d["translate_url"] = url
        arts.append(d)
    return jsonify(articles=arts, total=total)


@app.route("/api/articles/<int:aid>/translate", methods=["POST"])
def api_article_translate(aid):
    """Translate one article body into English, on demand.

    The second of the two ways to read a foreign article. Google Translate is
    the first because it is free; this exists because a publisher that serves
    its feed happily will often serve a JavaScript wall to the article page,
    and then there is nothing for the proxy to translate either.

    Deliberately not done for every article on fetch: that would be one model
    call per foreign headline's body, every cycle, for text nobody reads. This
    runs on click and caches, so the cost tracks what people actually open.
    """
    with db() as con:
        row = con.execute(f"{ARTICLE_SELECT} WHERE a.id=?", (aid,)).fetchone()
        if row is None:
            return jsonify(error="Article not found."), 404
        cached = con.execute(
            "SELECT text FROM article_translations WHERE article_id=? AND lang=?",
            (aid, TRANSLATE_LANG)).fetchone()
    fallback = google_translate_url(row["link"], row["country"])
    if cached:
        return jsonify(ok=True, text=cached["text"], cached=True, translate_url=fallback)

    # The master switch covers this path too, not just the headline queue. This
    # is the one call a reader triggers by hand, so leaving it open would mean a
    # paused service still spends quota on the click that seemed harmless --
    # and the button is visible in the UI. Google Translate is free, so that
    # stays offered as the way to read the article.
    if get_setting_int("translate_enabled", 1) <= 0:
        return jsonify(
            error="Translation is switched off in Admin. Open this article "
                  "through Google Translate instead.",
            translate_url=fallback)

    if not row["link"]:
        return jsonify(error="This article has no link to fetch."), 400

    try:
        body = fetch_and_extract_article(row["link"])
    except ValueError as exc:
        # The SSRF guard rejected it, or the URL could not be resolved.
        log.info("article %s fetch refused: %s", aid, exc)
        return jsonify(error=f"Could not fetch this article: {exc}",
                       translate_url=fallback)
    except Exception as exc:
        log.info("article %s fetch failed: %s", aid, str(exc)[:160])
        return jsonify(error="Could not fetch this article page.",
                       translate_url=fallback)

    if not body:
        return jsonify(
            error=("This publisher does not serve the article body to a plain "
                   "request (a script wall or a paywall). Open it through "
                   "Google Translate instead."),
            via="google", translate_url=fallback)

    try:
        translated = translator.translate_article(body, row["title_en"] or row["title"])
    except Exception as exc:
        log.warning("article %s translation failed: %s", aid, str(exc)[:160])
        return jsonify(error="Translation failed. Try again in a moment.",
                       translate_url=fallback)
    if not translated:
        return jsonify(error="Translation came back empty.", translate_url=fallback)

    with db() as con:
        con.execute(
            "INSERT OR REPLACE INTO article_translations(article_id,lang,text,created_at)"
            " VALUES(?,?,?,?)", (aid, TRANSLATE_LANG, translated, int(time.time())))
    return jsonify(ok=True, text=translated, cached=False, translate_url=fallback)


@app.route("/api/articles/<int:aid>/headline", methods=["POST"])
def api_article_headline_translate(aid):
    """Translate one headline on demand, the way the reader already can bodies.

    The per-article ⇄ button is per-article by definition: nobody wants to be
    offered a translation of a body they did not ask for. A headline is
    different -- the queue is global, so one headline can look untranslated
    while the rest of its section is already done, or sit behind a disabled
    group. This is the escape hatch for "just this one".

    Deliberately NOT gated on translate_enabled. The master switch pauses the
    bulk queue because that is what spends quota unattended; a person clicking
    one headline is spending deliberately, and a pause that also blocked this
    would make the button lie. What it IS gated on is the daily budget and the
    rate-limit cooldown, so a single click can never be the thing that walks
    into an exhausted quota.
    """
    with db() as con:
        # The English headline lives in articles.title_en. article_translations
        # holds BODY translations only, so joining it here would report every
        # headline as uncached and re-spend a call on one already paid for.
        row = con.execute("SELECT a.title, a.title_en, f.kind FROM articles a "
                          "JOIN feeds f ON f.id=a.feed_id WHERE a.id=?",
                          (aid,)).fetchone()
        if row is None:
            return jsonify(error="Article not found."), 404
        if row["kind"] != "foreign":
            # Native articles never get a title_en; translating one would be
            # inventing an English headline for text that is already English.
            return jsonify(error="This is a native-language article; it has no "
                                 "headline to translate."), 400
        title_en = row["title_en"]

    if translator.cooldown_until > time.time():
        return jsonify(error="The translator is paused by a rate limit. Try later.",
                       retry_after=int(translator.cooldown_until - time.time()))

    if title_en:
        return jsonify(ok=True, title=title_en, cached=True)
    if budget_left() <= 0:
        return jsonify(error="Today's translation budget is spent. It resets at 00:00 UTC.")

    results = translator.translate_many([row["title"]])
    text = results[0] if results else None
    if not text:
        return jsonify(error="Translation came back empty. Try again.")

    with db() as con:
        con.execute("UPDATE articles SET title_en=? WHERE id=?", (text, aid))
    translator.calls_today = calls_today()
    return jsonify(ok=True, title=text, cached=False)


# Max article ids accepted per bulk translate call. The frontend sends one
# page's worth (50 list limit); 200 leaves headroom without letting a crafted
# request turn one HTTP call into an unbounded provider burst.
TRANSLATE_BATCH_IDS_MAX = 200


@app.route("/api/articles/translate-batch", methods=["POST"])
def api_articles_translate_batch():
    """Translate the headlines the reader is currently looking at, in bulk.

    The frontend sends the ids of the visible cards that still need it (one
    page, or one Load-more chunk) when its per-browser Auto-translate switch is
    on -- which is its default, so a foreign page translates itself on load.
    One HTTP request becomes ceil(n/TRANSLATE_BATCH) provider calls through the
    normal batching instead of n single-headline calls bursting at once. A
    silent 1s pause between chunks (AUTO_TRANSLATE_CHUNK_DELAY) keeps a full
    page run from bursting OpenRouter's shared upstream pool, which 429s
    under back-to-back calls even when the account has no per-minute limit.

    Deliberately NOT gated on translate_enabled, same as the single-headline
    route: a visible page is deliberate spend, not the unattended queue the
    master switch pauses. Still gated on the daily budget and the rate-limit
    cooldown. Returns proper HTTP statuses (429/409) rather than 200-with-error
    so the frontend can tell "stop" from "translated".
    """
    data = request.get_json(silent=True) or {}
    raw_ids = data.get("ids") or []
    try:
        ids = [int(i) for i in raw_ids]
    except (TypeError, ValueError):
        return jsonify(error="ids must be a list of article ids."), 400
    # Preserve page order, drop duplicates: the reply maps id -> title and the
    # frontend updates cards in place, so order stability is what keeps the
    # right translation landing on the right card.
    seen = set()
    ids = [i for i in ids if i not in seen and not seen.add(i)]
    if not ids:
        return jsonify(error="No article ids given."), 400
    if len(ids) > TRANSLATE_BATCH_IDS_MAX:
        return jsonify(error=f"At most {TRANSLATE_BATCH_IDS_MAX} ids per call."), 400

    if translator.cooldown_until > time.time():
        wait = int(translator.cooldown_until - time.time())
        resp = jsonify(error="The translator is paused by a rate limit. Try later.",
                       retry_after=wait, cooldown=True)
        resp.headers["Retry-After"] = str(wait)
        return resp, 429

    if not TRANSLATE_LOCK.acquire(blocking=False):
        # The worker or a forced drain is mid-run. 409 means "skip this
        # auto-run and leave the per-headline buttons", never queue behind it.
        return jsonify(error="A translation run is already in progress.",
                       busy=True), 409

    translated = {}
    cached = []
    skipped = []
    truncated = False
    cooldown = False
    retry_after = 0
    try:
        with db() as con:
            rows = {r["id"]: r for r in con.execute(
                "SELECT a.id, a.title, a.title_en, f.kind, f.active, f.region, f.country"
                " FROM articles a JOIN feeds f ON f.id=a.feed_id"
                f" WHERE a.id IN ({','.join('?' * len(ids))})", ids)}
        rules = group_rules_map()
        todo = []
        for aid in ids:
            r = rows.get(aid)
            if r is None:
                skipped.append(aid)
                continue
            if r["title_en"]:
                cached.append(aid)
                continue
            if r["kind"] != "foreign" or not r["active"]:
                skipped.append(aid)
                continue
            _, region_translate_ok = rules.get(("region", r["region"] or ""), (True, True))
            _, country_translate_ok = rules.get(("country", r["country"] or ""), (True, True))
            if not (region_translate_ok and country_translate_ok):
                skipped.append(aid)
                continue
            todo.append((aid, r["title"]))

        # Trim to what the remaining daily budget can actually pay for. Each
        # chunk of 8 costs one call plus headroom for per-item retries, so the
        # estimate is ceil(n/8)+1. The head of the page is translated, the rest
        # keeps its buttons and `truncated` tells the frontend there is more.
        while todo and math.ceil(len(todo) / TRANSLATE_BATCH) + 1 > budget_left():
            skipped.append(todo.pop()[0])
            truncated = True
        if todo and budget_left() <= 0:
            skipped.extend(aid for aid, _ in todo)
            todo = []
            truncated = True

        if todo:
            texts = [t for _, t in todo]
            results = translator.translate_many(texts, chunk_delay=AUTO_TRANSLATE_CHUNK_DELAY)
            with db() as con:
                for (aid, orig), text in zip(todo, results):
                    if text:
                        con.execute("UPDATE articles SET title_en=? WHERE id=?", (text, aid))
                        translated[aid] = text
                    else:
                        skipped.append(aid)
            translator.calls_today = calls_today()
            if not translator.ok:
                cooldown = translator.cooldown_until > time.time()
                retry_after = max(0, int(translator.cooldown_until - time.time()))
    finally:
        TRANSLATE_LOCK.release()

    out = {"ok": True, "translated": translated, "cached": cached,
           "skipped": skipped, "truncated": truncated, "cooldown": cooldown}
    if cooldown:
        out["retry_after"] = retry_after
        # 200, not 429: partial progress was saved and the frontend should apply
        # it rather than treat the whole run as refused.
    return jsonify(out)


@app.route("/api/articles/<int:aid>/read", methods=["POST"])
def api_article_read(aid):
    val = 1 if (request.json or {}).get("read", True) else 0
    with db() as con:
        con.execute("UPDATE articles SET read=? WHERE id=?", (val, aid))
    return jsonify(ok=True)


@app.route("/api/articles/read_all", methods=["POST"])
def api_read_all():
    where, params = article_filters(request.get_json(silent=True) or {})
    with db() as con:
        cur = con.execute(f"UPDATE articles SET read=1 WHERE id IN "
                          f"(SELECT a.id FROM articles a JOIN feeds f ON f.id=a.feed_id WHERE {where})",
                          params)
    return jsonify(ok=True, marked=cur.rowcount)


@app.route("/api/feeds", methods=["GET", "POST"])
def api_feeds():
    if request.method == "GET":
        with db() as con:
            rows = con.execute(
                "SELECT f.*, COUNT(a.id) total, "
                "SUM(CASE WHEN a.read=0 THEN 1 ELSE 0 END) unread "
                "FROM feeds f LEFT JOIN articles a ON a.feed_id=f.id "
                "GROUP BY f.id ORDER BY f.kind, f.category, f.name").fetchall()
        return jsonify(feeds=[dict(r) for r in rows])

    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    if not url.startswith(("http://", "https://")):
        return jsonify(error="A valid URL is required."), 400
    try:
        validate_feed_url(url)
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    name = (data.get("name") or "").strip() or urlparse(url).netloc.removeprefix("www.")
    kind = data["kind"] if data.get("kind") in ("native", "foreign") else "native"
    category = (data.get("category") or "").strip() or "News"
    region = (data.get("region") or "").strip()
    country = (data.get("country") or "").strip()
    with db() as con:
        if con.execute("SELECT 1 FROM feeds WHERE url=?", (url,)).fetchone():
            return jsonify(error="This feed URL already exists."), 400
        cur = con.execute(
            "INSERT INTO feeds(name,url,kind,category,region,country,active,created_at)"
            " VALUES(?,?,?,?,?,?,1,?)",
            (name, url, kind, category, region, country, int(time.time())))
        fid = cur.lastrowid
    threading.Thread(target=_fetch_single_and_categorize, args=(fid,), daemon=True).start()
    return jsonify(ok=True, id=fid), 201


def _fetch_single_and_categorize(fid):
    with db() as con:
        feed = con.execute("SELECT * FROM feeds WHERE id=?", (fid,)).fetchone()
    if not feed:
        return
    try:
        refresh_feed(feed)
    except Exception as e:
        with db() as con:
            con.execute("UPDATE feeds SET last_fetch=?, last_status=? WHERE id=?",
                        (int(time.time()), f"ERROR: {str(e)[:150]}", fid))
    if feed["kind"] == "foreign":
        # A newly added feed should not wait up to TRANSLATE_TICK for its first
        # batch, but it also must not become a second path around the budget --
        # so it takes the lock and draws from the same remaining allowance, and
        # does nothing at all while automatic translation is switched off.
        if (not TRANSLATE_AUTO_DISABLED
                and get_setting_int("translate_enabled", 1) > 0 and budget_left() > 0):
            if TRANSLATE_LOCK.acquire(blocking=False):
                try:
                    cap = min(get_setting_int("translate_per_tick", 12), budget_left())
                    if cap > 0:
                        translate_pending(cap)
                finally:
                    TRANSLATE_LOCK.release()


@app.route("/api/feeds/<int:fid>", methods=["PUT", "DELETE"])
def api_feed_edit(fid):
    if request.method == "DELETE":
        with db() as con:
            con.execute("DELETE FROM feeds WHERE id=?", (fid,))
        return jsonify(ok=True)
    data = request.get_json(silent=True) or {}
    with db() as con:
        feed = con.execute("SELECT * FROM feeds WHERE id=?", (fid,)).fetchone()
        if not feed:
            return jsonify(error="Feed not found."), 404
        name = (data.get("name") or feed["name"]).strip()
        url = (data.get("url") or feed["url"]).strip()
        if url != feed["url"]:
            try:
                validate_feed_url(url)
            except ValueError as exc:
                return jsonify(error=str(exc)), 400
        kind = data.get("kind") if data.get("kind") in ("native", "foreign") else feed["kind"]
        category = (data.get("category") or feed["category"]).strip()
    region = (data.get("region") if "region" in data else feed["region"]).strip()
    country = (data.get("country") if "country" in data else feed["country"]).strip()
    active = 1 if data.get("active", bool(feed["active"])) else 0
    if url != feed["url"]:
        con.execute("DELETE FROM articles WHERE feed_id=?", (fid,))
    con.execute(
        "UPDATE feeds SET name=?,url=?,kind=?,category=?,region=?,country=?,active=?"
        " WHERE id=?", (name, url, kind, category, region, country, active, fid))
    return jsonify(ok=True)


@app.route("/api/refresh", methods=["POST"])
def api_refresh():
    if REFRESH_LOCK.locked():
        return jsonify(ok=True, busy=True)
    threading.Thread(target=run_refresh, daemon=True).start()
    return jsonify(ok=True, busy=False)


# ------------------------------------------------------------------- admin ---
# Same authentication as everything else here: the SSO gate in front of the
# vhost. There is no separate admin role on this box, so anyone who can read the
# reader can also reach these. That is a property of the gateway, not something
# these routes can add on top of it.
def _group_list(axis):
    """Every group that exists on a feed, with its feed count, sorted by name.

    Derived from the feeds table rather than stored, so a feed added through the
    UI shows up in the admin panel without a code change -- the same reasoning as
    section_tree(). '' is a real group here: the feeds with no section set.
    """
    with db() as con:
        # "foreign" is a SQLite keyword and has to be quoted as an alias.
        rows = con.execute(
            f'SELECT {axis} name, COUNT(*) n, '
            'SUM(CASE WHEN kind=\'foreign\' THEN 1 ELSE 0 END) "foreign" '
            f"FROM feeds GROUP BY {axis} ORDER BY {axis}").fetchall()
    return [dict(r) for r in rows]


def _lane_spend(lane):
    """Tokens and estimated spend for `lane` today, from translate_usage.

    Spend is tokens times the lane's configured list price. The provider bill
    is the source of truth; this is the number to glance at before pressing
    the drain button twice.
    """
    try:
        price_in = float(get_setting(lane + "_price_in", "0") or 0)
        price_out = float(get_setting(lane + "_price_out", "0") or 0)
    except (TypeError, ValueError):
        price_in, price_out = 0.0, 0.0
    with db() as con:
        row = con.execute("SELECT calls, prompt_tokens, completion_tokens FROM translate_usage"
                          " WHERE day=? AND lane=?", (quota_day(), lane)).fetchone()
    calls = row["calls"] if row else 0
    prompt = row["prompt_tokens"] if row else 0
    completion = row["completion_tokens"] if row else 0
    tracked = bool(row)
    spend = (prompt * price_in + completion * price_out) / 1_000_000
    return {"calls": calls, "prompt_tokens": prompt, "completion_tokens": completion,
            "spend_usd": round(spend, 4), "tracked": tracked,
            "model": get_setting(lane + "_model", "wrss/news")}


@app.route("/api/admin", methods=["GET"])
def api_admin_get():
    with db() as con:
        pending = con.execute(
            "SELECT COUNT(*) c FROM articles a JOIN feeds f ON f.id=a.feed_id "
            "WHERE f.kind='foreign' AND a.title_en IS NULL").fetchone()["c"]
        translated = con.execute(
            "SELECT COUNT(*) c FROM articles a JOIN feeds f ON f.id=a.feed_id "
            "WHERE f.kind='foreign' AND a.title_en IS NOT NULL").fetchone()["c"]
        # Per-country pending breakdown
        pending_by_country = con.execute(
            "SELECT f.country, COUNT(*) c FROM articles a JOIN feeds f ON f.id=a.feed_id "
            "WHERE f.kind='foreign' AND a.title_en IS NULL GROUP BY f.country "
            "ORDER BY c DESC").fetchall()
        # Per-country translated breakdown
        translated_by_country = con.execute(
            "SELECT f.country, COUNT(*) c FROM articles a JOIN feeds f ON f.id=a.feed_id "
            "WHERE f.kind='foreign' AND a.title_en IS NOT NULL GROUP BY f.country "
            "ORDER BY c DESC").fetchall()
        # Most recent translation timestamp
        last_translated = con.execute(
            "SELECT MAX(a.id) last_id FROM articles a JOIN feeds f ON f.id=a.feed_id "
            "WHERE f.kind='foreign' AND a.title_en IS NOT NULL").fetchone()["last_id"]
    cooldown = max(0, int(translator.cooldown_until - time.time()))
    cooldown_batch = max(0, int(translator_batch.cooldown_until - time.time()))
    lanes = {"ongoing": _lane_spend("ongoing"), "batch": _lane_spend("batch")}
    lanes["ongoing"]["has_key"] = bool(XAI_API_KEY)
    lanes["batch"]["has_key"] = bool(XAI_API_KEY_BATCH)

    # Translation rate estimate: calls per minute from today's usage
    calls_today_count = calls_today()
    now_ts = time.time()
    day_start = int(time.mktime(time.strptime(time.strftime("%Y-%m-%d", time.gmtime(now_ts)), "%Y-%m-%d")))
    minutes_elapsed = max(1, (now_ts - day_start) / 60.0)
    calls_per_minute = calls_today_count / minutes_elapsed if minutes_elapsed > 0 else 0
    eta_minutes = round(pending / calls_per_minute) if calls_per_minute > 0 else None

    total_foreign = pending + translated
    pct_done = round(translated / total_foreign * 100, 1) if total_foreign > 0 else 0.0

    return jsonify(
        settings={k: get_setting(k) for k in SETTINGS_DEFAULTS},
        rules=[{"axis": a, "name": n, "pull": p, "translate": t}
               for (a, n), (p, t) in sorted(group_rules_map().items())],
        regions=_group_list("region"),
        countries=_group_list("country"),
        stats={
            "pending": pending,
            "translated": translated,
            "total_foreign": total_foreign,
            "pct_done": pct_done,
            "calls_today": calls_today_count,
            "budget": get_setting_int("translate_daily_budget", 800),
            "budget_left": max(0, get_setting_int("translate_daily_budget", 800) - calls_today_count),
            "cooldown_seconds": cooldown,
            "cooldown_batch_seconds": cooldown_batch,
            "calls_per_minute": round(calls_per_minute, 1),
            "eta_minutes": eta_minutes,
            "last_translated_id": last_translated,
            "in_window": in_window(),
            "forced": TRANSLATE_NOW.is_set(),
            "translator_ok": translator.ok,
            "translator_error": translator.last_error,
            "translator_batch_ok": translator_batch.ok,
            "translator_batch_error": translator_batch.last_error,
            "lanes": lanes,
            "pending_by_country": [{"country": r["country"] or "(none)", "pending": r["c"]} for r in pending_by_country],
            "translated_by_country": [{"country": r["country"] or "(none)", "translated": r["c"]} for r in translated_by_country],
       })


@app.route("/api/admin", methods=["PUT"])
def api_admin_put():
    """Save settings and group switches in one call.

    group_rules holds overrides only, so the PUT replaces the whole table with
    exactly the rows the client sent. Rows absent from the payload are deleted,
    which is what "re-enable this group" means: no row at all.
    """
    data = request.get_json(silent=True) or {}
    for key in SETTINGS_DEFAULTS:
        if key in data:
            value = str(data[key]).strip()
            if key in ("ongoing_model", "batch_model"):
                if not value or len(value) > 200:
                    return jsonify(error=f"{key} must be a non-empty model name or key label."), 400
            elif key in ("ongoing_price_in", "ongoing_price_out",
                         "batch_price_in", "batch_price_out"):
                try:
                    if float(value) < 0:
                        raise ValueError
                except (TypeError, ValueError):
                    return jsonify(error=f"{key} must be a price per 1M tokens, 0 or more."), 400
            elif key != "translate_enabled" and not value.lstrip("-").isdigit():
                return jsonify(error=f"{key} must be a number."), 400
            set_setting(key, value)
    if "rules" in data:
        incoming = set()
        with db() as con:
            con.execute("DELETE FROM group_rules")
            for rule in data["rules"] or []:
                axis = rule.get("axis")
                if axis not in ("region", "country"):
                    return jsonify(error="Unknown group axis."), 400
                name = (rule.get("name") or "").strip()
                incoming.add((axis, name))
                con.execute(
                    "INSERT OR REPLACE INTO group_rules(axis,name,pull,translate)"
                    " VALUES(?,?,?,?)",
                    (axis, name, 1 if rule.get("pull") else 0,
                     1 if rule.get("translate") else 0))
            # Drop rows for groups that no longer exist on any feed, so the table
            # does not accumulate rules for deleted feeds forever.
            stale = []
            for axis, name in [(r["axis"], r["name"]) for r in con.execute(
                    "SELECT DISTINCT axis,name FROM group_rules")]:
                live = con.execute(f"SELECT 1 FROM feeds WHERE {axis}=? LIMIT 1",
                                   (name,)).fetchone()
                if not live:
                    stale.append((axis, name))
            for axis, name in stale:
                con.execute("DELETE FROM group_rules WHERE axis=? AND name=?", (axis, name))
                incoming.discard((axis, name))
        log.info("admin saved group rules: %d active overrides", len(incoming))
    return jsonify(ok=True)


@app.route("/api/admin/translate-now", methods=["POST"])
def api_admin_translate_now():
    """Turn the worker loose on the backlog, ignoring window and budget.

    Works while the master switch is off, deliberately: the switch pauses the
    unattended queue, and pressing this button is a person choosing to spend
    on the backlog that piled up meanwhile. It is one-shot: the worker clears
    it as soon as the backlog empties or the daily budget is genuinely spent,
    so leaving it on cannot silently become an unlimited spender.
    """
    if TRANSLATE_AUTO_DISABLED:
        return jsonify(error="Automatic translation is currently disabled."), 403
    TRANSLATE_NOW.set()
    return jsonify(ok=True, forced=True)


@app.route("/api/admin/purge-pending", methods=["POST"])
def api_admin_purge_pending():
    """Delete untranslated foreign articles. Destructive and irreversible.

    dry_run=1 reports the exact count without touching anything, which is what
    the confirm dialog in the UI shows before asking. The count is re-read at
    delete time rather than trusted from the client, so the number in the
    confirmation and the number acted on cannot drift apart.
    """
    with db() as con:
        pending = con.execute(
            "SELECT COUNT(*) c, "
            "SUM(CASE WHEN a.read=0 THEN 1 ELSE 0 END) unread, "
            "COUNT(DISTINCT a.feed_id) feeds "
            "FROM articles a JOIN feeds f ON f.id=a.feed_id "
            "WHERE f.kind='foreign' AND a.title_en IS NULL").fetchone()
    summary = {"pending": pending["c"], "unread": pending["unread"] or 0,
               "feeds": pending["feeds"] or 0}
    if request.args.get("dry_run") == "1":
        return jsonify(ok=True, dry_run=True, **summary)
    with db() as con:
        cur = con.execute(
            "DELETE FROM articles WHERE title_en IS NULL AND feed_id IN "
            "(SELECT id FROM feeds WHERE kind='foreign')")
    summary["deleted"] = cur.rowcount
    log.info("admin purged %d untranslated foreign articles", cur.rowcount)
    return jsonify(ok=True, **summary)


@app.route("/api/admin/remove-oldest", methods=["POST"])
def api_admin_remove_oldest():
    """Remove the oldest XX articles from the database.
    
    xx parameter specifies how many oldest articles to remove.
    Returns the count of removed articles.
    """
    data = request.get_json(silent=True) or {}
    try:
        xx = int(data.get("xx", 0))
    except (TypeError, ValueError):
        return jsonify(error="xx must be a positive integer"), 400
    
    if xx <= 0:
        return jsonify(error="xx must be a positive integer"), 400
        
    removed = remove_oldest_xx(xx)
    log.info("admin removed %d oldest articles", removed)
    return jsonify(ok=True, removed=removed)


# ------------------------------------------------------------------- rss ---
ATOM_NS = "http://www.w3.org/2005/Atom"


def _lead_img_html(row):
    """Linked lead image, or "" when the article has no picture."""
    if not row["image"]:
        return ""
    return '<p><a href="%s"><img src="%s" alt="%s" /></a></p>' % (
        html.escape(row["link"] or "", quote=True),
        html.escape(row["image"], quote=True),
        html.escape(row["title"] or "", quote=True))


def _rss_description(row):
    """Body for <description>: lead image, attribution, plain-text summary.

    Readers that ignore content:encoded entirely still render the picture.
    """
    parts = []
    lead = _lead_img_html(row)
    if lead:
        parts.append(lead)
    if row["author"]:
        parts.append("<p>By %s</p>" % html.escape(row["author"]))
    if row["feed_kind"] == "foreign":
        if row["title_en"] and row["title_en"] != row["title"]:
            parts.append("<p><em>Original headline: %s</em></p>" % html.escape(row["title"]))
        # <link> stays the publisher URL because a reader app's "open" button
        # wants the real article; this is the translated copy alongside it.
        translated = google_translate_url(row["link"], row["country"])
        if translated:
            parts.append(
                '<p><a href="%s">Read this article in English (Google Translate)</a></p>'
                % html.escape(translated, quote=True))
    if row["summary"]:
        parts.append("<p>%s</p>" % html.escape(row["summary"]))
    return "".join(parts)


def _rss_content(row):
    """Body for <content:encoded>: the article HTML, image first."""
    body = row["content_html"] or ""
    if row["image"] and row["image"] not in body:
        lead = _lead_img_html(row)
        if lead:
            body = lead + body
    return body


@app.route("/feed")
def rss_feed():
    ET = _elementtree()
    where, params = article_filters(request.args)
    try:
        limit = min(int(request.args.get("limit", 100)), 500)
    except ValueError:
        limit = 100
    with db() as con:
        rows = con.execute(f"{ARTICLE_SELECT} WHERE {where} "
                           "ORDER BY a.published DESC, a.id DESC LIMIT ?",
                           params + [limit]).fetchall()

    parts = ["all news"]
    for key in ("kind", "cat", "region", "country", "source"):
        if request.args.get(key):
            parts.append(request.args[key])
    feed_title = "wrss · " + " / ".join(parts)

    base = request.base_url
    rss = ET.Element("rss", {"version": "2.0"})
    channel = ET.SubElement(rss, "channel")
    ET.SubElement(channel, "title").text = feed_title
    ET.SubElement(channel, "link").text = base
    ET.SubElement(channel, "description").text = "Aggregated RSS feed generated by wrss"
    ET.SubElement(channel, "generator").text = "wrss"
    ET.SubElement(channel, "lastBuildDate").text = formatdate(time.time(), usegmt=True)
    ET.SubElement(channel, "{%s}link" % ATOM_NS,
                  {"href": base, "rel": "self", "type": "application/rss+xml"})

    for r in rows:
        display = r["title_en"] if (r["feed_kind"] == "foreign" and r["title_en"]) else r["title"]
        item = ET.SubElement(channel, "item")
        ET.SubElement(item, "title").text = f"[{r['feed_name']}] {display}"
        ET.SubElement(item, "link").text = r["link"] or ""
        guid = ET.SubElement(item, "guid", {"isPermaLink": "false"})
        guid.text = f"wrss-{r['feed_name']}-{r['guid']}"
        ET.SubElement(item, "pubDate").text = formatdate(r["published"] or 0, usegmt=True)
        # image in all three dialects: enclosure for plain RSS readers,
        # media:thumbnail / media:content for Media RSS aware ones
        if r["image"]:
            mime = guess_image_type(r["image"])
            ET.SubElement(item, "enclosure",
                          {"url": r["image"], "type": mime, "length": "0"})
            ET.SubElement(item, "{%s}thumbnail" % MEDIA_NS, {"url": r["image"]})
            ET.SubElement(item, "{%s}content" % MEDIA_NS,
                          {"url": r["image"], "type": mime, "medium": "image"})
        ET.SubElement(item, "description").text = _rss_description(r)
        body = _rss_content(r)
        if body:
            ET.SubElement(item, "{%s}encoded" % CONTENT_NS).text = body

    xml = ET.tostring(rss, encoding="utf-8", xml_declaration=True)
    return Response(xml, mimetype="application/rss+xml")


def _elementtree():
    import xml.etree.ElementTree as ET
    ET.register_namespace("atom", ATOM_NS)
    ET.register_namespace("media", MEDIA_NS)
    ET.register_namespace("content", CONTENT_NS)
    return ET


# ------------------------------------------------------------------ main ---
init_db()
all_settings()                  # prime the cache before a worker thread reads it
threading.Thread(target=fetcher_loop, daemon=True).start()
threading.Thread(target=translate_worker, daemon=True).start()

if __name__ == "__main__":
    log.info("wrss listening on %s:%d", HOST, PORT)
    app.run(host=HOST, port=PORT, threaded=True, debug=False, use_reloader=False)
