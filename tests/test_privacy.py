import pytest

from social_scraper.privacy import contains_secret, scrub_text


def test_scrub_removes_contact_data_and_handles():
    text = "DM me at trader.joe@example.com or (514) 555-1234, cc @someone and u/other_user"
    out = scrub_text(text)
    assert "example.com" not in out
    assert "555" not in out
    assert "someone" not in out
    assert "other_user" not in out
    assert "[email]" in out and "[phone]" in out and "@user" in out and "u/user" in out


def test_scrub_keeps_tickers_prices_and_dates():
    text = "$NVDA 150.00 target, 2026-10-15 expiry, up 12.5%"
    assert scrub_text(text) == text


def test_scrub_passes_empty_through():
    assert scrub_text(None) is None
    assert scrub_text("") == ""


@pytest.mark.parametrize(
    "secret",
    [
        "AKIAABCDEFGHIJKLMNOP",  # pragma: allowlist secret - fake, the pattern under test
        "ghp_" + "a" * 36,
        "sk-ant-" + "b" * 30,
        "xoxb-1234567890-abc",
        "AIza" + "c" * 35,
        "-----BEGIN RSA PRIVATE KEY-----",  # pragma: allowlist secret - header only, no key
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",  # pragma: allowlist secret - public jwt.io sample
    ],
)
def test_contains_secret_matches_known_credential_shapes(secret):
    assert contains_secret("title", f"oops pasted {secret} here")


def test_contains_secret_ignores_ordinary_text():
    assert not contains_secret("NVDA to the moon", None, "https://www.reddit.com/r/stocks/")
