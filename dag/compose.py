"""Turn a registered workflow template into a single Celery canvas.

The workflow JSON is translated into Celery's `chain` and `group` primitives.

Layering: every node is assigned to exactly one layer, where
`layer[n] = 1 + max(layer[p] for p in n's predecessors)`. Single assignment
means each task appears in exactly one step of the canvas however many
downstream branches depend on it; every predecessor of a node sits in a
strictly earlier layer, so running the layers in chain order guarantees the
node's dependencies are finished before it starts. Celery treats a `group`
immediately followed by another step as a fan-in — it waits for the whole
group and hands the results onward.
"""
from collections import defaultdict

from celery import chain, group, signature

from .parser import DAG, Parser


def topological_layers(dag: DAG) -> list[list[str]]:
    """Group nodes into layers: a node's layer is 1 + max over its
    predecessors' layers, roots are layer 0."""
    layer = {}
    # store which layer each node belongs to as a list
    for node in dag.topological_sort():
        preds = dag.get_predecessors(node)
        layer[node] = 0 if not preds else 1 + max(layer[p] for p in preds)
    grouped = defaultdict(list)
    # group nodes by their layer number
    for node, value in layer.items():
        grouped[value].append(node)
    return [grouped[i] for i in range(max(grouped) + 1)]


def build_task_map(parser: Parser, job_context: dict) -> dict:
    """Map each workflow node to a Celery task signature.

    - A node declaring `call: "kwargs"` is dispatched with a slice of the
      job context named in its `inject` list (merged over its static
      `kwargs`), and is immutable so the chain never injects a predecessor
      result into it. `requires` fails the build if any such key is absent.
    - A node declaring `driver: "http"` becomes the generic `tasks.http_call`
      task, with the call parameters baked in; the predecessor's result
      arrives positionally as the request payload.
    - Every other node receives the previous link's result positionally —
      the single-predecessor merged-payload convention.

    Retry policy stays on the worker tasks themselves (a workflow's transient
    `download` node declares `retries`), so nothing needs to be attached here.
    """
    signatures = {}
    for node_id in parser.dag.get_all_nodes():
        # node attributes are legacy,
        # 
        attrs = parser.dag.get_node_attributes(node_id)

        if attrs.get("driver") == "http":
            signatures[node_id] = signature(
                "tasks.http_call",
                kwargs={
                    "url": attrs.get("url"),
                    "method": attrs.get("method", "POST"),
                    "headers": attrs.get("headers") or {},
                    "timeout": attrs.get("timeout", 60),
                    "file_field": attrs.get("file_field"),
                    "file_path_key": attrs.get("file_path_key", "file_path"),
                    "service": attrs.get("service"),
                    "body": attrs.get("body"),
                    "merge": attrs.get("merge", False),
                    "save": attrs.get("save"),
                },
            )
            continue

        task_name = attrs.get("task")
        if not task_name:
            raise ValueError(f"Node '{node_id}' has no 'task' to call.")

        if attrs.get("call") == "kwargs":
            kwargs = {
                **attrs.get("kwargs", {}),
                **{k: job_context[k] for k in attrs.get("inject", [])
                   if job_context.get(k) is not None},
            }
            missing = [k for k in attrs.get("requires", []) if kwargs.get(k) is None]
            if missing:
                raise ValueError(f"Node '{node_id}' is missing required inputs {missing}.")
            signatures[node_id] = signature(
                f"tasks.{task_name}", kwargs=kwargs, immutable=True
            )
        else:
            signatures[node_id] = signature(
                f"tasks.{task_name}", kwargs=attrs.get("kwargs", {})
            )
    return signatures


def build_canvas(dag: DAG, task_map: dict):
    """One flat chain of layers; a layer with more than one node becomes a
    group (parallel), a single-node layer is just the task itself."""
    steps = []
    for layer in topological_layers(dag):
        nodes = [task_map[node] for node in layer]
        steps.append(group(nodes) if len(nodes) > 1 else nodes[0])
    return chain(*steps)