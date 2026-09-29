from conftest import FakeBrowser

from social_scraper import fetch


def test_fake_browser_satisfies_the_browser_contract():
    # What every scraper test relies on: the fake has the protocol's shape.
    browser: fetch.Browser = FakeBrowser({"https://a": fetch.PageResult(200, "https://a")})
    assert browser.get_json("https://a").status == 200
    assert browser.capture("https://a", lambda u: True, 0, lambda: None).status == 200


def test_page_result_defaults_are_independent():
    a, b = fetch.PageResult(200, "u"), fetch.PageResult(200, "u")
    a.payloads.append(fetch.Payload("u", 200, {}))
    assert b.payloads == [] and a.text == "" and a.data is None


def test_fetch_failed_carries_outcome_and_detail():
    exc = fetch.FetchFailed("rate_limited", "HTTP 429")
    assert (exc.outcome, exc.detail) == ("rate_limited", "HTTP 429")
    assert str(exc) == "rate_limited: HTTP 429"
