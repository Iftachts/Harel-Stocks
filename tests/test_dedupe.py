from __future__ import annotations

from datetime import datetime, timedelta, timezone

from harel.dedupe import Clusterer, dedupe_key, hamming, normalize_title, simhash
from harel.models import RawItem

T0 = datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc)


def item(title, source="google_news", offset_h=0):
    return RawItem(
        source=source, source_kind="rss", external_id=f"{source}:{title[:30]}",
        title=title, url="https://example.com",
        published_at=T0 + timedelta(hours=offset_h),
    )


def test_normalize_strips_wire_boilerplate():
    a = normalize_title("Teva Pharmaceutical Industries Ltd. announces FDA approval")
    b = normalize_title("(GLOBE NEWSWIRE) Teva Pharmaceutical Industries announces FDA approval")
    assert a == b


def test_identical_story_from_two_sources_shares_a_cluster():
    clusterer = Clusterer()
    _, c1 = clusterer.assign(item("Camtek receives $25 million order from a leading OSAT",
                                  source="company_ir_rss"))
    _, c2 = clusterer.assign(item("Camtek receives $25 million order from a leading OSAT",
                                  source="google_news"))
    assert c1 == c2


def test_reworded_headline_still_clusters():
    clusterer = Clusterer()
    _, c1 = clusterer.assign(item("Elbit Systems awarded a $300 million contract in Europe"))
    _, c2 = clusterer.assign(
        item("Elbit Systems was awarded a $300 million contract in Europe",
             source="globes", offset_h=1)
    )
    assert c1 == c2


def test_unrelated_stories_do_not_cluster():
    clusterer = Clusterer()
    _, c1 = clusterer.assign(item("Kamada reports second quarter financial results"))
    _, c2 = clusterer.assign(item("ICL settles the annual potash contract with India",
                                  source="globes"))
    assert c1 != c2


def test_same_headline_on_a_different_day_is_a_different_story():
    """A recurring headline ('monthly update') must not fold into last month's."""
    assert dedupe_key(item("Monthly operating update")) != dedupe_key(
        item("Monthly operating update", offset_h=48)
    )


def test_simhash_distance_behaves():
    a = simhash("Nova Ltd reports record quarterly revenue")
    b = simhash("Nova Ltd reports record quarterly revenues")
    c = simhash("Gilat wins a satellite ground segment contract in Latin America")
    assert hamming(a, b) < hamming(a, c)


def test_clusterer_respects_the_time_window():
    clusterer = Clusterer(window_hours=6)
    _, c1 = clusterer.assign(item("AudioCodes announces a share repurchase program"))
    _, c2 = clusterer.assign(
        item("AudioCodes announces a share repurchase programme",
             source="globes", offset_h=48)
    )
    assert c1 != c2, "a near-identical headline two days later is a new event"
