import concurrent.futures
import importlib
import time

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


class PayloadConflictError(Exception):
    """Raised when two predecessors of a node disagree on the same payload key."""


class UnknownTaskError(Exception):
    """Raised when a node's driver/task attributes don't resolve to a callable."""


class TaskExecutionError(Exception):
    """Raised when a node's driver returns a failure envelope."""


DEFAULT_RETRY_BACKOFF = 1.0
DEFAULT_RETRY_BACKOFF_MAX = 60.0


ON_FAILURE_MODES = {"stop", "continue"}


class DAGEngine:
    def __init__(self, dag: DAG, job_id, job_type="full", callback_url=None, job_inputs=None,
                 on_failure="stop", retries=0, retry_backoff=DEFAULT_RETRY_BACKOFF,
                 retry_backoff_max=DEFAULT_RETRY_BACKOFF_MAX):
        """
        job_inputs: per-job runtime input from the client request (e.g.
        {"path": ..., "prompts": ...}), analogous to `ProcessRequest` in
        controller/main.py. Distinct from a node's static `kwargs` in the
        workflow JSON, which describe the reusable pipeline template.

        Every node builds its `inputs` from `_build_node_inputs()`:
        - a node declaring `call: "kwargs"` reads a slice of the job context
          (`job_inputs` + `job_id`/`job_type`/`callback_url`) via its
          `inject` list, over static `kwargs` defaults — this is how a
          download node picks up the request's `path`/`prompts`, and a
          finalize node picks up `job_id`/`job_type`/`callback_url`, without
          the engine knowing their task names. `requires` fails the node
          fast if any named key is missing after the merge.
        - any other node (the default) receives the merged predecessor
          payload plus its static `kwargs`, unchanged.

        on_failure: "stop" (default) halts the whole run on the first
        failed node; "continue" logs the failure and keeps walking the
        remaining nodes — note downstream nodes that actually depend on
        the failed node's output may then fail too (missing payload
        keys), since "continue" doesn't skip them, only avoids halting
        early. Sourced from a workflow's `settings.on_failure`, e.g. by
        passing `Parser(...).settings.get("on_failure", "stop")`.

        retries: how many times to re-attempt a node that fails, on top of
        the first attempt (0 = today's behaviour, one shot). Retry lives
        here rather than on the Celery task because `tasks.execute_workflow`
        is a single Celery task covering the *entire* DAG — a Celery-level
        retry would re-run every node, including the expensive GPU ones,
        to recover from one transient download. A node-level default can be
        set per workflow via `settings.retries` and overridden per node with
        a `retries` attribute.

        Note this replaces what `@app.task(autoretry_for=...)` gives the
        legacy chains: the python driver calls a task's function directly,
        so Celery's retry machinery never engages on the DAG path.

        retry_backoff / retry_backoff_max: seconds before the first retry,
        doubling each attempt, capped. Mirrors Celery's retry_backoff.

        Retries re-attempt the whole bracket, service acquisition included,
        so a node whose service failed to start gets a fresh cold start
        rather than being handed the same broken container. A retried node's
        side effects run again, so only declare retries on nodes that
        tolerate that.
        """
        if on_failure not in ON_FAILURE_MODES:
            raise ValueError(f"Unknown on_failure mode '{on_failure}'. Choose from: {ON_FAILURE_MODES}")
        self.dag = dag
        self.job_id = job_id
        self.job_type = job_type
        self.callback_url = callback_url
        self.job_inputs = job_inputs or {}
        self.job_context = {
            **self.job_inputs,
            "job_id": job_id,
            "job_type": job_type,
            "callback_url": callback_url,
        }
        self.on_failure = on_failure
        self.retries = retries
        self.retry_backoff = retry_backoff
        self.retry_backoff_max = retry_backoff_max
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
                if attrs.get('call') == 'kwargs':
                    # A kwargs node reads job context, so its `requires`
                    # keys must already be satisfiable at pre-flight time.
                    inputs = self._build_node_inputs(node_id)
                    missing = [k for k in attrs.get('requires', []) if inputs.get(k) is None]
                    if missing:
                        raise UnknownTaskError(
                            f"Node '{node_id}' is missing required inputs {missing} — "
                            "supply them via job_inputs (the request) or the node's kwargs."
                        )
            elif driver_name == 'http' and not attrs.get('url'):
                raise UnknownTaskError(f"Node '{node_id}' (http driver) has no 'url' attribute.")

            retries = attrs.get('retries', self.retries)
            if not isinstance(retries, int) or isinstance(retries, bool) or retries < 0:
                raise UnknownTaskError(
                    f"Node '{node_id}': 'retries' must be a non-negative integer, got {retries!r}."
                )

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

    def _build_node_inputs(self, node_id):
        """Build the `inputs` dict handed to a node's driver.

        A node declaring `call: "kwargs"` gets its static `kwargs` merged
        with the slice of job context named in its `inject` list — job
        context wins over template defaults, and a key the context doesn't
        hold simply leaves the template's own value in place. Every other
        node (the default) gets the merged predecessor payload plus its
        static `kwargs`, with conflicts rejected.
        """
        attributes = self.dag.get_node_attributes(node_id)
        static_kwargs = attributes.get('kwargs', {})

        if attributes.get('call') == 'kwargs':
            inject = attributes.get('inject', [])
            return {
                **static_kwargs,
                **{k: self.job_context[k]
                   for k in inject if self.job_context.get(k) is not None},
            }

        payload = self._merge_predecessor_payloads(node_id)
        for key, value in static_kwargs.items():
            if key in payload and payload[key] != value:
                raise PayloadConflictError(
                    f"Node '{node_id}': static kwargs disagree with predecessor output on "
                    f"'{key}' ({payload[key]!r} vs {value!r})"
                )
            payload[key] = value
        return payload

    def _attempt_node(self, node_id, node_attributes, driver, call_attributes, inputs):
        """One attempt at a node: acquire its service if it declares one, run
        the driver, release. Always returns an envelope, never raises for a
        task-level failure, so the retry loop above can decide what to do."""
        service_name = node_attributes.get('service')
        if not service_name:
            return driver.run(call_attributes, inputs)

        try:
            ensure_ready(service_name)
        except ServiceNotReadyError as e:
            return failure(str(e))
        try:
            return driver.run(call_attributes, inputs)
        finally:
            release(service_name)

    def execute_node(self, node_id):
        """Execute a single node by dispatching to its driver."""
        node_attributes = self.dag.get_node_attributes(node_id)
        task_name = self._task_name(node_id)
        driver_name = node_attributes.get('driver', 'python')
        driver = DRIVERS.get(driver_name)
        if driver is None:
            raise UnknownTaskError(f"Unknown driver '{driver_name}' (node '{node_id}').")

        # Inputs are built declaratively (see _build_node_inputs): a node
        # opts out of the merged-predecessor convention with `call: "kwargs"`
        # and names the job-context slice it wants via `inject`. The drivers
        # stay generic callers — this is the only place the engine branches
        # on input shape, never on task name.
        inputs = self._build_node_inputs(node_id)
        call_attributes = dict(node_attributes)

        retries = node_attributes.get('retries', self.retries)
        envelope = None
        for attempt in range(retries + 1):
            envelope = self._attempt_node(node_id, node_attributes, driver, call_attributes, inputs)
            if envelope['status'] == 'success' or attempt == retries:
                break
            delay = min(self.retry_backoff * (2 ** attempt), self.retry_backoff_max)
            print(
                f"[DAGEngine] Node '{node_id}' ({task_name}) attempt {attempt + 1}/"
                f"{retries + 1} failed: {envelope['error']} — retrying in {delay}s."
            )
            time.sleep(delay)

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

    def terminal_result(self):
        """The result a client should see for this job.

        The node declaring `terminal: true` produces the job's single
        meaningful output (the workflow's summary node, conventionally the
        finalize step), and that is what the legacy Celery chains' final
        task returned, so returning it here keeps one `/status` contract
        across both paths. Workflows with no terminal node have no such
        summary, so the full node map is returned instead.

        The per-node envelopes are never lost: run_summary() writes them to
        the job workspace for debugging.
        """
        for node_id in reversed(self.dag.topological_sort()):
            if self.dag.get_node_attributes(node_id).get('terminal'):
                envelope = self.node_results.get(node_id)
                if envelope and envelope.get('status') == 'success':
                    return envelope.get('data')
        return self.node_results

    def run_summary(self):
        """Per-node envelopes plus the resolved execution order, for writing
        alongside a job's outputs."""
        return {
            'order': self.dag.topological_sort(),
            'executed': sorted(self.executed_nodes),
            'nodes': self.node_results,
        }

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
