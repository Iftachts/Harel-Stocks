"""Reading the 6-K, which is the form two thirds of this basket actually uses.

17 of the 22 are Foreign Private Issuers, and a 6-K carries no item-code
taxonomy: the submissions feed returns items='' and primaryDocDescription='6-K'.
The title this collector could build was literally "[6-K] ELBIT SYSTEMS LTD -
6-K" - for Elbit, 845 filings rendered contentless. The words are one directory
listing and one document away, in the exhibit.
"""

from __future__ import annotations

import pytest

from harel.collect.edgar import _exhibit_headline, _exhibit_size


# Shapes taken from real filings: Z-K Global writes exhibit_1.htm, EdgarAgents
# writes ea030232701ex99-1.htm, and EDGAR prepends its own header line.
ELBIT = """<html><body>
<p>EX-99 2 exhibit_1.htm</p>
<p><b>Elbit Systems Signed Contracts with an International Customer Valued at
Approximately $270 Million for Advanced ISR &amp; Targeting Payloads</b></p>
<p>Haifa, Israel, September 1, 2026 &ndash; Elbit Systems Ltd. (NASDAQ: ESLT)
announced today that it has signed contracts...</p>
</body></html>"""

COVER_PAGE = """<html><body>
<p>UNITED STATES SECURITIES AND EXCHANGE COMMISSION</p>
<p>Washington, D.C. 20549</p>
<p>FORM 6-K</p>
<p>Report of Foreign Private Issuer</p>
<p>Pursuant to Rule 13a-16 or 15d-16</p>
<p>of the Securities Exchange Act of 1934</p>
</body></html>"""


def test_the_headline_is_read_out_of_the_exhibit():
    got = _exhibit_headline(ELBIT)
    assert got.startswith("Elbit Systems Signed Contracts")
    assert "$270 Million" in got


def test_edgars_own_rendering_header_is_stripped():
    """EDGAR prepends 'EX-99 2 exhibit_1.htm' to the rendered exhibit."""
    assert not _exhibit_headline(ELBIT).lower().startswith("ex-99")
    assert ".htm" not in _exhibit_headline(ELBIT)


def test_the_dateline_ends_the_headline():
    """Everything from 'Haifa, Israel, September 1, 2026 -' onward is body."""
    assert "announced today" not in _exhibit_headline(ELBIT)
    assert "NASDAQ" not in _exhibit_headline(ELBIT)


def test_a_cover_page_yields_nothing_rather_than_boilerplate():
    """The primary document of a 6-K is the cover page. 'Report of Foreign
    Private Issuer Pursuant to Rule 13a-16' as a headline would be worse than
    the form name it replaced, so there is deliberately no fallback to it."""
    assert _exhibit_headline(COVER_PAGE) == ""


def test_a_leading_page_number_is_dropped():
    html = "<p>1 Acquisition of IPS Group Creating a Full-Stack Platform</p>"
    assert _exhibit_headline(html).startswith("Acquisition of IPS Group")


def test_headline_is_length_capped():
    from harel.collect.edgar import EXHIBIT_HEADLINE_CHARS
    html = "<p>" + ("Very long headline text. " * 80) + "</p>"
    assert len(_exhibit_headline(html)) <= EXHIBIT_HEADLINE_CHARS


@pytest.mark.parametrize("entry,expected", [
    ({"size": "18788"}, 18788),
    ({"size": ""}, 0),          # EDGAR leaves this blank on index files
    ({"size": None}, 0),
    ({}, 0),
])
def test_missing_exhibit_size_does_not_raise(entry, expected):
    assert _exhibit_size(entry) == expected


def test_exhibit_work_is_budgeted_and_cached():
    """Two requests per 6-K, and the submissions feed re-emits the same filings
    every pass - so the answer is cached by accession and capped per run."""
    from harel.collect.edgar import MAX_EXHIBIT_LOOKUPS_PER_RUN
    assert 0 < MAX_EXHIBIT_LOOKUPS_PER_RUN <= 20


# ------------------------------------------------------- what it unlocks ---
@pytest.mark.parametrize("title,expected", [
    ("[6-K] ELBIT SYSTEMS LTD - Elbit Systems Signed Contracts with an "
     "International Customer Valued at Approximately $270 Million", "major_contract"),
    ("[6-K] Perion Network Ltd. - Perion Acquires PRN, a Leading In-Store "
     "Retail Media Company", "merger_acquisition"),
    ("[6-K] Nayax Ltd. - Acquisition of IPS Group Creating a Full-Stack Platform",
     "merger_acquisition"),
    ("Teva Announces Proposed Acquisition of a Novel Neuroscience Product",
     "merger_acquisition"),
])
def test_the_recovered_headline_is_classifiable(title, expected):
    from datetime import datetime, timezone
    from harel.config import get_config
    from harel.enrich.events import classify_events
    from harel.models import RawItem

    item = RawItem(source="sec_edgar_submissions", source_kind="edgar_submissions",
                   external_id="t", title=title, url="",
                   published_at=datetime.now(timezone.utc))
    assert expected in {r.key for r, _ in classify_events(item, get_config())}


@pytest.mark.parametrize("title", [
    "Analysts discuss acquires and buys in general terms",
    "The company acquired new office furniture last year",
])
def test_prose_about_acquiring_is_not_a_takeover(title):
    """Every pattern in scoring.yaml compiles with IGNORECASE, so a bare [A-Z]
    matches lowercase too - which made this sentence a takeover."""
    from datetime import datetime, timezone
    from harel.config import get_config
    from harel.enrich.events import classify_events
    from harel.models import RawItem

    item = RawItem(source="test", source_kind="test", external_id="t",
                   title=title, url="",
                   published_at=datetime.now(timezone.utc))
    assert "merger_acquisition" not in {r.key for r, _ in classify_events(item, get_config())}
