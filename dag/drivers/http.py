"""Generic HTTP driver, for a pipeline step with no Python wrapper at all —
the only driver where a node's work can run genuinely outside the worker's
own codebase (the call target is a URL, not an import)."""
import os

import requests

from ..envelope import as_envelope, failure


def run(attributes: dict, inputs: dict) -> dict:
    """
    attributes:
      url: required
      method: default "POST"
      headers: optional dict -- static, e.g. an API key provisioned
        externally to this driver (it has no concept of key generation,
        rotation, or any other service-specific auth lifecycle; that's
        deliberately kept out of a driver meant to call arbitrary services).
      timeout: optional, default 60 (seconds)
      file_field: optional. When set, the request is sent as
        multipart/form-data with the file at inputs[file_path_key] attached
        under this field name, instead of a JSON body -- for an endpoint
        that takes a raw upload rather than a shared-path reference.
      file_path_key: which inputs key holds the local path to upload.
        Default "file_path".
    inputs: the merged predecessor payload (+ static kwargs). Sent verbatim
      as the JSON request body, unless file_field is set.
    """
    url = attributes.get("url")
    if not url:
        return failure("http driver requires a 'url' attribute.")

    method = attributes.get("method", "POST").upper()
    headers = attributes.get("headers", {})
    timeout = attributes.get("timeout", 60)
    file_field = attributes.get("file_field")

    try:
        if file_field:
            file_path_key = attributes.get("file_path_key", "file_path")
            local_path = inputs.get(file_path_key)
            if not local_path:
                return failure(
                    f"http driver: no '{file_path_key}' in inputs to upload as '{file_field}'."
                )
            with open(local_path, "rb") as f:
                response = requests.request(
                    method, url,
                    files={file_field: (os.path.basename(local_path), f)},
                    headers=headers, timeout=timeout,
                )
        else:
            response = requests.request(method, url, json=inputs, headers=headers, timeout=timeout)
        response.raise_for_status()
    except Exception as e:
        return failure(f"HTTP driver request to '{url}' failed: {e}")

    try:
        data = response.json()
    except ValueError:
        data = response.text

    return as_envelope(data)
