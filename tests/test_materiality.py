from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from harel.enrich.linker import EntityLinker
from harel.enrich.materiality import MaterialityScorer, PriceContext
from harel.models import RawItem

NOW = datetime(2026, 7, 30, 14, 0, tzinfo=timezone.utc)   # 10:00 ET, intraday


@pytest.fixture(scope="module")
def parts():
    from pathlib import Path

    from harel.config import load_config

    cfg = load_config(Path(__file__).resolve().parents[1] / "config")
    return cfg, EntityLinker(cfg), MaterialityScorer(cfg)


def score(parts, title, *, source="company_ir_rss", summary="", meta=None,
          minutes_old=30, prices=None):
    cfg, linker, scorer = parts
    item = RawItem(
        source=source, source_kind="rss", external_id=title[:60], title=title,
        url="https://example.com", summary=summary,
        published_at=NOW - timedelta(minutes=minutes_old), meta=meta or {},
    )
    links = linker.link(item)
    return scorer.score(item, links, price_by_ticker=prices or {}, now=NOW)


# ----------------------------------------------------------------- tiers -- #
def test_fda_approval_is_an_alert(parts):
    result = score(parts, "FDA approves Kamada's KEDRAB for post-exposure prophylaxis")
    assert result.tier == "ALERT", result.reasons
    assert "regulatory_decision_primary" in result.events


def test_conference_presentation_is_noise(parts):
    result = score(parts, "Compugen to present at the Jefferies Healthcare Conference")
    assert result.tier == "NOISE", result.reasons
    assert result.score < 35


def test_esg_report_is_noise(parts):
    result = score(parts, "ICL Group publishes its 2025 sustainability report")
    assert result.score < 25


def test_s8_registration_is_capped_as_noise(parts):
    """Exactly the 'registration filings a trader does not care about' case."""
    result = score(
        parts, "[S-8] Teva Pharmaceutical Industries Ltd - registration statement",
        source="sec_edgar_submissions", meta={"form_type": "S-8"},
    )
    assert result.score <= 12, result.reasons


def test_form_144_is_capped(parts):
    result = score(
        parts, "[144] Nova Ltd - notice of proposed sale of securities",
        source="sec_edgar_submissions", meta={"form_type": "144"},
    )
    assert result.score <= 8


def test_late_filing_notice_is_not_treated_as_noise(parts):
    """NT 10-Q looks like paperwork but is a genuine red flag."""
    result = score(
        parts, "[NT 10-Q] LivePerson Inc - notification of late filing",
        source="sec_edgar_submissions", meta={"form_type": "NT 10-Q"},
    )
    assert result.score >= 35, result.reasons


# ----------------------------------------------------------- relations --- #
def test_peer_news_scores_below_the_same_news_about_us(parts):
    ours = score(parts, "Camtek raises full-year guidance on HBM inspection demand")
    theirs = score(parts, "Onto Innovation raises full-year guidance on HBM demand")
    camt_ours = ours.per_ticker_score.get("CAMT", 0)
    camt_theirs = theirs.per_ticker_score.get("CAMT", 0)
    assert camt_ours > camt_theirs > 0, (ours.per_ticker_score, theirs.per_ticker_score)


def test_tigit_read_across_is_treated_almost_as_direct_for_cgen(parts):
    """CGEN carries a PRODUCT_RIVAL override precisely because class data moves it."""
    result = score(
        parts,
        "Roche says tiragolumab met the primary endpoint in the SKYSCRAPER-01 trial",
    )
    assert result.per_ticker_score.get("CGEN", 0) >= 55, result.reasons


# --------------------------------------------------------------- floats -- #
def test_offering_scores_higher_in_a_microcap_than_a_large_cap(parts):
    micro = score(parts, "Oramed Pharmaceuticals announces pricing of public offering")
    large = score(parts, "Teva Pharmaceutical Industries announces pricing of public offering")
    assert micro.per_ticker_score["ORMP"] > large.per_ticker_score["TEVA"]


# --------------------------------------------------------------- timing -- #
def test_premarket_item_beats_the_same_item_after_the_close(parts):
    """Age held constant so this isolates the session-timing boost from decay.
    12:00 UTC = 08:00 ET (pre-market); 22:00 UTC = 18:00 ET (closed)."""
    cfg, linker, scorer = parts

    def at(hour_utc):
        published = datetime(2026, 7, 30, hour_utc, 0, tzinfo=timezone.utc)
        item = RawItem(
            source="company_ir_rss", source_kind="rss", external_id=f"x{hour_utc}",
            title="AudioCodes reports fourth quarter results and raises guidance",
            url="https://example.com", published_at=published,
        )
        return scorer.score(item, linker.link(item),
                            now=published + timedelta(hours=1))

    assert at(12).per_ticker_score["AUDC"] > at(22).per_ticker_score["AUDC"]


def test_the_session_boosts_follow_the_tzdb_not_the_month(parts):
    """`offset = 4 if 3 <= month <= 11 else 5` is a month-based DST guess, and
    DST ends the first Sunday of November: through most of November an item
    published at 08:45 ET was read as 09:45 and lost the pre-market boost,
    while one published at 15:30 ET was read as 16:30 and lost the intraday
    boost. Both are the difference between a gap and an archive entry."""
    cfg, linker, scorer = parts

    def boost_reason(published):
        item = RawItem(
            source="company_ir_rss", source_kind="rss", external_id="tz",
            title="AudioCodes reports fourth quarter results and raises guidance",
            url="https://example.com", published_at=published,
        )
        result = scorer.score(item, linker.link(item),
                              now=published + timedelta(minutes=5))
        return " ".join(r for r in result.reasons if "published" in r)

    november = datetime(2026, 11, 20, tzinfo=timezone.utc)     # EST, UTC-5
    assert "pre-market (08:45 ET)" in boost_reason(november.replace(hour=13, minute=45))
    assert "intraday (15:30 ET)" in boost_reason(november.replace(hour=20, minute=30))


def test_recency_decays(parts):
    fresh = score(parts, "Gilat wins $50 million defense satcom contract", minutes_old=10)
    stale = score(parts, "Gilat wins $50 million defense satcom contract",
                  minutes_old=60 * 40)
    assert fresh.per_ticker_score["GILT"] > stale.per_ticker_score["GILT"]


# ---------------------------------------------------------------- trust -- #
def test_low_trust_source_cannot_reach_alert_alone(parts):
    result = score(
        parts,
        "Teva Pharmaceutical Industries agrees to acquire a rival in a definitive "
        "merger agreement",
        source="google_news",
    )
    assert result.tier != "ALERT", result.reasons
    assert result.score <= 60


def test_high_trust_source_can_reach_alert(parts):
    result = score(
        parts,
        "Teva Pharmaceutical Industries agrees to acquire a rival in a definitive "
        "merger agreement",
        source="company_ir_rss",
    )
    assert result.tier == "ALERT", result.reasons


# ---------------------------------------------------------------- price -- #
def test_tape_confirmation_boosts_a_real_story(parts):
    headline = "Perion Network cuts its full-year revenue guidance"
    plain = score(parts, headline)
    confirmed = score(parts, headline,
                      prices={"PERI": PriceContext(change_pct=-9.4, volume_multiple=5.1)})
    assert plain.events, "the fixture headline must classify as an event"
    assert confirmed.per_ticker_score["PERI"] > plain.per_ticker_score["PERI"]


def test_tape_confirmation_does_not_promote_a_chart_generated_article(parts):
    """"News the tape is confirming outranks news nothing reacted to" - but an
    article generated FROM the chart is not news. A "Technical Analysis:
    Support, Resistance, Indicators" piece on base 28 with no event collected
    +8 for coinciding with the move it was generated from, and outranked a
    guidance raise carrying base 86."""
    headline = "Perion Network Ltd (PERI) Technical Analysis: Support, Resistance"
    plain = score(parts, headline)
    confirmed = score(parts, headline,
                      prices={"PERI": PriceContext(change_pct=-9.4, volume_multiple=5.1)})
    assert not plain.events, "this headline must not classify as an event"
    assert confirmed.per_ticker_score["PERI"] == plain.per_ticker_score["PERI"]
    assert confirmed.per_ticker_score["PERI"] <= 12, confirmed.reasons


# ------------------------------------------------------------ 8-K items -- #
def test_8k_item_severity_lifts_a_headline_the_regexes_would_miss(parts):
    result = score(
        parts, "[8-K] NOVA LTD",
        source="sec_edgar_submissions",
        meta={"form_type": "8-K", "items": ["4.02"], "item_severity": "critical",
              "item_labels": ["Non-reliance on prior financials"]},
    )
    assert result.tier == "ALERT", result.reasons


def test_8k_shareholder_vote_stays_low(parts):
    result = score(
        parts, "[8-K] ALLOT LTD - Shareholder vote results",
        source="sec_edgar_submissions",
        meta={"form_type": "8-K", "items": ["5.07"], "item_severity": "low",
              "item_labels": ["Shareholder vote results"]},
    )
    assert result.score < 35, result.reasons


# ------------------------------------------------------------ keywords --- #
def test_ticker_keyword_boost_applies(parts):
    plain = score(parts, "Perion Network announces a new product launch")
    boosted = score(
        parts,
        "Perion Network announces a change to its Microsoft Bing search "
        "advertising revenue share agreement",
    )
    assert boosted.per_ticker_score["PERI"] > plain.per_ticker_score["PERI"] + 10


# -------------------------------------------------------- explainability -- #
def test_every_score_carries_a_reason_trace(parts):
    result = score(parts, "Elbit Systems awarded a $300 million contract in Europe")
    assert result.reasons
    assert any("relation" in r for r in result.reasons)
    assert any("event=" in r or "default base" in r for r in result.reasons)


def test_panw_ngs_arr_keyword_boost(parts):
    plain = score(parts, "Palo Alto Networks announces a new product launch")
    boosted = score(
        parts, "Palo Alto Networks reports next-generation security ARR above guidance"
    )
    assert boosted.per_ticker_score["PANW"] > plain.per_ticker_score["PANW"] + 10


def test_a_lowercase_override_key_applies_its_whole_block(tmp_path):
    """`cgen:` instead of `CGEN:` used to apply half its own block: the keyword
    boosts were compiled under an uppercased key and fired, the relation
    override was looked up under the raw key and was silently dropped. A trader
    editing YAML got a score that matched neither reading."""
    import shutil
    from pathlib import Path

    from harel.config import load_config

    cdir = tmp_path / "config"
    shutil.copytree(Path(__file__).resolve().parents[1] / "config", cdir)
    scoring = cdir / "scoring.yaml"
    text = scoring.read_text(encoding="utf-8")
    assert "\n  CGEN:\n" in text
    scoring.write_text(text.replace("\n  CGEN:\n", "\n  cgen:\n"), encoding="utf-8")

    cfg = load_config(cdir)
    scorer = MaterialityScorer(cfg)
    item = RawItem(
        source="company_ir_rss", source_kind="rss", external_id="tigit",
        title="Roche says tiragolumab met the primary endpoint in SKYSCRAPER-01",
        url="https://example.com", published_at=NOW - timedelta(minutes=30),
    )
    result = scorer.score(item, EntityLinker(cfg).link(item), now=NOW)
    assert any("relation PRODUCT_RIVAL override x0.95" in r for r in result.reasons), \
        result.reasons


# ------------------------------------------------------ sector read-across -- #
def test_a_peer_story_is_weighted_by_its_sectors_own_read_across(parts):
    """`peer_read_across` is tuned per sector and was parsed and then never
    read, so the semicap comment - "WFE names trade almost 1:1 with peers'
    guidance, the single most valuable indirect channel for NVMI/CAMT" - moved
    no score at all. 0.85 there against 0.65 globally has to mean something."""
    cfg, _, _ = parts
    result = score(parts, "Onto Innovation raises full-year guidance on HBM demand")

    peer = cfg.sector(cfg.ticker("CAMT").sector).peer_read_across
    assert peer == 0.85
    assert any(f"[CAMT] relation PEER x{peer:.2f}" in r for r in result.reasons), \
        result.reasons


def test_a_sector_that_barely_reads_across_scores_below_one_that_does(parts):
    """The same regulator document, two sectors: semicap at 0.75 lives on
    export-control rules, enterprise software at 0.30 does not."""
    cfg, _, scorer = parts
    from harel.models import Link

    item = RawItem(
        source="federal_register", source_kind="federal_register", external_id="fr",
        title="[FR] Additional Export Controls: Semiconductor Manufacturing Equipment",
        url="https://example.com", published_at=NOW - timedelta(minutes=30),
    )
    result = scorer.score(
        item,
        [Link("CAMT", "SECTOR_REG", 0.62, 'mentions "entity list"'),
         Link("NICE", "SECTOR_REG", 0.62, 'mentions "entity list"')],
        now=NOW,
    )
    assert result.per_ticker_score["CAMT"] > result.per_ticker_score["NICE"]


def test_a_ticker_override_still_beats_its_sectors_read_across(parts):
    """Kenon's peers are peers of the *assets*, not of the listed holding
    company, so KEN carries PEER: 0.45 while its sector reads across at 0.50.
    The narrower statement has to win, or the override is decoration."""
    cfg, _, _ = parts
    sector = cfg.sector(cfg.ticker("KEN").sector)
    override = cfg.scoring.overrides["KEN"]["relation_overrides"]["PEER"]
    assert override < sector.peer_read_across

    result = score(parts, "NRG Energy raises full-year guidance on data-centre demand")
    assert any(f"[KEN] relation PEER override x{override:.2f}" in r
               for r in result.reasons), result.reasons


def test_microsoft_bundling_is_classified_as_a_competitive_threat(parts):
    """A platform owner bundling away your product is a first-order event for
    PANW, AUDC and PERI, and nothing else in the taxonomy catches it."""
    result = score(
        parts,
        "Microsoft says it will bundle Defender for Cloud into E5 licensing at no extra cost",
    )
    assert "competitive_threat" in result.events, result.events
    assert result.per_ticker_score.get("PANW", 0) >= 40, result.reasons
