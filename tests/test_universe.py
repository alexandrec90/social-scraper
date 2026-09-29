import os
import time
from typing import ClassVar

import pytest

from social_scraper.symbols import universe as universe_mod
from social_scraper.symbols.universe import (
    Universe,
    download,
    is_stale,
    load_universe,
    normalize_symbol,
    parse_nasdaq_traded,
    parse_symbol_list,
)

# Lines in the real directory's shape (captured 2026-09-28), trimmed to the cases.
DIRECTORY = "\n".join(
    [
        "Nasdaq Traded|Symbol|Security Name|Listing Exchange|Market Category|ETF|Round Lot Size|Test Issue|Financial Status|CQS Symbol|NASDAQ Symbol|NextShares",
        "Y|A|Agilent Technologies, Inc. Common Stock|N| |N|100|N||A|A|N",
        "Y|BRK.B|Berkshire Hathaway Inc. New Common Stock|N| |N|40|N||BRK.B|BRK.B|N",
        "Y|F$B|Ford Motor Company 6.20% Notes due June 1, 2059|N| |N|100|N||FpB|F-B|N",
        "Y|GME.W|GameStop Corporation Warrants to Purchase Common Stock|N| |N|100|N||GME.WS|GME+|N",
        "Y|SPY|State Street SPDR S&P 500 ETF Trust|P| |Y|40|N||SPY|SPY|N",
        "Y|ZZZTX|TXSE Test Stk 5 Common Stock|F| |N|40|Y||ZZZTX|ZZZTX|N",
        "Y|ABCU|Some Acquisition Corp Units|Q|G|N|100|N|N||ABCU|N",
        "File Creation Time: 0928202621:32|||||",
    ]
)


def test_normalize_symbol():
    assert normalize_symbol(" $brk.b ") == "BRK-B"
    assert normalize_symbol("bf/a") == "BF-A"
    assert normalize_symbol("nvda") == "NVDA"


def test_parse_nasdaq_traded_keeps_stocks_and_etfs_only():
    parsed = parse_nasdaq_traded(DIRECTORY)
    assert set(parsed) == {"A", "BRK-B", "SPY"}
    assert parsed["SPY"].is_etf is True
    assert parsed["A"].exchange == "N"
    assert parsed["BRK-B"].name.startswith("Berkshire")


def test_parse_nasdaq_traded_rejects_other_files():
    with pytest.raises(ValueError):
        parse_nasdaq_traded("not|a|directory\nx|y|z")
    assert parse_nasdaq_traded("") == {}


def test_parse_symbol_list_ignores_comments_and_blanks():
    assert parse_symbol_list("RY\n\n# comment\nbrk-b  # class B\n") == {"RY", "BRK-B"}


def test_universe_membership_and_extras():
    u = Universe(parse_nasdaq_traded(DIRECTORY))
    assert "A" in u
    assert 42 not in u
    assert len(u) == 3
    assert u.get("SPY").is_etf
    u.add_symbols({"RY", "A"})
    assert "RY" in u
    assert u.get("A").name.startswith("Agilent")  # extras never overwrite


def test_is_stale(tmp_path):
    f = tmp_path / "f.txt"
    assert is_stale(f, 1)
    f.write_text("x")
    assert not is_stale(f, 1)
    old = time.time() - 7200
    os.utime(f, (old, old))
    assert is_stale(f, 1)


def test_download_refuses_non_https(tmp_path):
    with pytest.raises(ValueError):
        download("http://example.com/x.txt", tmp_path / "x.txt")
    with pytest.raises(ValueError):
        download("file:///etc/passwd", tmp_path / "x.txt")


class _Resp:
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self._body = body

    def read(self) -> bytes:
        return self._body


class _Conn:
    status = 200
    requested: ClassVar[list[str]] = []

    def __init__(self, host, port, timeout):
        self.host = host

    def request(self, method, path, headers):
        _Conn.requested.append(f"{method} {self.host}{path}")

    def getresponse(self):
        return _Resp(_Conn.status, DIRECTORY.encode())

    def close(self):
        return None


def test_download_writes_atomically(tmp_path, monkeypatch):
    monkeypatch.setattr(universe_mod.http.client, "HTTPSConnection", _Conn)
    dest = tmp_path / "sub" / "nasdaq.txt"
    download("https://example.com/dir/file.txt?x=1", dest)
    assert dest.read_text() == DIRECTORY
    assert _Conn.requested[-1] == "GET example.com/dir/file.txt?x=1"
    assert not dest.with_suffix(".tmp").exists()


def test_download_raises_on_http_error(tmp_path, monkeypatch):
    monkeypatch.setattr(universe_mod.http.client, "HTTPSConnection", _Conn)
    monkeypatch.setattr(_Conn, "status", 503)
    with pytest.raises(OSError):
        download("https://example.com/f.txt", tmp_path / "f.txt")


def test_load_universe_refreshes_when_stale(tmp_path):
    cache = tmp_path / "nasdaq.txt"

    def fetch(url, dest):
        dest.write_text(DIRECTORY)

    u = load_universe(cache, "https://x", 24, fetch=fetch)
    assert "SPY" in u


def test_load_universe_falls_back_to_stale_cache_on_failure(tmp_path):
    cache = tmp_path / "nasdaq.txt"
    cache.write_text(DIRECTORY)
    old = time.time() - 10 * 86400
    os.utime(cache, (old, old))

    def fetch(url, dest):
        raise OSError("network down")

    assert "A" in load_universe(cache, "https://x", 24, fetch=fetch)


def test_load_universe_with_no_cache_uses_extra_files_only(tmp_path):
    extra = tmp_path / "tickers.txt"
    extra.write_text("RY\nTD\n")

    def fetch(url, dest):
        raise OSError("offline")

    u = load_universe(
        tmp_path / "missing.txt", "https://x", 24, (extra, tmp_path / "nope.txt"), fetch
    )
    assert set(u.securities) == {"RY", "TD"}


def test_security_record():
    sec = universe_mod.Security("NVDA", "NVIDIA", "Q", is_etf=False)
    assert Universe({"NVDA": sec}).get("NVDA") is sec
