"""The TA-35 additions: Hebrew event vocabulary, Hebrew name ambiguity, numeric
IR-page dates and the Bank of Israel rate collector.

Every title here is one this system actually collected for the new names on
2026-10-07, scored wrongly, and was the reason for the change it pins.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from harel.collect.base import CollectorContext
from harel.enrich.events import classify_events
from harel.enrich.linker import EntityLinker
from harel.models import RawItem

from conftest import FakeHttpClient


def item(title: str, summary: str = "", source: str = "maya_tase") -> RawItem:
    return RawItem(source=source, source_kind="maya", external_id=title[:40],
                   title=title, url="https://example.com", summary=summary,
                   published_at=datetime.now(timezone.utc))


def events(config, title: str) -> list[str]:
    return [rule.key for rule, _ in classify_events(item(title), config)]


def top_event(config, title: str) -> str | None:
    found = events(config, title)
    return found[0] if found else None


# --------------------------------------------------------------- events --- #
@pytest.mark.parametrize("title,expected", [
    ("[MAYA] קבלת הזמנה לרכישת מצלמות ומוצרים נוספים של החברה תמורת סך כולל של 24מ'$",
     "major_contract"),
    ("[MAYA] Receipt of purchase order", "major_contract"),
    ("[MAYA] הסכם אספקת גז מפרו' לוויתן - לעמדת רצו ונומד הודעת הביטול תקפה",
     "contract_termination"),
    ("[MAYA] איתקה – חתימה על עסקה לרכישת נכסי נפט ימיים בקנדה מחברת Suncor",
     "asset_transaction"),
    ("[MAYA] הבהרה בעקבות פניות משקיעים בקשר עם פרוייקט סי ליון",
     "disclosure_clarification"),
    ("[MAYA] אפשרות לביצוע הנפקת נע\"מ 15 בנוסף לאגח 189 ו190 לציבור",
     "debt_financing"),
    ("בנק ישראל הותיר את הריבית ללא שינוי", "macro_sector_policy"),
    ("משה לארי ורון אבידן בין המועמדים לתפקיד מנכ״ל מליסרון", "management_change"),
])
def test_hebrew_reports_get_the_event_they_are(config, title, expected):
    assert top_event(config, title) == expected


def test_a_subsidiary_folded_into_its_parent_is_not_a_takeover(config):
    title = "[MAYA] הסכם למיזוג מרכנתיל דיסקונט (בת בבעלות מלאה) עם ולתוך הבנק, המשך"
    assert "merger_acquisition" not in events(config, title)


def test_a_rating_on_commercial_paper_is_not_an_equity_raise(config):
    """Discount's CP rating read "גיוס ... 800מ'" and scored as dilution at 82."""
    title = "[MAYA] מידרוג קובעת דירוג P-1.il לגיוס נע\"מ 10 בעד 800מ'שח ע.נ"
    assert "equity_offering" not in events(config, title)


@pytest.mark.parametrize("title", [
    "[MAYA] מרשם בעלי מניות, שינויים ומצבת הון-מימוש אופציות עובדים ופקיעה",
    "[MAYA] מניות רדומות-שינוי במספר המניות",
    "[MAYA] שינוי החזקות בעלי עניין/נושאי משרה-דמרי יגאל",
    "[MAYA] פס\"ד המאשר הסתלקות מבקשה לייצוגית בק\"ע העמדת הלוואות לפירעון מיידי",
    "[MAYA] הנפקה פרטית של 6,615 מניות ל9 חברי דירקטוריון",
    "[MAYA] פדיון מוקדם מנדטורי חלקי של אג\"ח צמודות אשראי (סדרה 2-5)",
])
def test_routine_maya_paperwork_is_capped(config, title):
    from harel.enrich.materiality import MaterialityScorer

    cap = MaterialityScorer(config)._noise_cap(item(title))
    assert cap is not None and cap <= 20


# ---------------------------------------------------------------- names --- #
@pytest.fixture(scope="module")
def linker(config):
    return EntityLinker(config)


def tickers(linker, title: str, source: str = "google_news_he") -> set[str]:
    return {link.ticker for link in linker.link(item(title, source=source))}


@pytest.mark.parametrize("title,ticker", [
    ("ביטוח לאומי הודיע על הקדמת הקצבאות", "LUMI"),
    ("\"סגירת\" כביש לאומי 14D למשאיות גדולות", "LUMI"),
    ("הבחירות: בחירות בזק צפויות בחורף", "BEZQ"),
    ("יעל נכון הראל: תנורו של עכנאי", "HARL"),
    ("טקס הפתיחה של הכנס המדעי הבינלאומי ה-18", "FIBI"),
])
def test_an_ordinary_hebrew_word_is_not_the_company(linker, title, ticker):
    assert ticker not in tickers(linker, title)


@pytest.mark.parametrize("title,ticker", [
    ("בנק לאומי מדווח על רווח נקי של 2.5 מיליארד שקל", "LUMI"),
    ("לאומי הציג תשואה על ההון של 17%", "LUMI"),
    ("מניית בזק זינקה אחרי החלטת משרד התקשורת", "BEZQ"),
    ("מגדל ביטוח רוכשת את השליטה", "MGDL"),
])
def test_the_company_is_still_found_in_financial_grammar(linker, title, ticker):
    assert ticker in tickers(linker, title)


def test_a_us_namesake_symbol_is_not_a_tase_name(linker):
    """STRS is Stratus Properties and PHOE is Phoenix Asia in the SEC map."""
    found = tickers(linker, "Stratus Properties (STRS) and PHOE file 10-Q", "google_news")
    assert not found & {"STRS", "PHOE"}


# ------------------------------------------------------------- IR pages --- #
def test_numeric_dates_follow_the_declared_order():
    from harel.collect.ir_pages import _block_date

    assert _block_date("24/09/2026 Notice", "dmy") == date(2026, 9, 24)
    assert _block_date("12.08.26 Press release", "dmy") == date(2026, 8, 12)
    assert _block_date("08/19/2026 Kenon announces", "mdy") == date(2026, 8, 19)
    # A page that declares nothing keeps reading month names only.
    assert _block_date("08/05/2026 Kenon announces") is None


# ------------------------------------------------------------ BOI rate --- #
def _boi(config, db, payload):
    from harel.collect.boi import BoiInterestCollector

    client = FakeHttpClient({"PublicApi/GetInterest": payload})
    ctx = CollectorContext(config=config, client=client, db=db, lookback_hours=72)
    return BoiInterestCollector(config.sources["boi_interest"], ctx)


def test_the_next_rate_decision_is_a_bank_calendar_row(config, db):
    now = datetime.now(timezone.utc)
    payload = {"currentInterest": 3.25,
               "nextInterestDate": (now + timedelta(days=14)).strftime("%Y-%m-%dT00:00:00Z"),
               "lastPublishedDate": (now - timedelta(hours=2)).isoformat()}
    items = list(_boi(config, db, payload).collect())
    schedule = [i for i in items if i.meta.get("scheduled_kind") == "rate_decision"]
    assert schedule and "LUMI" in schedule[0].seed_tickers
    assert schedule[0].seed_relation == "SECTOR_REG"
    # Telecom does not list the Bank of Israel as its regulator.
    assert "BEZQ" not in schedule[0].seed_tickers


def test_a_decision_is_phrased_against_the_last_rate_seen(config, db):
    now = datetime.now(timezone.utc)
    first = {"currentInterest": 3.25, "nextInterestDate": None,
             "lastPublishedDate": (now - timedelta(days=30)).isoformat()}
    assert not list(_boi(config, db, first).collect()), \
        "a month-old decision is recorded, not emitted"
    cut = {"currentInterest": 3.0, "nextInterestDate": None,
           "lastPublishedDate": (now - timedelta(hours=1)).isoformat()}
    items = list(_boi(config, db, cut).collect())
    assert len(items) == 1 and "cuts its interest rate to 3.00%" in items[0].title
