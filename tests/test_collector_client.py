import httpx
import pytest

from app.collector_client import CollectorClient, CollectorUnavailable


def install_transport(monkeypatch, handler):
    monkeypatch.setattr(
        httpx,
        "HTTPTransport",
        lambda uds: httpx.MockTransport(handler),
    )


def test_collector_client_returns_object_payload(monkeypatch):
    def handler(request):
        assert request.url.path == "/internal/drives"
        assert request.url.params["force"] == "true"
        return httpx.Response(200, json={"drives": []})

    install_transport(monkeypatch, handler)

    payload = CollectorClient("/tmp/test.sock").get(
        "/internal/drives", params={"force": True}
    )

    assert payload == {"drives": []}


def test_collector_client_normalizes_connection_failure(monkeypatch):
    def handler(request):
        raise httpx.ConnectError("socket missing", request=request)

    install_transport(monkeypatch, handler)

    with pytest.raises(CollectorUnavailable, match="collector connection failed"):
        CollectorClient("/tmp/test.sock").get("/internal/health")


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (httpx.Response(200, text="not-json"), "invalid JSON"),
        (httpx.Response(200, json=["not", "an", "object"]), "invalid response"),
        (
            httpx.Response(
                503,
                json={"detail": {"collector_error": "inventory unavailable"}},
            ),
            "inventory unavailable",
        ),
    ],
)
def test_collector_client_rejects_bad_responses(monkeypatch, response, message):
    install_transport(monkeypatch, lambda request: response)

    with pytest.raises(CollectorUnavailable, match=message):
        CollectorClient("/tmp/test.sock").get("/internal/drives")
