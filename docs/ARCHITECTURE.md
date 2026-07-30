# Architecture

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
  │ prices (stooq / yahoo)    │   │   move the print?    │      ┌───────────┐
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

Builds ~1,000 rules from `universe.yaml`: company names, aliases, symbols, our
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

Hot-path cost matters — 1,000 regexes × every item, every couple of minutes — so
each rule carries a literal `probe` string tested with a plain `in` before the
regex runs. That is ~4× on the full pipeline.

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

1. **`dedupe_key`** — normalised title (wire boilerplate stripped) + publication
   day. Exact collision ⇒ same story.
2. **SimHash** over word bigrams, 64-bit, Hamming ≤ 6, within a 36-hour window —
   catches rewrites and translations of the same wire copy.

The feed collapses a cluster to its highest-scoring member and attaches the rest
as `also[]`. `corroboration` = independent sources carrying the story, which the
agent is told to weigh.

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
