"""dag/drivers/http.py — driven against a real loopback HTTP server rather
than a monkeypatched `requests`, so the actual request/response path runs."""
import pytest

from dag.drivers import http as http_driver


def test_posts_inputs_as_json_body_by_default(http_server):
    http_server.echo()

    env = http_driver.run({"url": http_server.url + "/analyze"}, {"file_path": "/app/tmp/j/v.mp4"})

    assert env["status"] == "success"
    assert env["data"]["method"] == "POST"
    assert env["data"]["path"] == "/analyze"
    assert env["data"]["body"] == {"file_path": "/app/tmp/j/v.mp4"}


def test_method_attribute_is_honoured_and_upcased(http_server):
    http_server.echo()
    env = http_driver.run({"url": http_server.url + "/health", "method": "get"}, {})
    assert env["data"]["method"] == "GET"


def test_headers_are_sent(http_server):
    http_server.echo()

    env = http_driver.run(
        {"url": http_server.url, "headers": {"X-API-Key": "secret-value"}},
        {},
    )

    assert env["data"]["headers"]["x-api-key"] == "secret-value"


def test_json_response_becomes_envelope_data(http_server):
    http_server.respond(body={"segments": [1, 2, 3]})
    env = http_driver.run({"url": http_server.url}, {})
    assert env == {"status": "success", "data": {"segments": [1, 2, 3]}, "error": None}


def test_non_json_response_falls_back_to_text(http_server):
    http_server.respond(body="<xml>not json</xml>", content_type="text/plain")
    env = http_driver.run({"url": http_server.url}, {})
    assert env["status"] == "success"
    assert env["data"] == "<xml>not json</xml>"


def test_service_returning_an_envelope_is_passed_through(http_server):
    http_server.respond(body={"status": "error", "data": None, "error": "model unavailable"})

    env = http_driver.run({"url": http_server.url}, {})

    assert env["status"] == "error"
    assert env["error"] == "model unavailable"


def test_service_response_with_its_own_status_field_is_still_wrapped(http_server):
    """A service saying {"status": "completed"} is data, not an envelope."""
    http_server.respond(body={"status": "completed", "n": 2})

    env = http_driver.run({"url": http_server.url}, {})

    assert env["status"] == "success"
    assert env["data"] == {"status": "completed", "n": 2}


# --------------------------------------------------------------------------
# Failure paths
# --------------------------------------------------------------------------

def test_missing_url_returns_failure_envelope():
    env = http_driver.run({}, {})
    assert env["status"] == "error"
    assert "requires a 'url'" in env["error"]


@pytest.mark.parametrize("status", [400, 404, 500, 503])
def test_http_error_status_becomes_failure_envelope(http_server, status):
    http_server.respond(status=status, body={"detail": "nope"})

    env = http_driver.run({"url": http_server.url}, {})

    assert env["status"] == "error"
    assert "HTTP driver request to" in env["error"]
    assert env["data"] is None


def test_connection_refused_becomes_failure_envelope(closed_port):
    env = http_driver.run({"url": f"http://127.0.0.1:{closed_port}/analyze"}, {})
    assert env["status"] == "error"
    assert "failed" in env["error"]


def test_unroutable_host_becomes_failure_envelope():
    env = http_driver.run(
        {"url": "http://visualservice.invalid/analyze", "timeout": 2},
        {},
    )
    assert env["status"] == "error"


def test_driver_never_raises_on_a_bad_request(closed_port):
    """Contract relied on by dag/engine.py: a driver returns envelopes, it
    does not raise — so `on_failure` decides what happens, not a traceback."""
    env = http_driver.run({"url": f"http://127.0.0.1:{closed_port}", "method": "PUT"}, {"a": 1})
    assert env["status"] == "error"
