import json
import threading
from urllib.error import HTTPError
from urllib.request import urlopen

import pytest

import dashboard


@pytest.fixture
def dashboard_server(monkeypatch):
    server = dashboard.HTTPServer(("127.0.0.1", 0), dashboard.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, monkeypatch
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_chart_api_returns_json_for_encoded_crypto_symbol(dashboard_server):
    server, monkeypatch = dashboard_server
    captured = {}

    def payload(symbol, range_key):
        captured.update(symbol=symbol, range_key=range_key)
        return {"symbol": symbol, "range": range_key, "bars": [{"close": 100.0}]}

    monkeypatch.setattr(dashboard, "_chart_payload_for_request", payload)

    with urlopen(
        f"http://127.0.0.1:{server.server_port}/api/chart?symbol=BTC%2FUSD&range=1M"
    ) as response:
        body = json.loads(response.read().decode("utf-8"))

    assert response.status == 200
    assert response.headers["Content-Type"] == "application/json; charset=utf-8"
    assert response.headers["Access-Control-Allow-Origin"] == "*"
    assert captured == {"symbol": "BTC/USD", "range_key": "1M"}
    assert body["bars"][0]["close"] == 100.0


def test_chart_api_returns_visible_validation_error(dashboard_server):
    server, monkeypatch = dashboard_server
    monkeypatch.setattr(
        dashboard,
        "_chart_payload_for_request",
        lambda *args: (_ for _ in ()).throw(ValueError("허용되지 않은 자산")),
    )

    with pytest.raises(HTTPError) as caught:
        urlopen(f"http://127.0.0.1:{server.server_port}/api/chart?symbol=EVIL&range=1M")

    assert caught.value.code == 400
    body = json.loads(caught.value.read().decode("utf-8"))
    assert body == {"error": "허용되지 않은 자산"}
