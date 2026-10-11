"""Polymarket: Gamma `/events` parsing, market classification, storage, the crawl.

Payload builders mirror the shape `gamma-api.polymarket.com` served on 2026-10-10
(JSON-string `outcomes`/`outcomePrices`, decimal-string volumes, a game's `teams`,
`score`, `period`) with invented markets.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from conftest import NOW, Clock, FakeBrowser, IsolatedSettings, make_universe
from sqlalchemy import select

from social_scraper.config import SPORTS, STOCKS
from social_scraper.db import schema
from social_scraper.fetch import PageResult
from social_scraper.models import POLYMARKET
from social_scraper.polymarket import cycle, store
from social_scraper.polymarket.cycle import Plan, PolymarketCrawler, ids_url, scope_for, tag_url
from social_scraper.polymarket.parse import Market, parse_events, parse_market
from social_scraper.runner import policy_for
from social_scraper.sports.leagues import League
from social_scraper.throttle import Block, MemoryThrottleStore, Policy, Throttle

NHL = League(key="nhl", name="NHL", sport="hockey", polymarket_tags=("nhl",))
TENNIS = League(key="atp", name="ATP", sport="tennis", polymarket_tags=("atp", "tennis"))


def pm_market(market_id: str, question: str, **over: Any) -> dict[str, Any]:
    return {
        "id": market_id,
        "question": question,
        "slug": f"slug-{market_id}",
        "createdAt": "2026-06-12T19:23:42.611725Z",
        "outcomes": '["Yes", "No"]',
        "outcomePrices": '["0.0215", "0.9785"]',
        "lastTradePrice": 0.02,
        "bestBid": 0.021,
        "bestAsk": 0.022,
        "spread": 0.001,
        "volume": "43105.46494400001",
        "volumeNum": 43105.46494400001,
        "volume24hr": 7555.56,
        "liquidity": "59295.0707",
        "liquidityNum": 59295.0707,
        "endDate": "2027-07-01T03:59:00Z",
        "active": True,
        "closed": False,
        "submitted_by": "0xF43d000000000000000000000000000000000000",
        "resolvedBy": "0x69c4000000000000000000000000000000000000",
        **over,
    }


def pm_event(event_id: str, tags: tuple[str, ...], markets: list[dict[str, Any]], **over: Any):
    return {
        "id": event_id,
        "slug": f"event-{event_id}",
        "title": f"Event {event_id}",
        "createdAt": "2026-06-12T19:23:41Z",
        "startDate": "2026-06-12T19:43:46.737243Z",
        "endDate": "2027-07-01T03:59:00Z",
        "tags": [{"id": str(i), "slug": t, "label": t.upper()} for i, t in enumerate(tags)],
        "markets": markets,
        **over,
    }


def pm_game(event_id: str, **over: Any) -> dict[str, Any]:
    moneyline = pm_market(
        f"{event_id}-ml",
        "Red Wings vs. Canadiens",
        outcomes='["Red Wings", "Canadiens"]',
        outcomePrices='["0.415", "0.585"]',
        sportsMarketType="moneyline",
        gameStartTime="2026-10-10 23:00:00+00",
        endDate="2026-10-10T23:00:00Z",
    )
    state = {
        "gameId": "30025534",
        "teams": [
            {
                "id": 1,
                "name": "Red Wings",
                "abbreviation": "det",
                "league": "nhl",
                "record": "1-3-0",
                "logo": "https://example.invalid/det.png",
            },
            {
                "id": 2,
                "name": "Canadiens",
                "abbreviation": "mtl",
                "league": "nhl",
                "record": "3-1-0",
                "logo": "https://example.invalid/mtl.png",
            },
        ],
        "score": "0-0",
        "period": "End P2",
        "live": True,
        "ended": False,
        "startTime": "2026-10-10T23:00:00Z",
    }
    return pm_event(event_id, ("sports", "nhl", "games", "hockey"), [moneyline], **(state | over))


def earnings_event(event_id: str = "e-gs") -> dict[str, Any]:
    return pm_event(
        event_id,
        ("finance", "earnings", "equities", "gs"),
        [pm_market(f"{event_id}-m", "Will Goldman Sachs (GS) beat quarterly earnings?")],
        title="Will Goldman Sachs (GS) beat quarterly earnings?",
    )


# --- parse --------------------------------------------------------------------------------


def test_parse_market_decodes_json_strings_and_numbers():
    (m,) = parse_events([pm_event("e1", ("nhl", "sports"), [pm_market("m1", "Q?")])])
    assert m.market_id == "m1" and m.event_id == "e1" and m.question == "Q?"
    assert m.outcomes == ("Yes", "No") and m.prices == (0.0215, 0.9785)
    assert m.volume == pytest.approx(43105.46) and m.liquidity == pytest.approx(59295.07)
    assert m.tags == ("nhl", "sports")
    assert m.created_at == datetime(2026, 6, 12, 19, 23, 42, 611725, tzinfo=UTC)
    assert m.end_date == datetime(2027, 7, 1, 3, 59, tzinfo=UTC)


def test_parse_game_keeps_its_state_and_drops_logos():
    (m,) = parse_events([pm_game("g1")])
    assert m.sports_market_type == "moneyline" and m.game_id == "30025534"
    assert m.score == "0-0" and m.period == "End P2" and m.live is True and m.ended is False
    # The games' "2026-10-10 23:00:00+00" form, with an hour-only offset.
    assert m.start_time == datetime(2026, 10, 10, 23, 0, tzinfo=UTC)
    assert m.teams[0] == {
        "name": "Red Wings",
        "abbreviation": "det",
        "league": "nhl",
        "record": "1-3-0",
    }
    assert all("logo" not in t and "id" not in t for t in m.teams)


def test_parse_skips_markets_without_id_question_or_time_and_odd_entries():
    event = pm_event("e1", (), [pm_market("", "Q"), pm_market("m2", ""), "junk"])
    event["createdAt"] = None
    no_time = pm_market("m3", "Q", createdAt=None)
    assert parse_events([event, "junk", pm_event("e2", (), [no_time], createdAt=None)]) == []


def test_parse_market_tolerates_lists_bad_numbers_and_bad_json():
    m = parse_market(
        pm_market(
            "m1",
            "Q",
            outcomes=["A", "B"],
            outcomePrices="not json",
            lastTradePrice="x",
            bestBid=True,
            volumeNum=None,
            volume="",
        ),
        pm_event("e", (), []),
    )
    assert m is not None and m.outcomes == ("A", "B") and m.prices == ()
    assert m.last_trade_price is None and m.best_bid is None and m.volume is None


def test_parse_events_refuses_what_is_not_a_list():
    with pytest.raises(ValueError):
        parse_events({"type": "validation error", "error": "id is invalid"})


def test_winner_only_for_a_closed_market_its_prices_resolved():
    base = parse_market(pm_market("m", "Q", outcomePrices='["1", "0"]'), pm_event("e", (), []))
    assert base is not None and base.winner is None  # still open
    closed = parse_market(
        pm_market("m", "Q", closed=True, outcomePrices='["0", "1"]'), pm_event("e", (), [])
    )
    assert closed is not None and closed.winner == 1
    unresolved = parse_market(
        pm_market("m", "Q", closed=True, outcomePrices='["0.5", "0.5"]'), pm_event("e", (), [])
    )
    assert unresolved is not None and unresolved.winner is None


# --- classification and storage ------------------------------------------------------------


def scope(universe=None, stock_tags=("earnings", "stocks")) -> store.Scope:
    return store.Scope(frozenset(stock_tags), (NHL, TENNIS), universe)


def test_scope_assigns_domains_and_leagues_from_the_event_tags():
    s = scope()
    assert s.domains(("sports", "nhl")) == [SPORTS] and s.league_keys(("nhl",)) == ["nhl"]
    assert s.domains(("earnings", "gs")) == [STOCKS]
    assert s.domains(("earnings", "tennis")) == [STOCKS, SPORTS]
    assert s.league_keys(("tennis",)) == ["atp"] and s.domains(("politics",)) == []


def test_market_row_names_the_tickers_of_a_stock_market():
    (m,) = parse_events([earnings_event()])
    row = store.market_row(m, scope(universe=make_universe("GS", "JPM")), STOCKS)
    assert row["domains"] == [STOCKS] and row["platform"] == POLYMARKET
    assert row["symbols"] == ["GS"] and row["leagues"] == []
    assert "submitted_by" not in row and "resolvedBy" not in row


def test_market_symbols_come_from_text_and_ticker_tags(universe):
    m = Market("m", "e", "Will Nvidia (NVDA) close above $200?", NOW, tags=("tsla", "ai", "it"))
    assert store.market_symbols(m, universe) == ["NVDA", "TSLA"]  # `AI`/`IT` are stoplisted
    assert store.market_symbols(m, None) == []


def test_market_rows_fit_their_columns_whatever_gamma_sends():
    """2026-10-10, the first live cycle: Postgres refused a player-prop market, its
    `sportsMarketType` `baseball_player_earned_runs_allowed` longer than the column.
    SQLite never enforces a length, so this checks every bounded column itself."""
    long = "x" * 300
    m = Market(
        long,
        long,
        "Q",
        NOW,
        sports_market_type="baseball_player_earned_runs_allowed" + long,
        game_id=long,
        score=long,
        period=long,
    )
    row = store.market_row(m, scope(), SPORTS)
    prices = store.price_row(m)
    for table, values in ((schema.polymarket_markets, row), (schema.polymarket_prices, prices)):
        for column in table.columns:
            limit = getattr(column.type, "length", None)
            value = values.get(column.name)
            if limit and isinstance(value, str):
                assert len(value) <= limit, f"{table.name}.{column.name}"
    assert row["sports_market_type"].startswith("baseball_player_earned_runs_allowed")


def test_market_row_falls_back_to_the_domain_that_queried_it():
    m = Market("m", "e", "Q", NOW, tags=("politics",))
    assert store.market_row(m, scope(), SPORTS)["domains"] == [SPORTS]
    assert store.market_row(m, scope(), SPORTS)["symbols"] == []


def test_store_upserts_and_snapshots_only_when_the_price_moves(db):
    (m,) = parse_events([pm_game("g1")])
    s = scope()
    with db.begin() as conn:
        assert store.store(conn, [m], s, SPORTS, NOW) == (1, 1)
        # Same prices and game state an hour later: the market refreshes, no new snapshot.
        assert store.store(conn, [m], s, SPORTS, NOW + timedelta(hours=1)) == (1, 0)
    moved = parse_events([pm_game("g1", score="1-0")])
    with db.begin() as conn:
        assert store.store(conn, moved, s, SPORTS, NOW + timedelta(hours=2)) == (1, 1)
        markets = conn.execute(select(schema.polymarket_markets)).mappings().all()
        prices = conn.execute(select(schema.polymarket_prices)).mappings().all()
    assert len(markets) == 1 and markets[0]["score"] == "1-0" and markets[0]["leagues"] == ["nhl"]
    assert markets[0]["teams"][1]["abbreviation"] == "mtl"
    assert [p["score"] for p in sorted(prices, key=lambda p: p["observed_at"])] == ["0-0", "1-0"]


def test_upsert_market_refreshes_state_but_keeps_when_it_was_first_seen(db):
    (m,) = parse_events([pm_game("g1")])
    row = store.market_row(m, scope(), SPORTS)
    with db.begin() as conn:
        store.upsert_market(conn, row, NOW)
        store.upsert_market(conn, row | {"closed": True, "winner": 1}, NOW + timedelta(days=1))
        got = conn.execute(select(schema.polymarket_markets)).mappings().one()
    assert got["closed"] is True and got["winner"] == 1
    assert got["first_seen_at"].replace(tzinfo=UTC) == NOW
    assert got["updated_at"].replace(tzinfo=UTC) == NOW + timedelta(days=1)


def test_record_price_skips_a_market_with_no_price_at_all(engine):
    m = Market("m", "e", "Q", NOW)
    with engine.begin() as conn:
        assert store.record_price(conn, store.price_row(m), NOW) is False


def test_pending_events_are_the_overdue_open_ones_oldest_first(db):
    def market(mid, eid, end, closed=False):
        return Market(mid, eid, "Q", NOW, end_date=end, closed=closed, prices=(0.5, 0.5))

    rows = [
        market("a", "late", NOW - timedelta(hours=1)),
        market("b", "older", NOW - timedelta(days=2)),
        market("c", "done", NOW - timedelta(days=3), closed=True),
        market("d", "future", NOW + timedelta(days=1)),
    ]
    with db.begin() as conn:
        store.store(conn, rows, scope(), SPORTS, NOW)
        assert store.pending_events(conn, NOW, 10) == ["older", "late"]
        assert store.pending_events(conn, NOW, 1) == ["older"]
        assert store.counts(conn) == {"polymarket_markets": 4, "polymarket_prices": 4}


# --- the crawl ----------------------------------------------------------------------------


def throttle(clock: Clock, mem: MemoryThrottleStore | None = None) -> Throttle:
    policy = Policy(POLYMARKET, 0, 0, 1000, 10000)
    return Throttle(policy, mem or MemoryThrottleStore(), clock=clock, sleep=lambda s: None)


def ok(data: Any) -> PageResult:
    return PageResult(200, "u", data=data)


def test_tag_and_ids_urls():
    q = parse_qs(urlsplit(tag_url("nhl", 100)).query)
    assert q["tag_slug"] == ["nhl"] and q["closed"] == ["false"] and q["offset"] == ["100"]
    assert q["order"] == ["volume24hr"] and q["ascending"] == ["false"]
    assert parse_qs(urlsplit(ids_url(["1", "2"])).query)["id"] == ["1", "2"]
    assert urlsplit(tag_url("nhl", 0)).hostname == cycle.HOST


def test_crawl_reads_each_tag_once_and_stores_an_event_once(engine, clock):
    game = pm_game("g1")
    browser = FakeBrowser(
        {
            tag_url("earnings", 0): ok([earnings_event()]),
            tag_url("nhl", 0): ok([game]),
            tag_url("atp", 0): ok([]),
            tag_url("tennis", 0): ok([game]),  # the same event under another tag
        }
    )
    crawler = PolymarketCrawler(
        browser, throttle(clock), engine, Plan(scope(stock_tags=("earnings",))), clock=clock
    )
    stats = crawler.run()
    assert stats.errors == [] and stats.requests == 4
    assert stats.markets == 2 and stats.snapshots == 2
    assert sorted(browser.calls) == sorted(
        [tag_url("earnings", 0), tag_url("nhl", 0), tag_url("atp", 0), tag_url("tennis", 0)]
    )


def test_traded_needs_volume_past_the_floor():
    assert cycle.traded(Market("m", "e", "Q", NOW, volume_24hr=0.5), 0.0)
    assert not cycle.traded(Market("m", "e", "Q", NOW, volume_24hr=0.0), 0.0)
    assert not cycle.traded(Market("m", "e", "Q", NOW, volume_24hr=None), 0.0)
    assert not cycle.traded(Market("m", "e", "Q", NOW, volume_24hr=50.0), 100.0)


def test_crawl_stores_only_markets_traded_in_the_last_day(engine, clock):
    """2026-10-10: 9,472 of the NFL tag's 10,371 open markets had no 24-hour volume."""
    traded = pm_market("live", "Traded?", volume24hr=12.5)
    dead = pm_market("dead", "Dead?", volume24hr=0)
    unknown = pm_market("none", "Unknown?", volume24hr=None)
    browser = FakeBrowser(
        {tag_url("nhl", 0): ok([pm_event("e1", ("nhl",), [traded, dead, unknown])])}
    )
    s = store.Scope(frozenset(), (NHL,), None)
    stats = PolymarketCrawler(browser, throttle(clock), engine, Plan(s), clock=clock).run()
    assert stats.markets == 1
    with engine.connect() as conn:
        assert conn.execute(select(schema.polymarket_markets.c.market_id)).scalars().all() == [
            "live"
        ]


def test_crawl_reads_the_least_recently_read_tags_first(engine, clock):
    browser = FakeBrowser()
    browser.default = ok([])
    s = store.Scope(frozenset({"earnings", "stocks"}), (NHL, TENNIS), None)
    plan = Plan(s, tags_per_cycle=2)
    PolymarketCrawler(browser, throttle(clock), engine, plan, clock=clock).run()
    first = list(browser.calls)
    clock.now = NOW + timedelta(minutes=30)
    PolymarketCrawler(browser, throttle(clock), engine, plan, clock=clock).run()
    second = browser.calls[len(first) :]

    def tags(calls):
        return {parse_qs(urlsplit(u).query)["tag_slug"][0] for u in calls}

    assert len(first) == len(second) == 2
    assert tags(first).isdisjoint(tags(second))
    assert tags(first) | tags(second) == {"earnings", "stocks", "nhl", "atp"}


def test_crawl_pages_until_a_short_page(engine, clock):
    full = [pm_event(f"e{i}", ("nhl",), [pm_market(f"m{i}", "Q")]) for i in range(cycle.PAGE_SIZE)]
    browser = FakeBrowser({tag_url("nhl", 0): ok(full), tag_url("nhl", cycle.PAGE_SIZE): ok([])})
    s = store.Scope(frozenset(), (NHL,), None)
    stats = PolymarketCrawler(browser, throttle(clock), engine, Plan(s, 3), clock=clock).run()
    assert stats.requests == 2 and stats.markets == cycle.PAGE_SIZE


def test_crawl_resolves_overdue_events_by_id(engine, clock):
    s = store.Scope(frozenset(), (NHL,), None)
    with engine.begin() as conn:
        store.store(conn, parse_events([pm_game("g1")]), s, SPORTS, NOW - timedelta(days=1))
    clock.now = NOW + timedelta(days=30)
    final = pm_game("g1", score="3-1", period="VFT", live=False, ended=True)
    final["markets"][0] |= {"closed": True, "outcomePrices": '["0", "1"]'}
    browser = FakeBrowser({tag_url("nhl", 0): ok([]), ids_url(["g1"]): ok([final])})
    plan = Plan(s, resolve_limit=5)
    stats = PolymarketCrawler(browser, throttle(clock), engine, plan, clock=clock).run()
    assert isinstance(stats, cycle.PolymarketRunStats)
    assert stats.errors == [] and stats.resolved_lookups == 1
    with engine.connect() as conn:
        row = conn.execute(select(schema.polymarket_markets)).mappings().one()
    assert row["closed"] is True and row["winner"] == 1 and row["period"] == "VFT"


def test_crawl_records_an_error_page_and_goes_on(engine, clock):
    browser = FakeBrowser(
        {
            tag_url("earnings", 0): ok({"error": "bad request"}),
            tag_url("stocks", 0): ok([earnings_event()]),
        }
    )
    browser.default = ok([])  # the leagues' tags
    stats = PolymarketCrawler(browser, throttle(clock), engine, Plan(scope()), clock=clock).run()
    assert len(stats.errors) == 1 and "tag:earnings" in stats.errors[0]
    assert stats.markets == 1


def test_crawl_stops_on_a_rate_limit(engine, clock):
    mem = MemoryThrottleStore()
    browser = FakeBrowser({tag_url("earnings", 0): PageResult(429, "u", text="slow down")})
    browser.default = ok([earnings_event()])
    plan = Plan(scope())
    stats = PolymarketCrawler(browser, throttle(clock, mem), engine, plan, clock=clock).run()
    assert browser.calls == [tag_url("earnings", 0)] and stats.errors
    assert mem.get_block(POLYMARKET).blocked_until is not None


# --- run_platform -------------------------------------------------------------------------


def settings(**over: Any) -> IsolatedSettings:
    return IsolatedSettings(polymarket_min_delay_s=0, polymarket_max_delay_s=0, **over)


def test_scope_for_reads_only_the_enabled_domains(universe):
    s = settings(polymarket_stock_tags="Earnings, stocks")
    assert scope_for(s, None, universe).stock_tags == {"earnings", "stocks"}
    assert scope_for(s, (NHL,), None).stock_tags == frozenset()
    assert scope_for(s, None, None).leagues == ()


def test_run_platform_skips_when_there_is_nothing_to_read(engine):
    def never(platform):
        raise AssertionError("no client for nothing to read")

    result = cycle.run_platform(
        settings(polymarket_stock_tags=""), engine, never, policy_for, None, None
    )
    assert "no Polymarket tags" in result["skipped"]


def test_run_platform_crawls_and_reports(engine):
    browser = FakeBrowser()
    browser.default = ok([earnings_event()])
    s = settings(polymarket_stock_tags="earnings", polymarket_resolve_per_cycle=0)
    result = cycle.run_platform(s, engine, lambda p: browser, policy_for, None, make_universe("GS"))
    assert result["markets"] == 1 and result["requests"] == 1 and "error" not in result
    with engine.connect() as conn:
        row = conn.execute(select(schema.polymarket_markets)).mappings().one()
    assert row["symbols"] == ["GS"] and row["domains"] == [STOCKS]


def test_run_platform_skips_a_paused_platform_and_reports_errors(engine, universe):
    from social_scraper.db.throttle_store import DbThrottleStore

    s = settings(polymarket_stock_tags="earnings")
    DbThrottleStore(engine).set_block(
        POLYMARKET, Block(datetime.now(UTC) + timedelta(hours=1), "429", 1), datetime.now(UTC)
    )
    assert (
        "paused"
        in cycle.run_platform(s, engine, lambda p: FakeBrowser(), policy_for, None, universe)[
            "skipped"
        ]
    )
    DbThrottleStore(engine).set_block(POLYMARKET, Block(None, None, 0), datetime.now(UTC))
    browser = FakeBrowser()
    browser.default = ok({"error": "x"})
    result = cycle.run_platform(s, engine, lambda p: browser, policy_for, None, universe)
    assert "unparseable" in result["error"]


def test_run_platform_turns_an_os_error_into_a_result(engine, universe):
    class Broken:
        def __enter__(self):
            raise OSError("no network")

        def __exit__(self, *exc):
            return None

    s = settings(polymarket_stock_tags="earnings")
    result = cycle.run_platform(s, engine, lambda p: Broken(), policy_for, None, universe)
    assert result["error"].startswith("OSError")


def test_teams_survive_the_round_trip_as_json(engine):
    with engine.begin() as conn:
        store.store(conn, parse_events([pm_game("g1")]), scope(), SPORTS, NOW)
        teams = conn.execute(select(schema.polymarket_markets.c.teams)).scalar_one()
    assert json.loads(json.dumps(teams))[0]["name"] == "Red Wings"
