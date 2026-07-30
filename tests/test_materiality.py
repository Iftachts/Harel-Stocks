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
def test_tape_confirmation_boosts_the_score(parts):
    plain = score(parts, "Perion Network updates its outlook for the full year")
    confirmed = score(
        parts, "Perion Network updates its outlook for the full year",
        prices={"PERI": PriceContext(change_pct=-9.4, volume_multiple=5.1)},
    )
    assert confirmed.per_ticker_score["PERI"] > plain.per_ticker_score["PERI"]


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
