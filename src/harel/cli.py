"""Command line interface.

    harel doctor              config + source health, missing keys, gaps
    harel collect             one collection pass
    harel watch               keep collecting on an interval
    harel morning             overnight digest
    harel feed                ranked feed
    harel brief TEVA          one name, direct + indirect
    harel search "potash"     full text
    harel moving              price movers and their causes
    harel serve               REST API + HTML terminal on 127.0.0.1:8787
    harel mcp                 MCP server over stdio for an LLM agent
    harel export out.html     static snapshot of the terminal
    harel probe-maya          verify the TASE/MAYA endpoint shape
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import datetime, timezone
from typing import Any

from .config import get_config
from .db import Database
from .pipeline import Pipeline
from .views import Views

# ANSI. Disabled automatically when stdout is not a tty.
class C:
    RESET = "\033[0m"; DIM = "\033[2m"; BOLD = "\033[1m"
    AMBER = "\033[38;5;214m"; RED = "\033[38;5;203m"; GREEN = "\033[38;5;114m"
    BLUE = "\033[38;5;81m"; PURPLE = "\033[38;5;141m"; GREY = "\033[38;5;244m"

    @classmethod
    def off(cls) -> None:
        for name in dir(cls):
            if name.isupper():
                setattr(cls, name, "")


TIER_COLOR = {"ALERT": C.RED, "HIGH": C.AMBER, "NORMAL": C.RESET, "NOISE": C.GREY}
REL_COLOR = {
    "DIRECT": C.AMBER, "SUBSIDIARY": C.AMBER, "PRODUCT_RIVAL": C.BLUE,
    "CUSTOMER": C.BLUE, "PEER": C.GREEN, "SECTOR_REG": C.PURPLE,
    "SECTOR_THEME": C.GREY, "MACRO": C.GREY,
}


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if not sys.stdout.isatty() or getattr(args, "no_color", False):
        C.off()
        TIER_COLOR.update({k: "" for k in TIER_COLOR})
        REL_COLOR.update({k: "" for k in REL_COLOR})

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    # requests/urllib3 chatter is noise at INFO.
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    handler = getattr(args, "handler", None)
    if handler is None:
        parser.print_help()
        return 1
    return handler(args) or 0


# --------------------------------------------------------------------------- #
def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="harel", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db", default=None, help="path to the SQLite file")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--no-color", action="store_true")
    p.add_argument("--json", action="store_true", help="emit raw JSON")
    sub = p.add_subparsers(dest="command")

    d = sub.add_parser("doctor", help="config and source health")
    d.set_defaults(handler=cmd_doctor)

    c = sub.add_parser("collect", help="one collection pass")
    c.add_argument("--sources", default=None, help="comma-separated source keys")
    c.add_argument("--hours", type=float, default=72.0, help="lookback window")
    c.set_defaults(handler=cmd_collect)

    w = sub.add_parser("watch", help="collect on an interval, forever")
    w.add_argument("--interval", type=int, default=300, help="seconds between passes")
    w.add_argument("--hours", type=float, default=12.0)
    w.add_argument("--sources", default=None)
    w.set_defaults(handler=cmd_watch)

    m = sub.add_parser("morning", help="overnight digest")
    m.add_argument("--hours", type=float, default=16.0)
    m.set_defaults(handler=cmd_morning)

    f = sub.add_parser("feed", help="ranked feed")
    f.add_argument("--tickers", default=None)
    f.add_argument("--min-score", type=float, default=45.0)
    f.add_argument("--hours", type=float, default=24.0)
    f.add_argument("--limit", type=int, default=40)
    f.add_argument("--relations", default=None,
                   help="e.g. DIRECT  or  PRODUCT_RIVAL,PEER")
    f.add_argument("--events", default=None)
    f.add_argument("--why", action="store_true", help="show the scoring trace")
    f.add_argument("--tape", action="store_true",
                   help="include [TAPE] unexplained-move markers (see `harel moving`)")
    f.set_defaults(handler=cmd_feed)

    b = sub.add_parser("brief", help="one name in full")
    b.add_argument("ticker")
    b.add_argument("--hours", type=float, default=48.0)
    b.set_defaults(handler=cmd_brief)

    s = sub.add_parser("search", help="full-text search")
    s.add_argument("query")
    s.add_argument("--limit", type=int, default=30)
    s.add_argument("--hours", type=float, default=None)
    s.set_defaults(handler=cmd_search)

    mv = sub.add_parser("moving", help="price movers and their causes")
    mv.add_argument("--min-pct", type=float, default=2.0)
    mv.set_defaults(handler=cmd_moving)

    sv = sub.add_parser("serve", help="REST API + HTML terminal")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8787)
    sv.set_defaults(handler=cmd_serve)

    mc = sub.add_parser("mcp", help="MCP server over stdio")
    mc.set_defaults(handler=cmd_mcp)

    ex = sub.add_parser("export", help="static HTML snapshot")
    ex.add_argument("out")
    ex.set_defaults(handler=cmd_export)

    pm = sub.add_parser("probe-maya", help="verify the TASE/MAYA endpoint shape")
    pm.set_defaults(handler=cmd_probe_maya)

    vf = sub.add_parser("verify-feeds",
                        help="check every configured RSS feed and report dead ones")
    vf.set_defaults(handler=cmd_verify_feeds)

    return p


# --------------------------------------------------------------------------- #
def _views(args) -> Views:
    return Views(db=Database(args.db), config=get_config())


def _emit(args, payload: Any) -> bool:
    """Print JSON and return True when --json was requested."""
    if getattr(args, "json", False):
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        return True
    return False


def cmd_doctor(args) -> int:
    cfg = get_config()
    views = _views(args)
    health = views.health()
    if _emit(args, health):
        return 0

    print(f"{C.BOLD}{C.AMBER}HAREL TERMINAL - doctor{C.RESET}")
    print(f"  universe          {len(cfg.active_tickers)} active, "
          f"{len(cfg.unresolved_tickers)} unresolved")
    print(f"  sources           {health['sources_available']}/"
          f"{health['sources_configured']} available")
    print(f"  database          {health['db']['items']} items, "
          f"{health['db']['links']} ticker links, "
          f"{health['db']['alerts_24h']} alerts in 24h")

    if health["unresolved_tickers"]:
        print(f"\n{C.RED}Unresolved tickers (NOT collected):{C.RESET}")
        for entry in health["unresolved_tickers"]:
            print(f"  {entry['ticker']:<6} {entry['hint']}")

    if health["missing_api_keys"]:
        print(f"\n{C.AMBER}Sources off - missing API keys:{C.RESET}")
        for entry in health["missing_api_keys"]:
            print(f"  {entry['source']:<22} set {entry['env_var']}")

    degraded = health["degraded_sources"]
    if degraded:
        print(f"\n{C.AMBER}Degraded sources:{C.RESET}")
        for state in degraded[:15]:
            print(f"  {state['source']:<40} fails={state.get('consecutive_failures')} "
                  f"{C.GREY}{(state.get('last_error') or '')[:90]}{C.RESET}")

    by_source = health["db"]["by_source"]
    if by_source:
        print(f"\n{C.GREY}Items by source:{C.RESET}")
        for source, count in list(by_source.items())[:20]:
            print(f"  {source:<40} {count}")
    else:
        print(f"\n{C.GREY}No items yet. Run: harel collect{C.RESET}")
    return 0


def cmd_collect(args) -> int:
    pipeline = Pipeline(db=Database(args.db), lookback_hours=args.hours)
    only = [s.strip() for s in args.sources.split(",")] if args.sources else None
    report = pipeline.run(only=only)
    if _emit(args, report.to_dict()):
        return 0

    print(f"{C.AMBER}collected{C.RESET} {report.collected}  "
          f"{C.AMBER}stored{C.RESET} {report.stored}  "
          f"{C.GREY}dropped (no universe link){C.RESET} {report.deduped}  "
          f"in {report.duration_sec:.1f}s")
    for source, count in sorted(report.by_source.items(), key=lambda kv: -kv[1]):
        print(f"  {source:<40} {count}")
    for warning in report.warnings[:25]:
        print(f"  {C.AMBER}warn{C.RESET} {warning}")
    for error in report.errors[:25]:
        print(f"  {C.RED}err {C.RESET} {error}")
    if report.alerts:
        print(f"\n{C.RED}{C.BOLD}ALERTS{C.RESET}")
        _print_items(report.alerts)
    return 1 if report.errors else 0


def cmd_watch(args) -> int:
    only = [s.strip() for s in args.sources.split(",")] if args.sources else None
    pipeline = Pipeline(db=Database(args.db), lookback_hours=args.hours)
    # This loop is meant to run as a service, where stdout is a pipe and Python
    # block-buffers it. Without an explicit flush the per-pass line and, worse,
    # the ALERT lines sit in a 4KB buffer instead of reaching the operator - the
    # one output in this whole program that must never be delayed.
    print(f"{C.AMBER}watching{C.RESET} every {args.interval}s - Ctrl-C to stop",
          flush=True)
    while True:
        try:
            report = pipeline.run(only=only)
            stamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
            print(f"[{stamp}] {report.collected} collected, {report.stored} stored, "
                  f"{len(report.alerts)} alerts, {len(report.errors)} errors",
                  flush=True)
            for alert in report.alerts[:5]:
                print(f"  {C.RED}ALERT{C.RESET} {alert['ticker']:<6} "
                      f"{alert['title'][:110]}", flush=True)
        except KeyboardInterrupt:
            print("\nstopped", flush=True)
            return 0
        except Exception as exc:
            print(f"{C.RED}pass failed:{C.RESET} {exc}", flush=True)
        time.sleep(args.interval)


def cmd_morning(args) -> int:
    brief = _views(args).morning_brief(hours=args.hours)
    if _emit(args, brief):
        return 0

    print(f"{C.BOLD}{C.AMBER}MORNING BRIEF{C.RESET} "
          f"{C.GREY}last {args.hours:.0f}h{C.RESET}\n")
    for warning in brief["coverage_warnings"]:
        print(f"  {C.AMBER}!{C.RESET} {warning}")
    if brief["coverage_warnings"]:
        print()

    _titled("ALERTS", brief["alerts"])
    _titled("HIGH", brief["high"])
    _titled("TASE / MAYA OVERNIGHT", brief["tase_overnight"])

    if brief["movers"]:
        print(f"{C.BOLD}MOVERS{C.RESET}")
        for m in brief["movers"]:
            color = C.GREEN if m["change_pct"] >= 0 else C.RED
            tag = "" if m["explained"] else f"  {C.AMBER}<- unexplained{C.RESET}"
            print(f"  {m['ticker']:<6} {color}{m['change_pct']:+6.2f}%{C.RESET}{tag}")
        print()
    return 0


def cmd_feed(args) -> int:
    result = _views(args).feed(
        tickers=_split(args.tickers), min_score=args.min_score, hours=args.hours,
        limit=args.limit, relations=_split(args.relations),
        events=_split(args.events), include_reasons=args.why,
        include_tape=args.tape,
    )
    if _emit(args, result):
        return 0
    _print_items(result["items"], show_reasons=args.why)
    if not result["items"]:
        print(f"{C.GREY}nothing at score >= {args.min_score} in the last "
              f"{args.hours:.0f}h. Try --min-score 20, or run `harel collect`.{C.RESET}")
    return 0


def cmd_brief(args) -> int:
    brief = _views(args).ticker_brief(args.ticker, hours=args.hours)
    if _emit(args, brief):
        return 0
    if "error" in brief:
        print(f"{C.RED}{brief['error']}{C.RESET}")
        if brief.get("hint"):
            print(f"  {brief['hint']}")
        return 1

    print(f"{C.BOLD}{C.AMBER}{brief['ticker']}{C.RESET} - {brief['name']}")
    print(f"{C.GREY}{brief['sector']} | float {brief['float_class']} | "
          f"{brief['exchange']}{' | TASE dual-listed' if brief['dual_listed_tase'] else ''}"
          f"{C.RESET}")
    if brief["risk_flags"]:
        print(f"{C.AMBER}risk flags: {', '.join(brief['risk_flags'])}{C.RESET}")

    price = brief.get("price")
    if price and price.get("change_pct") is not None:
        color = C.GREEN if price["change_pct"] >= 0 else C.RED
        vol_mult = price.get("vol_mult")
        vol_text = f"{vol_mult:.1f}x" if vol_mult else "n/a"
        print(f"last {price.get('last')}  "
              f"{color}{price['change_pct']:+.2f}%{C.RESET}  vol {vol_text}")
    print()

    _titled("DIRECT", brief["direct_news"])
    _titled("INDIRECT / READ-ACROSS", brief["indirect_news"])
    if brief["calendar"]:
        print(f"{C.BOLD}CALENDAR{C.RESET}")
        for entry in brief["calendar"]:
            print(f"  {entry['date']}  {entry['kind']:<18} {entry['label']}")
    return 0


def cmd_search(args) -> int:
    result = _views(args).search(args.query, limit=args.limit, hours=args.hours)
    if _emit(args, result):
        return 0
    if "error" in result:
        print(f"{C.RED}{result['error']}{C.RESET}\n{result.get('hint', '')}")
        return 1
    _print_items(result["items"])
    return 0


def cmd_moving(args) -> int:
    result = _views(args).whats_moving(min_abs_pct=args.min_pct)
    if _emit(args, result):
        return 0
    for m in result["movers"]:
        color = C.GREEN if m["change_pct"] >= 0 else C.RED
        vol = f"{m['volume_multiple']:.1f}x" if m.get("volume_multiple") else "  -  "
        print(f"{m['ticker']:<6} {color}{m['change_pct']:+6.2f}%{C.RESET}  vol {vol}")
        if m["drivers"]:
            for driver in m["drivers"][:2]:
                print(f"       {C.GREY}{driver['title'][:120]}{C.RESET}")
        else:
            print(f"       {C.AMBER}no matching news{C.RESET}")
    if not result["movers"]:
        print(f"{C.GREY}nothing moving more than {args.min_pct}%. "
              f"Run `harel collect --sources prices_stooq` first.{C.RESET}")
    return 0


def cmd_serve(args) -> int:  # pragma: no cover
    try:
        import uvicorn
    except ImportError:
        print("uvicorn is not installed. Run: pip install 'harel-terminal[serve]'")
        return 1
    from .serve.api import create_app

    # Same reason as cmd_watch: under a service manager stdout is a pipe, and
    # without a flush this banner sits in the buffer - so a log tail shows an
    # empty file whether the server is starting, up, or wedged.
    print(f"{C.AMBER}Harel Terminal{C.RESET} http://{args.host}:{args.port}", flush=True)
    uvicorn.run(create_app(args.db), host=args.host, port=args.port, log_level="warning")
    return 0


def cmd_mcp(args) -> int:  # pragma: no cover
    from .serve.mcp_server import main as mcp_main

    mcp_main(args.db)
    return 0


def cmd_export(args) -> int:
    from .serve.terminal import render_terminal

    html_text = render_terminal(_views(args))
    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write(html_text)
    print(f"wrote {args.out} ({len(html_text)} bytes)")
    return 0


def _mask_secrets(headers: dict) -> dict:
    """Keep credentials out of terminal scrollback and pasted bug reports."""
    out = dict(headers)
    for key in ("apiKey", "apikey", "Authorization"):
        if out.get(key):
            out[key] = f"...{str(out[key])[-4:]}"
    return out


def cmd_probe_maya(args) -> int:
    """Hit the configured MAYA endpoint for one dual-listed name and report what
    actually came back. Run this once after deployment - the public endpoints
    are undocumented and this is how you find out they moved."""
    from .collect.base import CollectorContext
    from .collect.maya import MayaCollector, _find_records
    from .http import HttpClient

    cfg = get_config()
    source = cfg.sources.get("maya_tase")
    if source is None:
        print(f"{C.RED}no maya_tase source configured{C.RESET}")
        return 1

    db = Database(args.db)
    client = HttpClient(user_agent=cfg.user_agent())
    collector = MayaCollector(source, CollectorContext(config=cfg, client=client, db=db))

    probe = next(
        (t for t in cfg.active_tickers if cfg.ticker(t) and cfg.ticker(t).tase_id), None
    )
    if probe is None:
        print(f"{C.RED}no ticker has a tase_id set{C.RESET}")
        return 1

    tc = cfg.ticker(probe)
    tase_id = str(tc.tase_id)
    issuer_id = tc.raw.get("tase_issuer_id")
    if source.api_key and not issuer_id:
        print(f"{C.RED}{probe} has no tase_issuer_id.{C.RESET}")
        print(f"  The official v2 API keys on issuer number; tase_id ({tase_id}) is a")
        print("  security id. Add `tase_issuer_id` to universe.yaml for this name.")
        return 1

    url, headers, params = collector._build_request(tc, issuer_id)
    print(f"probing {probe} (TASE {tase_id})\n  GET {url}")
    if params:
        print(f"  params:  {params}")
    print(f"  headers: {_mask_secrets(headers)}")
    print(f"  auth: {'official API key' if source.api_key else 'public endpoint (undocumented)'}")

    try:
        resp = client.get(url, headers=headers, params=params,
                          allow_status=(400, 401, 403, 404, 429, 500))
    except Exception as exc:
        print(f"{C.RED}request failed: {exc}{C.RESET}")
        return 1

    print(f"  HTTP {resp.status}, {len(resp.content)} bytes")
    if resp.status >= 400:
        print(f"{C.RED}endpoint rejected the request.{C.RESET}")
        if resp.status == 401:
            print("  401 - the apiKey header was missing or is not a valid key.")
        elif resp.status == 403 and source.api_key:
            print("  403 - the key authenticates, but the MAYA product is not active")
            print("  for it. Check the product status in the TASE developer portal;")
            print("  a subscription sitting at PENDING returns exactly this.")
        elif resp.status == 403:
            print("  403 - the undocumented public endpoint refused us, which is now")
            print("  its normal state. The supported route is the official API: set")
            print("  TASE_API_KEY once the MAYA product subscription is active.")
        else:
            print("  Update sources.yaml -> maya_tase.official_endpoint (official)")
            print("  or maya_tase.endpoints.company_reports (public fallback).")
        return 1

    try:
        payload = resp.json()
    except Exception:
        print(f"{C.RED}response was not JSON:{C.RESET} {resp.text[:300]}")
        return 1

    records = _find_records(payload)
    print(f"  parsed {len(records)} record-like objects")
    if records:
        print(f"  first record keys: {sorted(records[0])[:20]}")
        item = collector._record_to_item(records[0], probe, tase_id)
        if item:
            print(f"  {C.GREEN}OK{C.RESET} -> {item.published_at.isoformat()} | {item.title[:110]}")
        else:
            print(f"  {C.AMBER}records found but fields did not map.{C.RESET}")
            print("  Add the real key names to TITLE_KEYS/DATE_KEYS in collect/maya.py")
    else:
        print(f"  {C.AMBER}no record-like objects found. Top-level shape:{C.RESET} "
              f"{type(payload).__name__} {list(payload)[:15] if isinstance(payload, dict) else ''}")
    return 0


def cmd_verify_feeds(args) -> int:
    """Check every RSS URL in the config and say which ones actually work.

    The IR feed URLs in universe.yaml are educated guesses about each company's
    site layout. Some will be wrong. This finds them in one pass instead of one
    warning at a time.
    """
    import feedparser

    from .http import HttpClient

    cfg = get_config()
    # No retries here: this is a reachability check over ~40 URLs, and backing
    # off three times per dead feed turns a 30-second check into ten minutes.
    client = HttpClient(user_agent=cfg.user_agent(), timeout=10, max_retries=0)

    targets: list[tuple[str, str]] = []
    for ticker in cfg.active_tickers:
        tc = cfg.ticker(ticker)
        for url in (tc.ir_feeds if tc else []):
            targets.append((f"{ticker} IR", url))
    for source in cfg.sources.values():
        for url in source.feeds:
            targets.append((source.key, url))

    ok = dead = empty = 0
    results: list[tuple[str, str, str, str]] = []

    for label, url in targets:
        try:
            resp = client.get(url, allow_status=(400, 401, 403, 404, 410, 429, 500, 503))
        except Exception as exc:
            results.append((label, url, "ERROR", f"{type(exc).__name__}: {exc}"))
            dead += 1
            continue
        if resp.status >= 400:
            results.append((label, url, "DEAD", f"HTTP {resp.status}"))
            dead += 1
            continue
        parsed = feedparser.parse(resp.content)
        entries = len(parsed.entries)
        if entries == 0:
            results.append((label, url, "EMPTY", "parsed, but zero entries"))
            empty += 1
        else:
            newest = ""
            if parsed.entries and parsed.entries[0].get("published"):
                newest = str(parsed.entries[0]["published"])[:31]
            results.append((label, url, "OK", f"{entries} entries, newest {newest}"))
            ok += 1

    for label, url, status, detail in results:
        color = {"OK": C.GREEN, "EMPTY": C.AMBER}.get(status, C.RED)
        print(f"{color}{status:<6}{C.RESET} {label:<24} {detail}")
        if status != "OK":
            print(f"       {C.GREY}{url}{C.RESET}")

    print(f"\n{C.GREEN}{ok} ok{C.RESET}, {C.AMBER}{empty} empty{C.RESET}, "
          f"{C.RED}{dead} dead{C.RESET}, of {len(targets)} feeds")
    if dead or empty:
        print(f"{C.GREY}Fix the URLs in config/universe.yaml (ir_feeds) or "
              f"config/sources.yaml (feeds). Google News still covers any name "
              f"whose IR feed is dead, just with lower trust.{C.RESET}")
    return 0


# --------------------------------------------------------------------------- #
def _titled(title: str, items: list[dict[str, Any]]) -> None:
    if not items:
        return
    print(f"{C.BOLD}{title}{C.RESET}")
    _print_items(items)
    print()


def _print_items(items: list[dict[str, Any]], show_reasons: bool = False) -> None:
    for it in items:
        tier = TIER_COLOR.get(it.get("tier", "NORMAL"), "")
        rel = it.get("relation", "")
        rel_color = REL_COLOR.get(rel, C.GREY)
        corr = f" x{it['corroboration']}" if it.get("corroboration") else ""
        print(f"  {tier}{it['score']:5.1f}{C.RESET} "
              f"{C.AMBER}{it.get('ticker', ''):<6}{C.RESET}"
              f"{rel_color}{rel:<14}{C.RESET}"
              f"{C.GREY}{_ago(it['t']):>9}{C.RESET}  {it['title'][:118]}")
        detail = f"        {C.GREY}{it['source']}{corr}"
        if it.get("events"):
            detail += f" | {','.join(it['events'][:3])}"
        if it.get("why"):
            detail += f" | {it['why'][:80]}"
        print(detail + C.RESET)
        if it.get("url"):
            print(f"        {C.GREY}{it['url'][:150]}{C.RESET}")
        if show_reasons:
            for reason in (it.get("reasons") or [])[:12]:
                print(f"          {C.GREY}. {reason}{C.RESET}")


def _ago(iso: str) -> str:
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return iso[:10]
    minutes = (datetime.now(timezone.utc) - dt).total_seconds() / 60
    if minutes < 60:
        return f"{int(minutes)}m"
    if minutes < 60 * 48:
        return f"{int(minutes // 60)}h"
    return f"{int(minutes // 1440)}d"


def _split(value: str | None) -> list[str] | None:
    if not value:
        return None
    return [v.strip().upper() for v in value.split(",") if v.strip()]


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
