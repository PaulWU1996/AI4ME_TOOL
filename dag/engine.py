import concurrent.futures
import importlib

import networkx as nx

from .parser import DAG
from .drivers import http as http_driver
from .drivers import python as python_driver
from .envelope import failure
from .readiness import ensure_ready, release, validate_mode, ServiceNotReadyError

DRIVERS = {
    "python": python_driver,
    "http": http_driver,
}

# Nodes whose task doesn't follow the payload-in/payload-out convention used
# by everything else in worker/tasks.py — they need job context (job_id,
# request input, job_type/callback_url) instead of a merged predecessor
# payload. Kept as an explicit set (rather than threading job context
# through node attributes generically) so drivers/* stay simple, generic
# callers with no knowledge of DAGEngine's job-level state.
NON_PAYLOAD_TASKS = {"download_file", "finalize_results"}


class PayloadConflictError(Exception):
    """Raised when two predecessors of a node disagree on the same payload key."""


class UnknownTaskError(Exception):
    """Raised when a node's driver/task attributes don't resolve to a callable."""


class TaskExecutionError(Exception):
    """Raised when a node's driver returns a failure envelope."""


ON_FAILURE_MODES = {"stop", "continue"}


class DAGEngine:
    def __init__(self, dag: DAG, job_id, job_type="full", callback_url=None, job_inputs=None,
                 on_failure="stop"):
        """
        job_inputs: per-job runtime input from the client request (e.g.
        {"path": ..., "prompts": ...}), analogous to `ProcessRequest` in
        controller/main.py. Distinct from a node's static `kwargs` in the
        workflow JSON, which describe the reusable pipeline template.

        on_failure: "stop" (default) halts the whole run on the first
        failed node; "continue" logs the failure and keeps walking the
        remaining nodes — note downstream nodes that actually depend on
        the failed node's output may then fail too (missing payload
        keys), since "continue" doesn't skip them, only avoids halting
        early. Sourced from a workflow's `settings.on_failure`, e.g. by
        passing `Parser(...).settings.get("on_failure", "stop")`.
        """
        if on_failure not in ON_FAILURE_MODES:
            raise ValueError(f"Unknown on_failure mode '{on_failure}'. Choose from: {ON_FAILURE_MODES}")
        self.dag = dag
        self.job_id = job_id
        self.job_type = job_type
        self.callback_url = callback_url
        self.job_inputs = job_inputs or {}
        self.on_failure = on_failure
        self.executed_nodes = set()
        self.node_results = {}

    def _task_name(self, node_id):
        attrs = self.dag.get_node_attributes(node_id)
        return attrs.get('func') or attrs.get('task', node_id)

    def _validate_task_names(self):
        """Check every node's driver/target resolves before any node runs,
        so a typo'd task fails fast instead of partway through the DAG
        (after earlier nodes already had real side effects)."""
        for node_id in self.dag.get_all_nodes():
            attrs = self.dag.get_node_attributes(node_id)
            driver_name = attrs.get('driver', 'python')
            if driver_name not in DRIVERS:
                raise UnknownTaskError(f"Unknown driver '{driver_name}' (node '{node_id}').")

            if driver_name == 'python':
                module_name = attrs.get('module', 'tasks')
                func_name = attrs.get('func') or attrs.get('task')
                if not func_name:
                    raise UnknownTaskError(f"Node '{node_id}' has no 'func'/'task' to call.")
                try:
                    module = importlib.import_module(module_name)
                except ImportError as e:
                    raise UnknownTaskError(
                        f"Cannot import module '{module_name}' (node '{node_id}'): {e}"
                    )
                if not hasattr(module, func_name):
                    raise UnknownTaskError(
                        f"No function '{func_name}' in module '{module_name}' (node '{node_id}')."
                    )
                if func_name == 'download_file':
                    static_kwargs = attrs.get('kwargs', {})
                    if not self.job_inputs.get('path', static_kwargs.get('path')):
                        raise UnknownTaskError(
                            f"Node '{node_id}' (download_file) has no 'path' — "
                            "supply one via job_inputs (the request) or the node's kwargs."
                        )
            elif driver_name == 'http' and not attrs.get('url'):
                raise UnknownTaskError(f"Node '{node_id}' (http driver) has no 'url' attribute.")

            if attrs.get('service'):
                try:
                    validate_mode()
                except ValueError as e:
                    raise UnknownTaskError(f"Node '{node_id}': {e}")

    def _merge_predecessor_payloads(self, node_id):
        """Merge predecessor result envelopes' `data` into a single payload
        dict.

        Raises PayloadConflictError if two predecessors disagree on the
        same key, instead of silently letting one overwrite the other.
        """
        merged = {}
        for predecessor in self.dag.get_predecessors(node_id):
            envelope = self.node_results.get(predecessor)
            if not envelope or envelope.get('status') != 'success':
                continue
            data = envelope.get('data')
            if not isinstance(data, dict):
                continue
            for key, value in data.items():
                if key in merged and merged[key] != value:
                    raise PayloadConflictError(
                        f"Node '{node_id}': predecessors disagree on '{key}' "
                        f"({merged[key]!r} vs {value!r})"
                    )
                merged[key] = value
        return merged

    def execute_node(self, node_id):
        """Execute a single node by dispatching to its driver."""
        node_attributes = self.dag.get_node_attributes(node_id)
        task_name = self._task_name(node_id)
        driver_name = node_attributes.get('driver', 'python')
        driver = DRIVERS.get(driver_name)
        if driver is None:
            raise UnknownTaskError(f"Unknown driver '{driver_name}' (node '{node_id}').")

        static_kwargs = node_attributes.get('kwargs', {})

        if task_name in NON_PAYLOAD_TASKS:
            if task_name == 'download_file':
                # Runtime request input takes priority over any static
                # default in the workflow JSON (useful for testing a
                # template without a real request).
                path = self.job_inputs.get('path', static_kwargs.get('path'))
                prompts = self.job_inputs.get('prompts', static_kwargs.get('prompts'))
                if not path:
                    raise ValueError(
                        f"Node '{node_id}' (download_file) has no 'path' — "
                        "supply one via job_inputs (the request) or the node's kwargs."
                    )
                inputs = {'path': path, 'job_id': self.job_id, 'prompts': prompts}
            else:  # finalize_results
                inputs = {
                    'job_id': self.job_id,
                    'job_type': self.job_type,
                    'callback_url': self.callback_url,
                }
            call_attributes = {**node_attributes, 'call': 'kwargs'}
        else:
            payload = self._merge_predecessor_payloads(node_id)
            for key, value in static_kwargs.items():
                if key in payload and payload[key] != value:
                    raise PayloadConflictError(
                        f"Node '{node_id}': static kwargs disagree with predecessor output on "
                        f"'{key}' ({payload[key]!r} vs {value!r})"
                    )
                payload[key] = value
            inputs = payload
            call_attributes = {**node_attributes, 'call': 'payload'}

        service_name = node_attributes.get('service')
        if service_name:
            try:
                ensure_ready(service_name)
            except ServiceNotReadyError as e:
                envelope = failure(str(e))
            else:
                try:
                    envelope = driver.run(call_attributes, inputs)
                finally:
                    release(service_name)
        else:
            envelope = driver.run(call_attributes, inputs)

        self.node_results[node_id] = envelope
        self.executed_nodes.add(node_id)

        predecessors = self.dag.get_predecessors(node_id)
        if predecessors:
            print(f"  Dependencies: {predecessors}")
        print(f"Executed {task_name} (ID: {node_id}) — status: {envelope['status']}")

        if envelope['status'] != 'success':
            message = f"Node '{node_id}' ({task_name}) failed: {envelope['error']}"
            if self.on_failure == 'stop':
                raise TaskExecutionError(message)
            print(f"[DAGEngine] {message} — on_failure='continue', proceeding.")

        return envelope

    def execute(self):
        """Execute the DAG in topological order."""
        self._validate_task_names()
        print("Starting DAG execution...")
        for node_id in self.dag.topological_sort():
            if node_id not in self.executed_nodes:
                self.execute_node(node_id)
        print("DAG execution completed.")
        return self.node_results

    def execute_parallel(self, max_workers=None):
        """Execute the DAG within a single job, running independent nodes
        concurrently.

        Nodes are grouped into topological generations (a generation
        contains only nodes with no dependency on one another); nodes
        within a generation run concurrently via a thread pool, and
        generations run in order so every node's dependencies are always
        finished before it starts.
        """
        self._validate_task_names()
        print("Starting parallel DAG execution...")
        for generation in nx.topological_generations(self.dag.graph):
            pending = [n for n in generation if n not in self.executed_nodes]
            if not pending:
                continue
            with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = {executor.submit(self.execute_node, node_id): node_id for node_id in pending}
                for future in concurrent.futures.as_completed(futures):
                    future.result()
        print("DAG execution completed.")
        return self.node_results

    def get_node_status(self, node_id):
        """Get the execution status of a specific node."""
        return {
            'id': node_id,
            'executed': node_id in self.executed_nodes,
            'attributes': self.dag.get_node_attributes(node_id),
            'predecessors': self.dag.get_predecessors(node_id),
            'successors': self.dag.get_successors(node_id)
        }

    def get_execution_plan(self):
        """Return the execution plan for debugging."""
        plan = []
        for node_id in self.dag.topological_sort():
            status = self.get_node_status(node_id)
            plan.append(status)
        return plan
