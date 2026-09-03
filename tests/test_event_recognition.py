"""Real headlines this system collected, and the event each one is.

Every string here was observed live - on MAYA, on an issuer's own page, or on
the wire - during the audit that produced these patterns. They are regression
tests rather than examples: each one was, at the time it was captured, being
scored as NOISE, and the taxonomy is easy to narrow again by accident.

Two failure modes are pinned deliberately, because both were silent:

* a trailing ``\\b`` after a group of singular stems - ``(contract|order)\\b``
  cannot match "CONTRACTS" or "ORDERS", which is how the wire actually writes
  them, and ``\\b(FDA approv|...)\\b`` could not match "FDA APPROVAL";
* ``\\b`` before ``$`` - a space and a "$" are both non-word characters, so
  there is no boundary between them and "$105 million" mid-sentence never
  matched.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from harel.config import get_config
from harel.enrich.events import classify_events
from harel.models import RawItem


def _events(title: str, meta: dict | None = None) -> set[str]:
    item = RawItem(
        source="test", source_kind="test", external_id="t", title=title, url="",
        published_at=datetime.now(timezone.utc), meta=meta or {},
    )
    return {rule.key for rule, _ in classify_events(item, get_config())}


# (headline, event that must be present)
MUST_MATCH = [
    # -- issuer press releases, read off the companies' own pages ------------
    ("Elbit Systems Signed Contracts with an International Customer Valued at "
     "Approximately $270 Million for Advanced ISR & Targeting Payloads", "major_contract"),
    ("Elbit Systems of America Awarded Contracts from U.S Customs and Border "
     "Protection Totaling Over $370 Million", "major_contract"),
    ("CAMTEK RECEIVES OVER $105 MILLION MULTI-SYSTEM ORDERS FROM A TIER-1 OSAT "
     "AND A LEADING HBM MANUFACTURER", "major_contract"),
    ("Kamada Announces a Three-Year $50 Million Sales Agreement", "major_contract"),
    ("Kamada Declares Cash Dividend of $0.17 Per Share", "capital_return"),
    ("Kamada Announces Planned Transition of Chief Financial Officer", "management_change"),
    ("Tower Semiconductor Announces Record Results for Revenue and Profitability", "earnings"),
    ("Kenon Announces Receipt of approximately $93 million in connection with "
     "Payment of Arbitration Award by the Republic of Peru", "litigation_outcome"),
    ("OPKO Health Expands Strategic Relationship with HealthCare Royalty "
     "Through $125 Million Notes Issuance", "equity_offering"),
    ("Teva Announces Positive Topline Results from Phase 2a Study in Celiac Disease",
     "clinical_readout"),
    # -- analyst actions: the gap LIMITATIONS.md prices at $200-400/month ----
    ("Stifel initiates Tower Semiconductor stock with buy on SiPho growth", "rating_change"),
    ("PANW Reiterated by Piper Sandler -- Price Target Raised to $410", "rating_change"),
    ("DA Davidson raises Palo Alto Networks price target on strong results", "rating_change"),
    # -- the collectors' own generated titles --------------------------------
    ("[FDA APPROVAL] HOFFMANN LA ROCHE: ZELBORAF (NDA202429, SUPPL)",
     "regulatory_decision_primary"),
    # -- MAYA, in Hebrew: two thirds of this basket reports here first -------
    ("[MAYA] חתמה על חוזים עם לקוח בינלאומי בהיקף של כ-270מ'$", "major_contract"),
    ("[MAYA] 6K-תכנית רכישה עצמית של מניות בהיקף של 200מ'$", "capital_return"),
    ("[MAYA] הנפקה פרטית של אג\"ח להמרה ל5 שנים בהיקף של 100מ$", "equity_offering"),
    ("[MAYA] 8K-תוצאות כספיות לרבעון 4 ולשנה שמסתיימת ביום 31.7.26", "earnings"),
    ("[MAYA] רוכשת את PRN, חברה מובילה בתחום מדיה קמעונאית בחנויות", "merger_acquisition"),
    ("[MAYA] דיבידנד בסך 17.50273 אג' למניה", "capital_return"),
    ("פייפר סנדלר מעלה את יעד המחיר של מנית פאלו אלטו נטוורקס", "rating_change"),
    ("טבע מדווחת על תוצאות חיוביות בניסוי בנוגדן לחולי צליאק", "clinical_readout"),
]

# Headlines that must NOT reach the given event. Each one is a false positive
# that a widened pattern actually produced.
MUST_NOT_MATCH = [
    # A drug-pricing policy story is not a contract win, and "announce ...
    # agreement" alone was enough to make it one.
    ("Teva and Trump Administration Announce Intent to Reach Agreement around "
     "Affordable Medicines and Supply", "major_contract"),
    # "בהיקף של N מיליון" is equally the phrasing of a buyback and a bond
    # placement; it scored Nova's $200M repurchase as a $200M contract win.
    ("[MAYA] 6K-תכנית רכישה עצמית של מניות בהיקף של 200מ'$", "major_contract"),
    ("Analysts discuss the agreement between two unrelated companies", "major_contract"),
]


@pytest.mark.parametrize("title,event", MUST_MATCH)
def test_real_headline_is_classified(title, event):
    assert event in _events(title), f"{event!r} not found in {sorted(_events(title))}"


@pytest.mark.parametrize("title,event", MUST_NOT_MATCH)
def test_real_headline_is_not_misclassified(title, event):
    assert event not in _events(title)


def test_dollar_amount_is_matchable_after_a_space():
    """`\\b$` cannot match "$" preceded by a space - both are non-word chars."""
    import re
    assert not re.search(r"\b\$\s?\d+", "OVER $105 MILLION")
    assert re.search(r"\$\s?\d+", "OVER $105 MILLION")


def test_fda_supplement_is_capped_but_original_is_not():
    """61 of 70 approvals in a fortnight were routine supplements."""
    cfg = get_config()
    assert "FDA-SUPPL" in cfg.scoring.noise_form_types
    assert "FDA-ORIG" not in cfg.scoring.noise_form_types
