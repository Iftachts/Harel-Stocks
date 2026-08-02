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


# --------------------------------------------------------------------------- #
# A story does not stop being one story at a run boundary.
# --------------------------------------------------------------------------- #
def _item(title, when, source="google_news"):
    from datetime import datetime, timezone

    from harel.models import RawItem

    return RawItem(source=source, source_kind="rss", external_id=title[:40],
                   title=title, url="http://x", published_at=when)


def test_a_rewritten_headline_joins_the_story_it_arrived_after(config=None):
    """The near-duplicate matcher was rebuilt empty every pass, so the same
    story carried by a second publisher an hour later opened its own cluster.
    The exact-title half persisted by accident - `cluster_id` is derived from
    the key - which is why this went unnoticed."""
    from datetime import datetime, timedelta, timezone

    from harel.dedupe import Clusterer

    now = datetime.now(timezone.utc)
    a = _item("Teleflex Announces FDA BLA Approval of EZPLAZ Freeze Dried Plasma", now)
    first = Clusterer()
    key_a, cluster_a = first.assign(a, {"KMDA"})

    # A NEW run: a fresh Clusterer, primed only from what is stored.
    later = Clusterer()
    later.seed([{"dedupe_key": key_a, "cluster_id": cluster_a, "title": a.title,
                 "published_at": a.published_at, "tickers": {"KMDA"}}])
    b = _item("Teleflex announces FDA BLA approval of Ezplaz freeze dried plasma - TipRanks",
              now + timedelta(hours=1), source="google_news_he")
    _, cluster_b = later.assign(b, {"KMDA"})

    assert cluster_b == cluster_a, "the second copy must join the first one's story"


def test_two_similar_headlines_naming_no_common_company_stay_apart(config=None):
    """Widening the match across runs also widens the window for a false merge.
    On the live corpus exactly two pairs collided on a generic headline: one
    Evogene exhibit mentioning Kamada against the same exhibit mentioning
    Compugen, and two different FERC dockets both called "Combined Notice of
    Filings #1"."""
    from datetime import datetime, timedelta, timezone

    from harel.dedupe import Clusterer

    now = datetime.now(timezone.utc)
    a = _item("[EX-99.2] Evogene Ltd. (EVGN) (CIK 0001574565) mentions Kamada", now)
    b = _item("[EX-99.2] Evogene Ltd. (EVGN) (CIK 0001574565) mentions Compugen",
              now + timedelta(hours=2))

    c = Clusterer()
    _, cluster_a = c.assign(a, {"KMDA"})
    _, cluster_b = c.assign(b, {"CGEN"})

    assert cluster_a != cluster_b, "different subjects are not one story"


def test_the_window_still_bounds_a_merge(config=None):
    """Seeding must not let a match reach back past the window."""
    from datetime import datetime, timedelta, timezone

    from harel.dedupe import Clusterer

    now = datetime.now(timezone.utc)
    old = _item("Teleflex Announces FDA BLA Approval of EZPLAZ Freeze Dried Plasma",
                now - timedelta(hours=200))
    first = Clusterer()
    key, cluster = first.assign(old, {"KMDA"})

    later = Clusterer()
    later.seed([{"dedupe_key": key, "cluster_id": cluster, "title": old.title,
                 "published_at": old.published_at, "tickers": {"KMDA"}}])
    fresh = _item("Teleflex announces FDA BLA approval of Ezplaz freeze dried plasma - Yahoo",
                  now)
    _, cluster_fresh = later.assign(fresh, {"KMDA"})

    assert cluster_fresh != cluster, "200 hours apart is not the same event"
