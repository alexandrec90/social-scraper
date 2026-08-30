"""Smoke test — proves the suite, the import path, and the config all work.

Delete this once real tests exist (it is listed in TODO.md). Until then it is what
makes a red PR gate mean something on day one instead of "no tests collected".
"""

import social_scraper


def test_package_imports():
    assert social_scraper.__name__ == "social_scraper"


def test_version_is_declared():
    # Catches the common generated-project break: pyproject says the package lives
    # somewhere the import path does not actually reach.
    assert isinstance(social_scraper.__version__, str)
    assert social_scraper.__version__
