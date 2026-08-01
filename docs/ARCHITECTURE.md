# Architecture

> This is the engineering view. For the plain-language walkthrough — where the
> information comes from and what happens to it — see
> [`HOW-IT-WORKS.md`](HOW-IT-WORKS.md).

```
                 collectors                 enrichment              surfaces
  ┌───────────────────────────┐   ┌──────────────────────┐   ┌──────────────────┐
  │ edgar_submissions         │   │ linker               │   │ CLI   harel …    │
  │ edgar_full_text           │   │   who is this about, │   │ REST  /api/*     │
  │ federal_register (+ PI)   │──▶│   and how?           │──▶│ MCP   tools      │
  │ fda (openFDA + RSS)       │   │ events               │   │ HTML  terminal   │
  │ clinicaltrials            │   │   what kind of news? │   └──────────────────┘
  │ maya (TASE)               │   │ materiality          │            ▲
  │ rss (IR, wires, Hebrew)   │   │   how much can it    │            │
  │ prices (yahoo)            │   │   move the print?    │      ┌───────────┐
  └───────────────────────────┘   └──────────────────────┘      │  SQLite   │
                │                            │                  │  + FTS5   │
                └────────► RawItem ──────────┴─► ScoredItem ────▶└───────────┘
                                                     │
                                            dedupe / cluster
```

Everything is config-driven. The four YAML files under `config/` hold all the
domain knowledge; the Python holds only mechanism.

---

## Data model

**`RawItem`** — what every collector emits, whatever the upstream shape:
`source`, `external_id`, `title`, `url`, `published_at`, `summary`, `body`,
`meta` (form type, 8-K items, NCT id, agency…), plus `seed_tickers` /
`seed_relation` for what the collector already knows.

`uid = sha1(source + external_id)` — the primary key. Re-collecting the same
filing updates the row instead of duplicating it, which is what makes
`harel collect` idempotent.

**`Link`** — a `(ticker, relation, confidence, why)` edge. `why` is a plain
sentence the agent can quote.

**`ScoredItem`** — `RawItem` + links + event labels + per-ticker scores + tier +
a `reasons` trace.

---

## The three enrichment stages

### 1. `enrich/linker.py` — who

Builds ~1,100 rules from `universe.yaml`: company names, aliases, symbols, our
products, rival products, competitor names, peer symbols, demand drivers, themes
and single points of failure. Precedence:

```
DIRECT > SUBSIDIARY > PRODUCT_RIVAL > CUSTOMER > PEER > SUPPLIER
       > SECTOR_REG > SECTOR_THEME > MACRO
```

Three details that took real care:

- **Ambiguous symbols.** `ICL`, `ORA`, `KEN`, `NICE`, `OPK`, `TAT` are ordinary
  English words. They only match with an exchange prefix (`NASDAQ: ICL`) or a `$`
  sigil. Without this the feed is unusable.
- **Locked relations.** Collectors that synthesise a headline containing our own
  company name (EDGAR full-text: *"Viatris mentions Teva"*) set
  `meta.lock_seed_relation`, so text matching cannot upgrade the link to DIRECT.
  That upgrade would turn a competitor's filing into "Teva news".
- **Synthetic items.** A `[TAPE] NVMI up 10%` alert links only to NVMI. Camtek
  lists NVMI as a peer, and without the guard the alert would read across.

- **Regulator documents seed nothing.** The Federal Register collectors query
  per sector, and `conditions[term]` searches the *full document text*. Seeding
  every ticker in the queried sector — which the linker honoured at 0.92 —
  turned a passing mention into a high-confidence link: a Family Violence
  Prevention rule became LivePerson and NICE news, a hospice wage index became
  BrainsWay news, both at score 45. The collector now emits `seed_tickers=[]`
  and keeps the queried tickers in `meta.queried_for` for the drill-down;
  `_link_sector_regulatory` establishes the link, and it reads only the title
  and abstract — the parts that say what a document is about.

Hot-path cost matters — 1,000 regexes × every item, every couple of minutes — so
each rule carries a literal `probe` string tested with a plain `in` before the
regex runs. That is ~4× on the full pipeline.

#### Relevance is not causation

`causal_eligible` is a separate axis from the score, and `whats_moving` gates
drivers on it rather than on any threshold.

A link is causally eligible when the document carries evidence about the
*company*: DIRECT, SUBSIDIARY, PRODUCT_RIVAL, CUSTOMER, PEER or SUPPLIER — all
of which exist because something named the company, its product, or a specific
counterparty we track for it. `SECTOR_REG`, `SECTOR_THEME` and `MACRO` are
evidence about a group and are not eligible.

The case that forced this: on 2026-07-31 one UFLPA entity-list notice was
offered as the reason TSEM rose 4.0% *and* as the reason CAMT fell 3.5%, with
SOXX flat. A document that explains a move and its opposite explains neither.

Sector-level matches are not suppressed — the entity list genuinely matters to a
foundry, and hiding it would be the same failure pointing the other way. They
surface as `possible_context` on the mover and on the unexplained-move alert,
stated at low confidence with "no company-specific exposure found", and the move
stays an open question.

One escalation exists. A sector-wide document predicts a sector-wide move, so if
at least 60% of a sector (minimum three names) moved in the same direction as
this name, with a median move of at least 1%, the item is promoted to a driver
and carries `causal_basis` saying why. A split basket corroborates nothing.

### 2. `enrich/events.py` — what

Matches the item against the taxonomy in `scoring.yaml`. An item can carry
several event types; all are kept, because the agent reasons better with the full
label set.

Form types are **supporting** evidence only, except for a small set that is
unambiguous alone (`424B5`, `NT 10-Q`, `SC 13D`). Generic containers — `8-K`,
`6-K`, `10-K` — can never stand alone; listing `8-K` under an event's
`form_types` once made every routine Teva filing score as a listing-compliance
event. There is a test guarding that now.

### 3. `enrich/materiality.py` — how much

```
score = base(event)
      × source_trust × relation_multiplier × float_sensitivity
      × recency_decay × link_confidence
      + session_timing_boost
      + per-ticker keyword boosts
      + tape confirmation
      − noise caps
```

- **8-K item severity** acts as both floor and ceiling: `critical` (4.02, 5.01,
  1.03) raises a base the regexes missed; `low` (5.03, 5.07, 9.01) caps an item
  whose text matched nothing.
- **Corroboration.** A `trust < 0.7` source whose story cluster contains no
  higher-trust member is capped at 60 — an aggregator alone never reaches ALERT.
- **`forced_score`.** Synthetic tape alerts carry a floor so an unexplained 5%
  move always surfaces.

---

## Dedupe and clustering

Two layers in `dedupe.py`:

0. **`meta.dedupe_id`** — a stable identifier from the source, when one exists.
   The Federal Register publishes one document twice, days apart, under one
   number: on public inspection and again on publication. Different title
   prefix, different date, so title-and-day made two stories out of one. The
   document number says otherwise and wins outright.
1. **`dedupe_key`** — normalised title (wire boilerplate stripped) + publication
   day. Exact collision ⇒ same story.
2. **SimHash** over word bigrams, 64-bit, Hamming ≤ 6, within a 36-hour window —
   catches rewrites and translations of the same wire copy.

The feed collapses a cluster to its highest-scoring member and attaches the rest
as `also[]`. `corroboration` = independent sources carrying the story, which the
agent is told to weigh.

**One event has one beginning.** `first_published_at` is the earliest
`published_at` across the whole cluster — asked of the cluster in the database,
not of the rows that happen to fall inside the query window, because the two
copies of one document sit days apart and a 30-hour feed window sees only the
later one. `views._event_start` uses it for the closing-bell test: the winning
row is usually the *later* copy, and testing that copy put a document readable
on Friday morning "after the bell" on Friday night.

---

## Language

The HTML terminal is **Hebrew, right-to-left**. The REST API, the MCP tools and
everything stored in SQLite stay English, because English is what the downstream
agent is instructed in - so `serve/hebrew.py` is a presentation layer, never a
translation of the data model.

Two kinds of pipeline string arrive in English and have to be spoken Hebrew on
screen: link explanations (`why`) and scoring-trace steps. Both are generated
from a small, stable set of shapes, so they are rewritten by an ordered pattern
table rather than translated as prose. The tables were written against the
strings actually in the database - which is how the `in headline` / `in body`
suffix, the space-bearing form types (`DEF 14A`) and the compound base reasons
were found. Coverage is 100% of both corpora; anything unmatched falls through
unchanged into an LTR island, because a legible English fragment beats a wrong
Hebrew one.

**Bidi is the hard part**, and it is a correctness problem, not a cosmetic one:

- Cells keep the *page* direction so their content hugs the reading edge. Putting
  `direction: ltr` on the cell aligned every number to the far side of its
  column, which glued `TATT` to `רגולציה` and `19` to `TATT`.
- Latin and numeric fields are wrapped in `<span class='ltr'>` - an isolate.
  Without it `+4.8%` renders as `%4.8+`.
- A unit belongs *inside* the island with its number. `NORMAL {island} · HIGH
  {island}` left the labels outside and the algorithm reordered the run into
  `75 NORMAL 35 · HIGH 55 · ALERT`.
- Two adjacent islands swap places, so a pair like `ITA +0.6%` is one island.
- Headlines carry `dir="auto"`: they can be either language, so the browser
  decides per string.
- The stylesheet uses only logical properties (`border-inline-start`,
  `padding-inline`, `text-align: start`). A test rejects physical ones.

---

## The audit surface

`views.explain(uid)` is the one place that reassembles everything known about an
item: the query or feed that found it (`meta.feed_label`), the source's trust and
what that number means in words, publication time in UTC/ET/Israel against
`last_session_close()`, the detection lag (`collected_at - published_at`), the
link rules with their `why`, the stored `reasons` trace regrouped into item-wide
and per-ticker steps, cluster membership, the tape with its provider, and a set
of outside verification URLs.

It **never recomputes**. `_trace()` only regroups and labels the stored strings,
and a test asserts the multiset of steps out equals the multiset stored — a
prettier explanation that quietly differs from the score is worse than no
explanation.

It backs four surfaces: `/item/{uid}` (HTML), `/api/explain/{uid}`, the `explain`
MCP tool and `harel explain`. `RELATION_MEANING` lives in `views.py` and is read
by the REST manifest, so what the trader reads and what the agent is told cannot
drift.

Two supporting details exist only for this: `prices.provider` (a quote with no
stated origin cannot be reconciled against a broker screen) and `meta.seed_why`,
which lets a collector explain *its own* seeding — "the story names 'NRG Energy',
tracked as a rival product for KEN" instead of "collected from google_news as
product_rival".

---

## Storage

SQLite with WAL and FTS5 (`unicode61`, so Hebrew search works). Tables: `items`,
`item_tickers`, `items_fts`, `prices`, `bars`, `calendar`, `source_state`,
`run_log`.

`source_state` holds ETag / Last-Modified for conditional GETs, per-source
cursors (the ClinicalTrials fingerprint map, the resolved CIK cache) and honest
health state — `consecutive_failures`, `last_error` — which is what
`harel doctor` reads.

---

## HTTP policy

`http.py` enforces a per-host token bucket (SEC 5 req/s against their 10 limit),
exponential backoff with jitter on 429/5xx, `Retry-After` when offered, and
conditional GET everywhere. Politeness here is a correctness requirement: SEC
will ban a misbehaving client and EDGAR is the best source in the system.

---

## Failure model

- A collector must never raise for one bad record. It calls `self.warn()` and
  continues; warnings surface in `harel doctor` and in `coverage_warnings`.
- A source needing an absent key is **skipped**, not fatal. Sources marked
  `key_optional` (MAYA) still run on a fallback and declare themselves degraded.
- An item that links to nothing in the universe is **dropped**, not stored.
- The `morning_brief` always carries `coverage_warnings`, and the MCP
  instructions tell the agent to read it before ever saying "there is no news".
  Silence must be distinguishable from blindness.

---

## Adding a ticker

Add a block to `config/universe.yaml`. The fields that actually drive coverage
quality are `peer_names`, `competitor_products` and `themes` — those are what the
linker turns into indirect coverage. `cik` can be left `null`; it is resolved at
runtime from the SEC ticker map.

`tests/test_config.py` asserts every active ticker has peers and themes, so a
half-filled entry fails the suite rather than silently under-collecting.
