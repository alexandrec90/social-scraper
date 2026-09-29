"""Cheap bullish/bearish stance features from a trading-slang lexicon.

This is **not** the sentiment score. ibkr_trader scores `social_posts.sentiment` itself
(VADER, optionally FinBERT) wherever it is NULL, so the scraper leaves that column
alone. What a general-purpose model misses is trading slang -- "puts", "drilling",
"bagholder", a rocket emoji -- and that is what these counts capture, as extra
features beside the model's number.

`stance` is `(bull - bear) / (bull + bear)` in [-1, 1], or None when neither side hit.
A negator within the three preceding tokens flips a term ("not bullish" is bearish).
Bare "call", "put", "long" and "short" are left out on purpose: in ordinary English
they are verbs and durations far more often than positions.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

LEXICON_VERSION = "stance-v1"

BULL_TERMS = frozenset(
    """
    bull bulls bullish calls buy buying bought moon mooning rocket rip ripping
    squeeze squeezing breakout undervalued tendies beat beats upgrade upgraded rally
    rallying soar soaring surge surging rebound bounce green lambo printing ath
    accumulate accumulating yolo diamond outperform
    """.split()
)
BEAR_TERMS = frozenset(
    """
    bear bears bearish shorting shorts puts sell selling sold dump dumping
    crash crashing tank tanking drill drilling bagholder bagholders bagholding rug
    rugpull overvalued downgrade downgraded miss missed plunge plunging collapse
    bankrupt bankruptcy dilution fraud red bleeding underperform
    """.split()
)
BULL_EMOJI = ("🚀", "🌙", "💎", "🐂", "📈", "🟢")
BEAR_EMOJI = ("🐻", "📉", "🩸", "🔴", "💀")
NEGATORS = frozenset({"not", "no", "never", "isnt", "isn't", "dont", "don't", "aint", "ain't"})
NEGATION_WINDOW = 3

_TOKEN_RE = re.compile(r"[a-z][a-z']*")


@dataclass(frozen=True)
class Stance:
    bull: int
    bear: int

    @property
    def score(self) -> float | None:
        total = self.bull + self.bear
        return None if total == 0 else round((self.bull - self.bear) / total, 4)


def score_stance(text: str) -> Stance:
    """Count bull and bear evidence in `text`, honouring nearby negation."""
    lowered = text.lower()
    tokens = _TOKEN_RE.findall(lowered)
    bull = bear = 0
    for i, tok in enumerate(tokens):
        side = 1 if tok in BULL_TERMS else -1 if tok in BEAR_TERMS else 0
        if not side:
            continue
        if any(t in NEGATORS for t in tokens[max(0, i - NEGATION_WINDOW) : i]):
            side = -side
        if side > 0:
            bull += 1
        else:
            bear += 1
    bull += sum(lowered.count(e) for e in BULL_EMOJI)
    bear += sum(lowered.count(e) for e in BEAR_EMOJI)
    return Stance(bull, bear)
