"""Generic HTTP driver, for a pipeline step with no Python wrapper at all —
the only driver where a node's work can run genuinely outside the worker's
own codebase (the call target is a URL, not an import)."""
import requests

from ..envelope import as_envelope, failure


def run(attributes: dict, inputs: dict) -> dict:
    """
    attributes:
      url: required
      method: default "POST"
      headers: optional dict
      timeout: optional, default 60 (seconds)
    inputs: the merged predecessor payload (+ static kwargs), sent as the
      JSON request body.
    """
    url = attributes.get("url")
    if not url:
        return failure("http driver requires a 'url' attribute.")

    method = attributes.get("method", "POST").upper()
    headers = attributes.get("headers", {})
    timeout = attributes.get("timeout", 60)

    try:
        response = requests.request(method, url, json=inputs, headers=headers, timeout=timeout)
        response.raise_for_status()
    except Exception as e:
        return failure(f"HTTP driver request to '{url}' failed: {e}")

    try:
        data = response.json()
    except ValueError:
        data = response.text

    return as_envelope(data)
