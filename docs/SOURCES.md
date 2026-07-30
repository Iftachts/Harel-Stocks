# Source inventory

Everything the terminal polls, why it is there, and how fast it is.
Configured in [`config/sources.yaml`](../config/sources.yaml).

`trust` multiplies materiality. Issuer and regulator sources are 1.0; aggregators
are 0.6–0.75 and cannot reach ALERT without corroboration from a higher-trust
source carrying the same story.

---

## Issuer channel — trust 1.0

| Source | What | Latency | Key |
|---|---|---|---|
| `sec_edgar_submissions` | 8-K / 6-K / 424B5 / 13D / NT filings per CIK | seconds–minutes | none (UA required) |
| `sec_edgar_full_text` | our names inside **other** issuers' filings | minutes | none |
| `company_ir_rss` | issuer press releases | real time | none |
| `maya_tase` | Israeli immediate reports (דיווח מיידי) | real time | `TASE_API_KEY` optional |

**`sec_edgar_submissions`** is the single highest-value source. 8-K Item codes are
parsed and mapped to severity — Item 4.02 (non-reliance on prior financials) is
`critical` and lifts the score even when the headline says nothing; Item 5.07
(shareholder vote results) is `low` and caps it.

**`sec_edgar_full_text`** is the indirect-news engine. It finds a competitor's
10-K naming Teva as a litigant, or a customer disclosing a Camtek order. Links
are locked at `PEER` — the collector synthesises a headline containing our own
company name, and without the lock the linker would read that back and call it
DIRECT.

**`maya_tase`** is the structural edge for this basket (20 of 22 dual-listed). See
[LIMITATIONS §3](LIMITATIONS.md#3-tase--maya--עובד-אבל-על-endpoint-לא-מתועד).

---

## Regulatory channel — trust 0.9–1.0

| Source | Covers | Latency |
|---|---|---|
| `federal_register` | BIS export controls, FDA rules, CMS rates, IRS energy credits, ITC duties, FAA ADs, FCC | next business day |
| `federal_register_public_inspection` | the same documents **~1 business day early** | hours |
| `fda_press` | FDA press releases + MedWatch | real time |
| `fda_drug_approvals` | Drugs@FDA approvals (ours **and** competitors') | ~1 day |
| `fda_enforcement` | drug / device / food recalls | ~1 day |
| `fda_device_clearances` | 510(k) and De Novo | weekly |
| `fda_warning_letters` | warning letters, import alerts | weekly, **fragile** |
| `ema_news` | EMA / CHMP outcomes | daily |
| `clinicaltrials` | trial **status changes**, not snapshots | daily |
| `dod_contracts` | US DoD daily contract awards (~17:00 ET) | daily |
| `dsca_fms` | foreign military sale notifications | daily, **fragile** |
| `faa_ads` | airworthiness directives | daily |
| `ferc_filings` | FERC news and orders | daily, **fragile** |
| `echa_reach` | EU REACH / ECHA | weekly, **fragile** |
| `fcc_filings` | FCC ECFS | daily, needs `FCC_API_KEY` |
| `courtlistener` | patent + securities dockets | daily, needs `COURTLISTENER_TOKEN` |

`federal_register` runs one query per (sector agency set × sector term). Terms are
in [`config/sectors.yaml`](../config/sectors.yaml) — that file is where you widen
or narrow regulatory coverage.

`clinicaltrials` keeps a fingerprint per NCT id and only emits when the status or
the primary-completion date actually moved. It watches our sponsors **and** the
competitor programs in each ticker's `competitor_products`, because for CGEN and
ORMP a rival's Phase 3 moves the stock more than their own press release.

**Fragile** sources are HTML scrapes or undocumented feeds. They fail soft: a
warning in `harel doctor`, never a failed run.

---

## Market channel

| Source | What | Latency |
|---|---|---|
| `prices_stooq` | daily OHLCV, ADV20, gap | end of day |
| `prices_yahoo` | quote incl. pre/post market | ~15 min, unofficial |

Used for score confirmation and for the `whats_moving` view. A move ≥5% with no
story scoring ≥45 in the last 18 hours becomes a synthetic `[TAPE]` alert.

---

## Aggregators — trust 0.6–0.75

| Source | Trust | Note |
|---|---|---|
| `globes` | 0.75 | Israeli scoops routinely precede the English wires |
| `calcalist` | 0.70 | **fragile** |
| `google_news` | 0.60 | broad safety net; one query per name plus sector themes |
| `google_news_he` | 0.60 | Hebrew queries from each ticker's Hebrew aliases |

Google News queries anchor on the **company name**, not the bare symbol — `ICL`,
`ORA`, `KEN` and `NICE` are ordinary English words and matching them bare floods
the feed. The same rule is enforced in the linker via `AMBIGUOUS_TICKERS`.

---

## Disabled by default

| Source | Why | Cost |
|---|---|---|
| `benzinga_ratings` | analyst rating changes — the biggest gap vs Bloomberg | paid |
| `finnhub` | earnings calendar (free tier) + ratings (paid) | free tier available |

Set `enabled: true` and provide the key. `scoring.yaml` already classifies
`rating_change`.

---

## Adding a source

1. Add a block to `config/sources.yaml` with a `kind`.
2. If the `kind` is new, write a collector in `src/harel/collect/` decorated with
   `@register("your_kind")`, implementing `collect() -> Iterator[RawItem]`.
3. Import it in `src/harel/collect/__init__.py`.
4. Add a fixture under `tests/fixtures/` and a test — the suite runs with no network.

A collector must never raise for one bad record. Call `self.warn(...)` and carry
on; the pipeline reports warnings without losing the batch.
