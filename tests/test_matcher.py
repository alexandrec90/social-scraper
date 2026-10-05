from social_scraper.sports.matcher import (
    CONTEXT,
    NAME,
    Entity,
    EntityMention,
    Matcher,
    derived_names,
    fold,
    tokens,
)


def team(eid, name, *aliases, leagues=("nhl",)):
    return Entity(eid, "team", name, tuple(aliases), frozenset(leagues))


def athlete(eid, name, *aliases, leagues=("nhl",)):
    return Entity(eid, "athlete", name, tuple(aliases), frozenset(leagues))


HABS = team("Q1", "Montreal Canadiens", "Habs", "Canadiens de Montréal")
LEAFS = team("Q2", "Toronto Maple Leafs", "Maple Leafs")
FLA = team("Q3", "Florida Panthers")
CAR_NFL = team("Q4", "Carolina Panthers", leagues=("nfl",))
WILD = team("Q5", "Minnesota Wild", "Wild")
MCDAVID = athlete("Q6", "Connor McDavid")
ARSENAL = team("Q7", "Arsenal F.C.", leagues=("epl",))
MAN_UTD = team("Q8", "Manchester United F.C.", leagues=("epl",))
NEWCASTLE = team("Q9", "Newcastle United F.C.", leagues=("epl",))

CHANNELS = {"nhl": frozenset({"nhl"}), "Habs": frozenset({"nhl"}), "nfl": frozenset({"nfl"})}


def matcher(*entities):
    return Matcher(entities or (HABS, LEAFS, FLA, CAR_NFL, WILD, MCDAVID), CHANNELS)


def ids(found):
    return {m.entity_id: m.match_type for m in found}


def test_fold_and_tokens():
    assert fold("Montréal\N{RIGHT SINGLE QUOTATION MARK}s") == "Montreal's"
    assert tokens("McDavid's goal, Canadiens de Montréal!") == [
        "McDavid",
        "goal",
        "Canadiens",
        "de",
        "Montreal",
    ]


def test_derived_names():
    assert derived_names(HABS) == ["Canadiens"]
    assert derived_names(ARSENAL) == ["Arsenal"]
    assert derived_names(MAN_UTD) == ["Manchester United", "United"]
    assert derived_names(MCDAVID) == ["McDavid"]
    assert derived_names(athlete("Q", "Carlos Sainz Jr.")) == ["Sainz"]
    assert derived_names(athlete("Q", "Neymar")) == []


def test_full_names_match_anywhere_in_any_case_and_accent():
    found = matcher().match("the montreal canadiens and CANADIENS DE MONTREAL", channel=None)
    assert found == [EntityMention("Q1", NAME)]


def test_one_word_names_need_a_league_context():
    m = matcher()
    assert m.match("Habs win again") == []
    assert ids(m.match("Habs win again", channel="nhl")) == {"Q1": CONTEXT}
    assert ids(m.match("Habs win again", context=["nhl"])) == {"Q1": CONTEXT}


def test_a_full_name_in_the_post_supplies_the_context():
    found = matcher().match("Toronto Maple Leafs beat the Habs; McDavid scored")
    assert ids(found) == {"Q2": NAME, "Q1": CONTEXT, "Q6": CONTEXT}


def test_one_word_names_must_be_capitalised():
    m = matcher()
    assert m.match("the wild card race", channel="nhl") == []
    assert ids(m.match("Wild in overtime", channel="nhl")) == {"Q5": CONTEXT}


def test_shared_nicknames_resolve_by_league_and_otherwise_drop():
    m = matcher()
    assert ids(m.match("Panthers won", channel="nhl")) == {"Q3": CONTEXT}
    assert ids(m.match("Panthers won", channel="nfl")) == {"Q4": CONTEXT}
    assert m.match("Panthers won", context=["nhl", "nfl"]) == []


def test_names_shared_inside_one_league_are_dropped():
    m = Matcher([MAN_UTD, NEWCASTLE, ARSENAL], {"soccer": frozenset({"epl"})})
    assert m.match("United were poor", channel="soccer") == []
    assert ids(m.match("Arsenal and Manchester United", channel="soccer")) == {
        "Q7": CONTEXT,
        "Q8": NAME,
    }


def test_longest_match_wins_over_its_parts():
    m = matcher()
    assert ids(m.match("Maple Leafs", channel="nhl")) == {"Q2": NAME}


def test_stop_words_and_short_names_never_index():
    m = Matcher([team("Q", "The", "CH", leagues=("nhl",))], CHANNELS)
    assert len(m) == 1 and m.index == {}
    assert not m.knows(["The"])


def test_knows_and_leagues_of():
    m = matcher()
    assert m.knows(["connor", "mcdavid"]) and m.knows(["McDavid"])
    assert not m.knows(["Nobody"])
    assert m.leagues_of(["Q1", "Q4", "missing"]) == frozenset({"nhl", "nfl"})


def test_channel_lookup_ignores_case():
    assert ids(matcher().match("Habs!", channel="HABS")) == {"Q1": CONTEXT}
