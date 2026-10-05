"""Find teams and athletes in free text. Pure; built once per cycle from `sports_entities`.

Two kinds of evidence, as with stock symbols:

- **name** -- a multi-word name or alias, matched case-insensitively and without
  accents: "Montreal Canadiens", "canadiens de montreal", "Connor McDavid". Specific
  enough to stand alone.
- **context** -- a one-word name: an alias ("Habs", "Bolts"), a team's nickname
  ("Canucks", from "Vancouver Canucks"), a club without its suffix ("Arsenal"), an
  athlete's surname ("McDavid"). Common words among them ("Wild", "Heat", "Kings",
  "City") make these worthless alone, so a one-word match must be capitalised as a name
  is, and it counts only when exactly one candidate plays in a league the post is
  already about: its subreddit's league, a league the caller names (an X search for
  that league), or the league of a name-match in the same post.

The same rule settles names two entities share: "Panthers" is Florida's in r/nhl,
Carolina's in r/nfl, and nobody's on its own.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass

NAME = "name"
CONTEXT = "context"
MAX_TOKENS = 6
_TOKEN = re.compile(r"[A-Za-z0-9]+(?:'[A-Za-z0-9]+)*")
# Club-form words a fan leaves off: "Arsenal F.C." is "Arsenal".
CLUB_WORDS = frozenset({"fc", "f", "c", "afc", "a", "cf", "sc", "ac", "cd", "ssc", "sv", "fk"})
NAME_SUFFIXES = frozenset({"jr", "sr", "ii", "iii", "iv"})
# Capitalised at the start of every other sentence; never a one-word name by themselves.
STOP = frozenset(
    """the this that they there then when what who why how and but for not all any can
    may will just game team today tonight""".split()
)


@dataclass(frozen=True)
class Entity:
    entity_id: str
    kind: str
    name: str
    aliases: tuple[str, ...]
    leagues: frozenset[str]


@dataclass(frozen=True)
class EntityMention:
    entity_id: str
    match_type: str


def fold(text: str) -> str:
    """Accents off, curly apostrophes straight: "Montréal" -> "Montreal"."""
    text = text.replace("\N{RIGHT SINGLE QUOTATION MARK}", "'")
    text = text.replace("\N{LEFT SINGLE QUOTATION MARK}", "'")
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def tokens(text: str) -> list[str]:
    """Word tokens, possessive `'s` dropped: "McDavid's" -> "McDavid"."""
    out = []
    for tok in _TOKEN.findall(fold(text)):
        if tok.lower().endswith("'s"):
            tok = tok[:-2]
        if tok:
            out.append(tok)
    return out


def derived_names(entity: Entity) -> list[str]:
    """Names people use that Wikidata does not list: nickname, short club name, surname."""
    words = tokens(entity.name)
    out: list[str] = []
    if entity.kind == "team":
        core = [w for w in words if w.lower() not in CLUB_WORDS]
        if core and core != words:
            out.append(" ".join(core))
        if len(core) >= 2:
            out.append(core[-1])
    else:
        core = [w for w in words if w.lower() not in NAME_SUFFIXES]
        if len(core) >= 2 and len(core[-1]) >= 4:
            out.append(core[-1])
    return out


def _key(toks: Iterable[str]) -> tuple[str, ...]:
    return tuple(t.casefold() for t in toks)


class Matcher:
    def __init__(
        self,
        entities: Iterable[Entity],
        channel_leagues: dict[str, frozenset[str]] | None = None,
    ) -> None:
        self.entities: dict[str, Entity] = {}
        # key -> {entity_id: is the key a multi-word name of it}
        self.index: dict[tuple[str, ...], dict[str, bool]] = {}
        self.channel_leagues = {k.lower(): v for k, v in (channel_leagues or {}).items()}
        for e in entities:
            self.entities[e.entity_id] = e
            for name in (e.name, *e.aliases, *derived_names(e)):
                toks = tokens(name)
                if not toks or len(toks) > MAX_TOKENS:
                    continue
                if len(toks) == 1 and (len(toks[0]) < 3 or toks[0].lower() in STOP):
                    continue
                slot = self.index.setdefault(_key(toks), {})
                slot[e.entity_id] = slot.get(e.entity_id, False) or len(toks) > 1

    def __len__(self) -> int:
        return len(self.entities)

    def knows(self, words: Iterable[str]) -> bool:
        """Some entity answers to exactly these words."""
        return _key(words) in self.index

    def leagues_of(self, entity_ids: Iterable[str]) -> frozenset[str]:
        out: set[str] = set()
        for eid in entity_ids:
            if eid in self.entities:
                out |= self.entities[eid].leagues
        return frozenset(out)

    def _spans(self, toks: list[str]) -> list[dict[str, bool]]:
        """Longest-first, non-overlapping matches; one-word ones only when capitalised."""
        spans, i = [], 0
        while i < len(toks):
            for n in range(min(MAX_TOKENS, len(toks) - i), 0, -1):
                hit = self.index.get(_key(toks[i : i + n]))
                if hit is None:
                    continue
                if n == 1 and not toks[i][0].isupper():
                    continue
                spans.append(hit)
                i += n
                break
            else:
                i += 1
        return spans

    def match(
        self, text: str, channel: str | None = None, context: Iterable[str] = ()
    ) -> list[EntityMention]:
        """Every entity `text` names, strongest evidence per entity, in text order."""
        spans = self._spans(tokens(text))
        found: dict[str, str] = {}
        rest: list[dict[str, bool]] = []
        for hit in spans:
            strong = [eid for eid, multi in hit.items() if multi]
            if len(strong) == 1:
                found.setdefault(strong[0], NAME)
            else:
                rest.append(hit)
        leagues = set(context) | set(self.leagues_of(found))
        if channel:
            leagues |= self.channel_leagues.get(channel.lower(), frozenset())
        for hit in rest:
            fits = [eid for eid in hit if self.entities[eid].leagues & leagues]
            if len(fits) == 1:
                found.setdefault(fits[0], CONTEXT)
        return [EntityMention(eid, kind) for eid, kind in found.items()]
