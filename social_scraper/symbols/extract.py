"""Find stock symbols in free text.

Two kinds of evidence, kept apart so a consumer can choose its own precision:

- **cashtag** -- `$NVDA`, `$brk.b`. The author said "this is a ticker". Kept even when
  the symbol is not in the universe (`in_universe=False`): that is how a fresh IPO or a
  symbol the directory lacks gets *discovered*.
- **bare** -- `NVDA` as an upper-case word. Only counted when it is in the universe,
  at least two letters, not a common word or trading slang (the stoplist), and the
  text is not shouting (an all-caps post turns every word into a candidate).
- **tagged** -- a symbol the platform itself attached (X `entities.symbols`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from social_scraper.symbols.universe import Universe, normalize_symbol

CASHTAG = "cashtag"
BARE = "bare"
TAGGED = "tagged"

# `$` not preceded by a word char or another `$`; 1-5 letters, optional class suffix;
# not followed by a letter/digit (so `$100`, `$AAPL123` and `$5k` never match).
_CASHTAG_RE = re.compile(r"(?<![\w$])\$([A-Za-z]{1,5}(?:[.\-][A-Za-z])?)(?![\w])")
_BARE_RE = re.compile(r"(?<![\w$./-])([A-Z]{2,5})(?![\w/-])")
_LETTERS_RE = re.compile(r"[A-Za-z]")
# Above this share of upper-case letters the text is shouting, and bare words mean nothing.
SHOUTING_RATIO = 0.6
SHOUTING_MIN_LETTERS = 20

# Real tickers that are also English words, acronyms or trading slang. A bare match on
# one of these is noise far more often than signal; `$IT` still counts. The second
# block came from auditing a live scrape of r/wallstreetbets and r/stocks (2026-09-28):
# each was a real listed symbol matched as jargon ("ITM", "API", "AGI", "SOFR", "CIA").
STOPLIST = frozenset(
    """
    A ACT ADD AGO AI AIR ALL ALLY AM AMP AN AND ANY APP ARE ARM AS AT ATH ATM AVG AWAY
    BAND BE BEAT BEST BIG BILL BIT BOX BRO BUY BY CAN CAR CASH CAT CC CEO CFO CHEF CPI
    CTO CUT DAY DD DO DOW DTE EARN EAT EDGE EOD EOY EPS ETF EU EV EVER EXP FAST FED FOR
    FUN FUND GAIN GDP GO GOOD GOLD GROW GUT HAS HE HOLD HOPE HUGE ICE IMO INFO IPO IQ IRS
    IS IT JOB JOBS KEY KIDS LIFE LIVE LOL LOVE LOW MAN MAX ME MOM MORE MOVE NET NEW NEXT
    NICE NOW NYSE OF OK ON ONE OPEN OR OUT OWN PAY PE PLAY PM POST PT RE REAL RH RIDE
    RSI RUN SAFE SAVE SEC SEE SELL SO SOS SPOT STAY SUB SUN TA TECH TELL TIME TOP TRUE
    TV TWO UK UP US USA USD VERY WAY WELL WIN WISH WORK WSB YOLO YOU

    AGI AGM API ARR AV BG BTC CE CIA CPU DPU DRAM ESG ETH FBI FCF FMS FOMC FT GPU HBM
    IR ITM IV JUST LLM MRR NAND NATO OI OTC OTM PMI PPI PS RAM REIT ROE ROI RPD RPU
    SLM SOFR SPAC TPU TTM WH YOY QOQ
    JAN FEB MAR APR JUN JUL AUG SEP SEPT OCT NOV DEC
    """.split()
)


# `has_strict_cashtag`: upper case, two letters or more, and not followed by another `$`
# (LaTeX's `$AB$`). Lower case is PHP's `$name` and jQuery's `$el`; one letter is `$x`.
_STRICT_CASHTAG_RE = re.compile(r"(?<![\w$])\$([A-Z]{2,5}(?:[.\-][A-Z])?)(?![\w$])")
# Shell variables a programmer writes in prose ("add it to your $PATH"; `PATH` is
# UiPath), and placeholder amounts ("$XX billion"; `XX` is listed) -- both seen in a
# Hacker News sample (2026-10-08). The stoplist covers the rest (`$USD` is a dollar).
NOT_CASHTAGS = frozenset(
    "PATH HOME USER SHELL TERM LANG PWD HOST TMP TEMP CC CXX IFS XX XXX XXXX XXXXX".split()
)


@dataclass(frozen=True)
class Mention:
    symbol: str
    match_type: str
    in_universe: bool


def is_shouting(text: str) -> bool:
    letters = _LETTERS_RE.findall(text)
    if len(letters) < SHOUTING_MIN_LETTERS:
        return False
    upper = sum(1 for ch in letters if ch.isupper())
    return upper / len(letters) > SHOUTING_RATIO


def extract_mentions(text: str, universe: Universe, tagged: tuple[str, ...] = ()) -> list[Mention]:
    """Every distinct symbol in `text`, strongest evidence first per symbol.

    Precedence when a symbol appears several ways: tagged > cashtag > bare.
    """
    found: dict[str, Mention] = {}

    def add(symbol: str, match_type: str) -> None:
        if symbol and symbol not in found:
            found[symbol] = Mention(symbol, match_type, symbol in universe)

    for sym in tagged:
        add(normalize_symbol(sym), TAGGED)
    for raw in _CASHTAG_RE.findall(text):
        add(normalize_symbol(raw), CASHTAG)
    # Shouting is judged per line: an all-caps title over a normal body is common, and
    # judged as a whole the body would hide the title's noise.
    for line in text.splitlines():
        if is_shouting(line):
            continue
        for raw in _BARE_RE.findall(line):
            if raw not in STOPLIST and raw in universe:
                add(raw, BARE)
    return list(found.values())


def has_strict_cashtag(text: str, universe: Universe) -> bool:
    """Whether `text` writes a listed symbol as an explicit `$TICKER`.

    The evidence a general-topic platform needs (Bluesky, Hacker News), where most
    upper-case words and many `$words` are not about stocks. Stricter than a `cashtag`
    mention: upper case, in the universe, and neither a stoplist word nor a shell
    variable.
    """
    for raw in _STRICT_CASHTAG_RE.findall(text):
        symbol = normalize_symbol(raw)
        if symbol in universe and symbol not in STOPLIST and symbol not in NOT_CASHTAGS:
            return True
    return False


def validated_symbols(mentions: list[Mention]) -> list[str]:
    """The symbols a consumer can trust: in the universe, sorted for stable output."""
    return sorted(m.symbol for m in mentions if m.in_universe)
