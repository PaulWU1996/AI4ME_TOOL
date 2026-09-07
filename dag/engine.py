import concurrent.futures

import networkx as nx

from .parser import DAG
from tasks import (
    download_file,
    process_visual,
    process_audio,
    finalize_results,
    process_summarise,
    process_tagging,
    speaker_extent,
    segment_extent,
    transcript_to_text,
)

# Maps a node's "task" attribute to the real worker/tasks.py function.
TASK_REGISTRY = {
    "download_file": download_file,
    "process_visual": process_visual,
    "process_audio": process_audio,
    "finalize_results": finalize_results,
    "process_summarise": process_summarise,
    "process_tags": process_tagging,
    "speaker_extent": speaker_extent,
    "segment_extent": segment_extent,
    "transcript_to_text": transcript_to_text,
}

# Tasks that don't follow the payload-in/payload-out convention used by
# everything else in worker/tasks.py, and so need special-cased arguments
# instead of a merged predecessor payload.
NON_PAYLOAD_TASKS = {"download_file", "finalize_results"}


class PayloadConflictError(Exception):
    """Raised when two predecessors of a node disagree on the same payload key."""


class UnknownTaskError(Exception):
    """Raised when a node's `task` attribute has no matching TASK_REGISTRY entry."""


class DAGEngine:
    def __init__(self, dag: DAG, job_id, job_type="full", callback_url=None, job_inputs=None):
        """
        job_inputs: per-job runtime input from the client request (e.g.
        {"path": ..., "prompts": ...}), analogous to `ProcessRequest` in
        controller/main.py. Distinct from a node's static `kwargs` in the
        workflow JSON, which describe the reusable pipeline template.
        """
        self.dag = dag
        self.job_id = job_id
        self.job_type = job_type
        self.callback_url = callback_url
        self.job_inputs = job_inputs or {}
        self.executed_nodes = set()
        self.node_results = {}

    def _validate_task_names(self):
        """Check every node's `task` resolves in TASK_REGISTRY before any
        node runs, so a typo'd task fails fast instead of partway through
        the DAG (after earlier nodes already had real side effects)."""
        for node_id in self.dag.get_all_nodes():
            task_name = self.dag.get_node_attributes(node_id).get('task', node_id)
            if task_name not in TASK_REGISTRY:
                raise UnknownTaskError(f"No registered task for '{task_name}' (node '{node_id}').")

    def _merge_predecessor_payloads(self, node_id):
        """Merge predecessor outputs into a single payload dict.

        Raises PayloadConflictError if two predecessors disagree on the
        same key, instead of silently letting one overwrite the other.
        """
        merged = {}
        for predecessor in self.dag.get_predecessors(node_id):
            result = self.node_results.get(predecessor)
            if not isinstance(result, dict):
                continue
            for key, value in result.items():
                if key in merged and merged[key] != value:
                    raise PayloadConflictError(
                        f"Node '{node_id}': predecessors disagree on '{key}' "
                        f"({merged[key]!r} vs {value!r})"
                    )
                merged[key] = value
        return merged

    def execute_node(self, node_id):
        """Execute a single node by dispatching to its real task function."""
        node_attributes = self.dag.get_node_attributes(node_id)
        task_name = node_attributes.get('task', node_id)
        task_func = TASK_REGISTRY.get(task_name)
        if task_func is None:
            raise UnknownTaskError(f"No registered task for '{task_name}' (node '{node_id}').")

        static_kwargs = node_attributes.get('kwargs', {})

        if task_name == "download_file":
            # Runtime request input takes priority over any static default
            # in the workflow JSON (useful for testing a template without
            # a real request).
            path = self.job_inputs.get("path", static_kwargs.get("path"))
            prompts = self.job_inputs.get("prompts", static_kwargs.get("prompts"))
            if not path:
                raise ValueError(
                    f"Node '{node_id}' (download_file) has no 'path' — "
                    "supply one via job_inputs (the request) or the node's kwargs."
                )
            result = task_func(path, self.job_id, prompts=prompts)
        elif task_name == "finalize_results":
            result = task_func(
                self.job_id,
                job_type=self.job_type,
                callback_url=self.callback_url,
            )
        else:
            payload = self._merge_predecessor_payloads(node_id)
            for key, value in static_kwargs.items():
                if key in payload and payload[key] != value:
                    raise PayloadConflictError(
                        f"Node '{node_id}': static kwargs disagree with predecessor output on "
                        f"'{key}' ({payload[key]!r} vs {value!r})"
                    )
                payload[key] = value
            result = task_func(payload)

        self.node_results[node_id] = result
        self.executed_nodes.add(node_id)

        predecessors = self.dag.get_predecessors(node_id)
        if predecessors:
            print(f"  Dependencies: {predecessors}")
        print(f"Executed {task_name} (ID: {node_id})")
        return result

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
