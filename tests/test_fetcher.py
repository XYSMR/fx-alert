from datetime import datetime

import pytest
import requests

from fx_alert.fetcher import CSV_URL, TIMEOUT_SECONDS, FetchError, fetch_csv


class FakeResponse:
    def __init__(self, content: bytes, status: int = 200, headers: dict | None = None):
        self.content = content
        self.status_code = status
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} error")


class FakeSession:
    def __init__(self, response=None, exc=None):
        self.response = response
        self.exc = exc
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.exc:
            raise self.exc
        return self.response


def test_fetch_sends_one_request_with_timeout_and_user_agent():
    session = FakeSession(FakeResponse("幣別\nJPY".encode()))
    fetch_csv(session=session)
    assert len(session.calls) == 1
    url, kwargs = session.calls[0]
    assert url == CSV_URL
    assert kwargs["timeout"] == TIMEOUT_SECONDS
    assert "fx-alert" in kwargs["headers"]["User-Agent"]


def test_fetch_strips_utf8_bom():
    session = FakeSession(FakeResponse("﻿幣別,匯率".encode()))
    assert fetch_csv(session=session).text == "幣別,匯率"


def test_fetch_reads_quoted_time_from_filename():
    headers = {"Content-Disposition": "attachment; filename=ExchangeRate@202611091530.csv"}
    session = FakeSession(FakeResponse(b"x", headers=headers))
    assert fetch_csv(session=session).quoted_at == datetime(2026, 11, 9, 15, 30)


def test_fetch_quoted_time_none_when_header_missing():
    session = FakeSession(FakeResponse(b"x"))
    assert fetch_csv(session=session).quoted_at is None


def test_http_error_raises_fetch_error():
    session = FakeSession(FakeResponse(b"", status=503))
    with pytest.raises(FetchError):
        fetch_csv(session=session)


def test_network_error_raises_fetch_error():
    session = FakeSession(exc=requests.ConnectionError("boom"))
    with pytest.raises(FetchError):
        fetch_csv(session=session)


def test_empty_body_raises_fetch_error():
    session = FakeSession(FakeResponse(b"   \n"))
    with pytest.raises(FetchError, match="空"):
        fetch_csv(session=session)


def test_html_challenge_page_raises_fetch_error():
    html = b"<!DOCTYPE html><html><head><title>Challenge Validation</title></head></html>"
    session = FakeSession(FakeResponse(html))
    with pytest.raises(FetchError, match="HTML"):
        fetch_csv(session=session)
