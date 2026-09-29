from conftest import make_universe

from social_scraper.symbols.extract import (
    BARE,
    CASHTAG,
    STOPLIST,
    TAGGED,
    Mention,
    extract_mentions,
    is_shouting,
    validated_symbols,
)

U = make_universe("NVDA", "AMD", "IT", "ON", "BRK-B", "QQQ", "F", "DD", "CEO")


def symbols(text, tagged=()):
    return {(m.symbol, m.match_type, m.in_universe) for m in extract_mentions(text, U, tagged)}


def test_cashtags_are_found_case_insensitively_and_normalised():
    assert symbols("loading $nvda and $BRK.B calls") == {
        ("NVDA", CASHTAG, True),
        ("BRK-B", CASHTAG, True),
    }


def test_cashtag_outside_universe_is_kept_but_flagged():
    # Discovery: a fresh IPO or crypto ticker the directory lacks.
    assert ("NEWCO", CASHTAG, False) in symbols("anyone looking at $NEWCO?")


def test_dollar_amounts_are_not_cashtags():
    assert symbols("up $100 today, $5k profit, $660p puts, $2T IPO") == set()


def test_bare_words_need_universe_and_must_not_be_stoplisted():
    found = symbols("NVDA and AMD ripping, IT and ON too, DD inside from the CEO, ZZZZ")
    assert found == {("NVDA", BARE, True), ("AMD", BARE, True)}


def test_stoplisted_ticker_still_counts_as_cashtag():
    assert "IT" in STOPLIST
    assert symbols("$IT beat earnings") == {("IT", CASHTAG, True)}


def test_single_letter_tickers_only_via_cashtag():
    assert symbols("F is cheap") == set()
    assert symbols("$F is cheap") == {("F", CASHTAG, True)}


def test_bare_word_ignores_mid_token_and_lowercase():
    assert symbols("nvda, NVDA-ish, www.NVDA.com") == set()


def test_shouting_disables_bare_words_but_not_cashtags():
    text = "THIS MARKET IS INSANE BUY NVDA NOW AND $AMD BEFORE IT RIPS TO THE MOON"
    assert is_shouting(text)
    assert symbols(text) == {("AMD", CASHTAG, True)}
    assert not is_shouting("Short but LOUD")


def test_shouting_title_over_a_normal_body_is_judged_per_line():
    u = make_universe("JUST", "NVDA", "AMD")
    text = (
        "A LOT OF YOU ASKED ABOUT THE NVDA POSITION TODAY\nHonestly I think AMD is the better buy."
    )
    found = {m.symbol for m in extract_mentions(text, u)}
    assert found == {"AMD"}


def test_jargon_seen_in_live_data_is_stoplisted():
    u = make_universe("ITM", "API", "AGI", "SOFR", "CIA", "SEPT", "NVDA")
    text = "NVDA calls ITM, the API and AGI hype, SOFR options, CIA director, SEPT 1ST"
    assert {m.symbol for m in extract_mentions(text, u)} == {"NVDA"}


def test_tagged_symbols_win_precedence():
    assert symbols("$NVDA NVDA", tagged=("nvda",)) == {("NVDA", TAGGED, True)}


def test_validated_symbols_sorted_and_filtered():
    ms = [Mention("NVDA", BARE, True), Mention("XYZ", CASHTAG, False), Mention("AMD", BARE, True)]
    assert validated_symbols(ms) == ["AMD", "NVDA"]
