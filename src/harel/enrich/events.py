"""Event classification.

Maps an item onto the event taxonomy in config/scoring.yaml. An item can carry
several event types (an 8-K can be both `earnings` and `guidance_change`); we
keep all of them, because the LLM agent downstream reasons better with the full
label set than with a single winner.
"""

from __future__ import annotations

from ..config import Config, EventRule
from ..models import RawItem


def classify_events(item: RawItem, config: Config) -> list[tuple[EventRule, str]]:
    """Return [(rule, evidence)] sorted by base score, highest first."""
    text = item.text
    form_type = str(item.meta.get("form_type") or "").upper()
    hits: list[tuple[EventRule, str]] = []

    for rule in config.scoring.events:
        evidence = _match(rule, text, form_type)
        if evidence:
            hits.append((rule, evidence))

    hits.sort(key=lambda pair: pair[0].base, reverse=True)
    return hits


# Forms that carry no event meaning on their own. An 8-K is a *container*; what
# makes it material is its Item code, which the EDGAR collector surfaces
# separately as `item_severity`. Letting a generic form stand alone would tag
# every routine filing with whatever rule happened to list it.
GENERIC_FORMS = {"8-K", "6-K", "8-K/A", "6-K/A", "10-K", "10-Q", "20-F", "40-F"}

# Rules whose form types ARE unambiguous evidence by themselves.
FORM_STANDALONE_RULES = {"equity_offering", "listing_compliance", "insider_activity"}


def _match(rule: EventRule, text: str, form_type: str) -> str | None:
    for pattern in rule.patterns:
        found = pattern.search(text)
        if found:
            snippet = found.group(0).strip()
            return f"matched {snippet!r}"

    if (
        form_type
        and form_type in rule.form_types
        and form_type not in GENERIC_FORMS
        and rule.key in FORM_STANDALONE_RULES
    ):
        return f"form type {form_type}"
    return None


def event_labels(hits: list[tuple[EventRule, str]]) -> list[str]:
    return [rule.key for rule, _ in hits]
