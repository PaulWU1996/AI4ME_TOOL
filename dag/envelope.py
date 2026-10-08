"""Standard task-result envelope shared by dag/engine.py and dag/drivers/*.

Every driver's run() returns one of these, regardless of whether the
underlying call (a worker/tasks.py function, an HTTP request, ...) already
returns this shape on its own — a driver wraps a plain/raw return value in
success(), so DAGEngine only ever has to deal with one shape.
"""

ENVELOPE_KEYS = {"status", "data", "error"}


def success(data):
    return {"status": "success", "data": data, "error": None}


def failure(error):
    return {"status": "error", "data": None, "error": str(error)}


def is_envelope(value):
    return isinstance(value, dict) and ENVELOPE_KEYS <= value.keys()
    # the subset check should be removed once all drivers return envelopes including default container driver.


def as_envelope(value):
    """Return `value` unchanged if it's already envelope-shaped, otherwise
    wrap it as an implicit success (today's worker/tasks.py functions return
    raw dicts, not envelopes, until they're migrated one at a time)."""
    if is_envelope(value):
        return value
    return success(value)
