from social_scraper.stance import LEXICON_VERSION, Stance, score_stance


def test_bullish_slang():
    s = score_stance("Loading calls, this is going to the moon 🚀🚀")
    assert s.bull == 4 and s.bear == 0
    assert s.score == 1.0


def test_bearish_slang():
    s = score_stance("bagholders about to get rugged, buying puts, it's drilling 📉")
    assert s.bear >= 3
    assert s.score is not None and s.score < 0


def test_negation_flips_within_window():
    assert score_stance("I am not bullish on this").score == -1.0
    assert score_stance("never selling").score == 1.0


def test_negation_outside_window_does_not_flip():
    assert score_stance("not that I care about any of it, bullish").score == 1.0


def test_neutral_text_has_no_score():
    s = score_stance("Earnings call is on Thursday, what time?")
    assert s == Stance(0, 0)
    assert s.score is None


def test_plain_english_call_and_put_are_ignored():
    assert score_stance("I'll call you and put it on the list").score is None


def test_mixed_rounds():
    assert Stance(2, 1).score == round(1 / 3, 4)
    assert LEXICON_VERSION.startswith("stance-")
