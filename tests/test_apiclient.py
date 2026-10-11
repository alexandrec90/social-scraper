"""`ApiClient`: the HTTPS JSON transport for Bluesky and Polymarket. No network: the
connection is a fake that records the request and plays back a response."""

from __future__ import annotations

import http.client
import json

import pytest

from social_scraper.apiclient import USER_AGENT, ApiClient

HOSTS = frozenset({"api.example.com"})


class FakeResponse:
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self.body = body

    def read(self, limit: int = -1) -> bytes:
        return self.body if limit < 0 else self.body[:limit]


class FakeConnection:
    def __init__(self, answer, log: list) -> None:
        self.answer = answer
        self.log = log
        self.closed = False

    def request(self, method, path, headers=None) -> None:
        self.log.append((method, path, headers))

    def getresponse(self):
        if isinstance(self.answer, BaseException):
            raise self.answer
        return self.answer

    def close(self) -> None:
        self.closed = True


CONNS: list[FakeConnection] = []


def client(answer, log=None) -> tuple[ApiClient, list]:
    calls: list = [] if log is None else log
    CONNS.clear()

    def connect(host: str, timeout: float):
        calls.append(("connect", host, timeout))
        conn = FakeConnection(answer, calls)
        CONNS.append(conn)
        return conn

    return ApiClient(HOSTS, timeout_s=5, connect=connect), calls


def test_get_json_parses_the_body_and_sends_the_contact_user_agent():
    c, calls = client(FakeResponse(200, json.dumps({"posts": []}).encode()))
    with c as api:
        result = api.get_json("https://api.example.com/xrpc/x?q=%24TSLA")
    assert result.status == 200 and result.data == {"posts": []} and result.text == ""
    assert calls[0] == ("connect", "api.example.com", 5)
    method, path, headers = calls[1]
    assert (method, path) == ("GET", "/xrpc/x?q=%24TSLA")
    assert headers["User-Agent"] == USER_AGENT and "@" not in USER_AGENT
    assert CONNS[0].closed


def test_a_body_past_the_cap_is_refused_by_name_not_cut_off(monkeypatch):
    """2026-10-10: Polymarket's NFL page was 33.6 MB; cut at 32 MB it failed to parse
    and read as "HTTP 200 but no json body", which said nothing about why."""
    from social_scraper import apiclient

    monkeypatch.setattr(apiclient, "MAX_BODY_BYTES", 10)
    c, _ = client(FakeResponse(200, b'{"a": "0123456789"}'))
    result = c.get_json("https://api.example.com/big")
    assert result.status == 200 and result.data is None
    assert "larger than" in result.text


def test_a_non_json_body_comes_back_as_text():
    c, _ = client(FakeResponse(403, b"<html>403 Forbidden</html>"))
    result = c.get_json("https://api.example.com/")
    assert result.status == 403 and result.data is None and "Forbidden" in result.text


def test_a_timeout_is_timed_out_and_a_dropped_connection_has_no_status():
    c, _ = client(TimeoutError("read timed out"))
    assert c.get_json("https://api.example.com/").timed_out
    c, _ = client(ConnectionResetError("reset"))
    result = c.get_json("https://api.example.com/")
    assert result.status is None and not result.timed_out and "ConnectionResetError" in result.text
    c, _ = client(http.client.RemoteDisconnected("gone"))
    assert c.get_json("https://api.example.com/").status is None


@pytest.mark.parametrize(
    "url", ["http://api.example.com/", "https://evil.example.com/", "file:///etc/passwd"]
)
def test_only_https_on_its_own_hosts(url):
    c, calls = client(FakeResponse(200, b"{}"))
    with pytest.raises(ValueError):
        c.get_json(url)
    assert calls == []


def test_get_html_and_capture_reuse_the_one_request():
    c, _ = client(FakeResponse(200, b"plain text"))
    page = c.get_html("https://api.example.com/p")
    assert page.html == "plain text" and page.status == 200
    c, _ = client(FakeResponse(200, b'{"a": 1}'))
    captured = c.capture("https://api.example.com/p", lambda u: True, 3, lambda: None)
    assert captured.payloads[0].data == {"a": 1}
    c, _ = client(FakeResponse(200, b'{"a": 1}'))
    assert c.capture("https://api.example.com/p", lambda u: False, 0, lambda: None).payloads == []
