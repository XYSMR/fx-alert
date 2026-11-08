import pytest
import requests

from fx_alert.fetcher import API_PARAMS, API_URL, TIMEOUT_SECONDS, FetchError, fetch_rates_json


class FakeResponse:
    def __init__(self, content: bytes, status: int = 200):
        self.content = content
        self.status_code = status

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


def test_sends_one_request_with_params_timeout_and_user_agent():
    session = FakeSession(FakeResponse(b'{"rates": []}'))
    fetch_rates_json(session=session)
    assert len(session.calls) == 1
    url, kwargs = session.calls[0]
    assert url == API_URL
    assert kwargs["params"] == API_PARAMS
    assert kwargs["timeout"] == TIMEOUT_SECONDS
    assert "fx-alert" in kwargs["headers"]["User-Agent"]


def test_returns_text_and_strips_bom():
    session = FakeSession(FakeResponse('﻿{"rates": []}'.encode()))
    assert fetch_rates_json(session=session) == '{"rates": []}'


def test_http_error_raises_fetch_error():
    with pytest.raises(FetchError):
        fetch_rates_json(session=FakeSession(FakeResponse(b"", status=503)))


def test_network_error_raises_fetch_error():
    with pytest.raises(FetchError):
        fetch_rates_json(session=FakeSession(exc=requests.ConnectionError("boom")))


def test_empty_body_raises_fetch_error():
    with pytest.raises(FetchError, match="空"):
        fetch_rates_json(session=FakeSession(FakeResponse(b"   \n")))


def test_html_page_raises_fetch_error():
    html = b"<!DOCTYPE html><html><head><title>Maintenance</title></head></html>"
    with pytest.raises(FetchError, match="HTML"):
        fetch_rates_json(session=FakeSession(FakeResponse(html)))
