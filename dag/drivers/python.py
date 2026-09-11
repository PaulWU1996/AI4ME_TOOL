"""Generic python-callable driver.

Generalizes what dag/engine.py's old TASK_REGISTRY dict did: resolve a
function by dotted module path + name and call it, instead of hardcoding
imports in engine.py. Existing workflow JSON that only specifies "task"
(no "driver"/"module"/"func") keeps working unmodified — dag/engine.py
defaults module to "tasks" and func to the "task" value before calling
this driver, for backward compatibility.
"""
import importlib

from ..envelope import as_envelope, failure


def run(attributes: dict, inputs: dict) -> dict:
    """
    attributes:
      module: dotted module to import (default handled by caller, usually "tasks")
      func / task: attribute name to call on that module
      call: "payload" (default; func(inputs)) or "kwargs" (func(**inputs))
    inputs: either the merged predecessor payload (call="payload") or a
      dict of keyword arguments (call="kwargs"), prepared by the caller.
    """
    module_name = attributes.get("module", "tasks")
    func_name = attributes.get("func") or attributes.get("task")
    if not func_name:
        return failure("python driver requires a 'func' (or legacy 'task') attribute.")

    try:
        module = importlib.import_module(module_name)
        func = getattr(module, func_name)
    except (ImportError, AttributeError) as e:
        return failure(f"Cannot resolve python driver target '{module_name}.{func_name}': {e}")

    try:
        if attributes.get("call") == "kwargs":
            result = func(**inputs)
        else:
            result = func(inputs)
    except Exception as e:
        return failure(str(e))

    return as_envelope(result)
