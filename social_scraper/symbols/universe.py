"""The set of real, tradable symbols a mention is checked against.

Source: NASDAQ Trader's `nasdaqtraded.txt`, a public pipe-delimited directory of every
US-listed security (NYSE, NASDAQ, ARCA, ...). No key, refreshed daily upstream. It is
cached under `data/symbols/` and re-downloaded when older than
`SYMBOL_UNIVERSE_MAX_AGE_HOURS`; a failed download falls back to the stale cache, so a
flaky NASDAQ server never stops a scrape.

Class shares are normalised to a hyphen (`BRK.B` -> `BRK-B`), the spelling ibkr_trader's
`tickers.txt` uses.
"""

from __future__ import annotations

import http.client
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

log = logging.getLogger(__name__)

_COMMON_SYMBOL = re.compile(r"^[A-Z]{1,5}(?:\.[A-Z])?$")
# Securities that share a root symbol with a stock but are not what people discuss.
_NOT_EQUITY = re.compile(r"\b(warrants?|rights?|units?|notes due|debentures?)\b", re.IGNORECASE)
DOWNLOAD_TIMEOUT_S = 30


@dataclass(frozen=True)
class Security:
    symbol: str
    name: str
    exchange: str
    is_etf: bool


def normalize_symbol(raw: str) -> str:
    """Upper-case, strip a leading `$`, and use `-` for share classes."""
    return raw.strip().lstrip("$").upper().replace(".", "-").replace("/", "-")


def parse_nasdaq_traded(text: str) -> dict[str, Security]:
    """Parse the directory into `{symbol: Security}`, keeping common stock and ETFs.

    Test issues, preferreds (`F$B`), warrants, units, rights and notes are dropped.
    The header and the trailing `File Creation Time` line are skipped.
    """
    out: dict[str, Security] = {}
    lines = text.splitlines()
    if not lines:
        return out
    header = lines[0].split("|")
    try:
        idx = {name: header.index(name) for name in ("Symbol", "Security Name", "Test Issue")}
    except ValueError as exc:
        raise ValueError(f"not a nasdaqtraded.txt header: {lines[0][:80]!r}") from exc
    exch = header.index("Listing Exchange") if "Listing Exchange" in header else None
    etf = header.index("ETF") if "ETF" in header else None
    for line in lines[1:]:
        cols = line.split("|")
        if len(cols) < len(header) or cols[idx["Test Issue"]] != "N":
            continue
        symbol, name = cols[idx["Symbol"]].strip(), cols[idx["Security Name"]].strip()
        if not _COMMON_SYMBOL.match(symbol) or _NOT_EQUITY.search(name):
            continue
        norm = normalize_symbol(symbol)
        out[norm] = Security(
            symbol=norm,
            name=name,
            exchange=cols[exch].strip() if exch is not None else "",
            is_etf=etf is not None and cols[etf].strip() == "Y",
        )
    return out


def parse_symbol_list(text: str) -> set[str]:
    """One symbol per line; blank lines and `#` comments ignored."""
    return {
        normalize_symbol(line.split("#", 1)[0])
        for line in text.splitlines()
        if line.split("#", 1)[0].strip()
    }


@dataclass
class Universe:
    securities: dict[str, Security]

    def __contains__(self, symbol: object) -> bool:
        return isinstance(symbol, str) and symbol in self.securities

    def __len__(self) -> int:
        return len(self.securities)

    def get(self, symbol: str) -> Security | None:
        return self.securities.get(symbol)

    def add_symbols(self, symbols: set[str], exchange: str = "extra") -> None:
        for sym in symbols:
            self.securities.setdefault(sym, Security(sym, "", exchange, is_etf=False))


def is_stale(path: Path, max_age_hours: float, now: float | None = None) -> bool:
    if not path.exists():
        return True
    age_s = (time.time() if now is None else now) - path.stat().st_mtime
    return age_s > max_age_hours * 3600


def download(url: str, dest: Path) -> None:
    """Fetch `url` to `dest` atomically (write a temp file, then replace).

    `HTTPSConnection` rather than `urlopen`: it cannot be pointed at `file://` or
    plain HTTP by a bad setting, which is the hazard the linter warns about.
    """
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        raise ValueError(f"refusing non-https symbol source: {url}")
    conn = http.client.HTTPSConnection(parts.hostname, parts.port, timeout=DOWNLOAD_TIMEOUT_S)
    try:
        path = parts.path + (f"?{parts.query}" if parts.query else "")
        conn.request("GET", path or "/", headers={"User-Agent": "social-scraper/0.1"})
        resp = conn.getresponse()
        if resp.status != 200:
            raise OSError(f"symbol source answered HTTP {resp.status}")
        body = resp.read()
    finally:
        conn.close()
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".tmp")
    tmp.write_bytes(body)
    tmp.replace(dest)


def load_universe(
    cache: Path,
    url: str,
    max_age_hours: float,
    extra_files: tuple[Path, ...] = (),
    fetch=download,
) -> Universe:
    """Load the cached directory, refreshing it when stale; merge any extra lists.

    Refresh failure is logged and the stale copy used. With no copy at all the
    universe holds only the extra files -- cashtags still work, bare words do not.
    """
    if is_stale(cache, max_age_hours):
        try:
            fetch(url, cache)
        except (OSError, ValueError, http.client.HTTPException) as exc:
            # Network, TLS (ssl.SSLError is an OSError), HTTP status: all degrade alike.
            log.warning("symbol universe refresh failed (%s); using cached copy", exc)
    securities = {}
    if cache.exists():
        securities = parse_nasdaq_traded(cache.read_text(encoding="utf-8", errors="replace"))
    universe = Universe(securities)
    for path in extra_files:
        if path.exists():
            universe.add_symbols(parse_symbol_list(path.read_text(encoding="utf-8")))
        else:
            log.warning("extra symbol file not found: %s", path)
    return universe
