"""Parser / DAG — pure, no I/O beyond a temp JSON file.

Replaces the old top-level test_parser.py, which imported `worker.dag.parser`
(the package moved to top-level dag/), read a `full_workflow.json` that does
not exist, and swallowed every exception in a bare try/except — so it could
never fail.
"""
import json
import os

import pytest

from dag.parser import DAG, Parser

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# --------------------------------------------------------------------------
# Structure
# --------------------------------------------------------------------------

def test_parses_nodes_edges_and_order(make_dag):
    parsed = make_dag([
        {"id": "download", "task": "download_file", "depends_on": []},
        {"id": "visual", "task": "process_visual", "depends_on": ["download"]},
        {"id": "audio", "task": "process_audio", "depends_on": ["download"]},
        {"id": "final", "task": "finalize_results", "depends_on": ["visual", "audio"]},
    ])

    assert parsed.dag.get_node_count() == 4
    assert parsed.dag.get_edge_count() == 4

    order = parsed.dag.topological_sort()
    assert order[0] == "download"
    assert order[-1] == "final"
    assert order.index("visual") < order.index("final")
    assert order.index("audio") < order.index("final")


def test_captures_metadata_and_settings(make_dag):
    parsed = make_dag(
        [{"id": "a", "task": "noop", "depends_on": []}],
        settings={"on_failure": "continue"},
        workflow={"name": "demo", "version": "2.1"},
    )

    assert parsed.metadata == {"name": "demo", "version": "2.1"}
    assert parsed.settings == {"on_failure": "continue"}


def test_predecessors_successors_ancestors_descendants(make_dag):
    parsed = make_dag([
        {"id": "a", "task": "noop", "depends_on": []},
        {"id": "b", "task": "noop", "depends_on": ["a"]},
        {"id": "c", "task": "noop", "depends_on": ["b"]},
    ])

    assert parsed.dag.get_predecessors("b") == ["a"]
    assert parsed.dag.get_successors("b") == ["c"]
    assert parsed.dag.get_ancestors("c") == {"a", "b"}
    assert parsed.dag.get_descendants("a") == {"b", "c"}


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------

def test_cycle_is_rejected(make_dag):
    with pytest.raises(ValueError, match="cycle"):
        make_dag([
            {"id": "a", "task": "noop", "depends_on": ["b"]},
            {"id": "b", "task": "noop", "depends_on": ["a"]},
        ])


def test_self_loop_is_rejected(make_dag):
    with pytest.raises(ValueError, match="cycle"):
        make_dag([{"id": "a", "task": "noop", "depends_on": ["a"]}])


def test_dependency_on_undeclared_task_is_rejected(make_dag):
    with pytest.raises(ValueError, match="undeclared task 'ghost'"):
        make_dag([{"id": "a", "task": "noop", "depends_on": ["ghost"]}])


def test_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        Parser(str(tmp_path / "nope.json"))


def test_malformed_json_raises(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{not json")
    with pytest.raises(json.JSONDecodeError):
        Parser(str(path))


# --------------------------------------------------------------------------
# Attribute layering (parser.py's documented precedence)
# --------------------------------------------------------------------------

def test_top_level_fields_win_over_legacy_nested_attributes(make_dag):
    parsed = make_dag([{
        "id": "a",
        "depends_on": [],
        "attributes": {"driver": "python", "module": "legacy", "only_nested": 1},
        "driver": "http",
        "url": "http://example.invalid/run",
    }])

    attrs = parsed.dag.get_node_attributes("a")
    assert attrs["driver"] == "http"          # top-level wins
    assert attrs["module"] == "legacy"        # nested-only survives
    assert attrs["only_nested"] == 1
    assert attrs["url"] == "http://example.invalid/run"


def test_structural_keys_never_leak_into_node_attributes(make_dag):
    parsed = make_dag([
        {"id": "a", "task": "noop", "depends_on": []},
        {"id": "b", "task": "noop", "depends_on": ["a"], "attributes": {"x": 1}},
    ])

    attrs = parsed.dag.get_node_attributes("b")
    assert "id" not in attrs
    assert "depends_on" not in attrs
    assert "attributes" not in attrs
    assert attrs == {"x": 1, "task": "noop"}


def test_kwargs_and_service_survive_as_attributes(make_dag):
    parsed = make_dag([{
        "id": "a", "task": "process_visual", "depends_on": [],
        "service": "visualservice",
        "kwargs": {"model": "narrative", "threshold": 0.5},
    }])

    attrs = parsed.dag.get_node_attributes("a")
    assert attrs["service"] == "visualservice"
    assert attrs["kwargs"] == {"model": "narrative", "threshold": 0.5}


# --------------------------------------------------------------------------
# DAG helper surface
# --------------------------------------------------------------------------

def test_dag_mutation_helpers():
    dag = DAG()
    dag.add_node("a", task="noop")
    dag.add_node("b", task="noop")
    dag.add_edge("a", "b", weight=3)

    assert dag.get_edge_attributes("a", "b") == {"weight": 3}
    assert dag.get_edge_attributes("b", "a") == {}   # missing edge -> {}
    assert dag.is_valid_dag()

    dag.remove_edge("a", "b")
    assert dag.get_edge_count() == 0
    dag.remove_node("a")
    assert dag.get_all_nodes() == ["b"]


def test_has_cycle_detects_a_cycle_directly():
    dag = DAG()
    dag.add_edge("a", "b")
    dag.add_edge("b", "a")
    assert dag.has_cycle() is True
    assert dag.is_valid_dag() is False


# --------------------------------------------------------------------------
# The real registered workflow
# --------------------------------------------------------------------------

def test_shipped_full_pipeline_workflow_parses():
    path = os.path.join(REPO_ROOT, "workflows", "full_pipeline_1.0.json")
    parsed = Parser(path)

    assert parsed.metadata == {"name": "full_pipeline", "version": "1.0"}
    assert parsed.settings["on_failure"] == "stop"
    assert parsed.dag.topological_sort() == ["download", "visual", "audio", "final"]


def test_shipped_workflow_declares_its_services(make_dag):
    """The readiness layer is only reachable from a workflow whose nodes say
    which service they need. Before this was declared, dag/readiness.py was
    dead code as far as the shipped pipeline was concerned, and lifecycle
    happened inline inside the task bodies instead."""
    path = os.path.join(REPO_ROOT, "workflows", "full_pipeline_1.0.json")
    spec = json.loads(open(path).read())
    declared = {t["id"]: t.get("service") for t in spec["tasks"]}

    assert declared["visual"] == "visualservice"
    assert declared["audio"] == "audioservice"
    assert declared["download"] is None      # pure I/O, no service
    assert declared["final"] is None


def test_shipped_workflow_declares_finalize_expectations():
    """finalize_results needs to know what counts as done for this workflow;
    without it the job fails at the last node for any name that is not one of
    the legacy job_types."""
    path = os.path.join(REPO_ROOT, "workflows", "full_pipeline_1.0.json")
    spec = json.loads(open(path).read())
    final = next(t for t in spec["tasks"] if t["task"] == "finalize_results")
    assert final["kwargs"]["expects"] == ["audio", "visual"]


def test_duplicate_task_id_is_rejected(make_dag):
    """networkx's add_node() merges attributes when called twice with the
    same id, so without an explicit check a workflow declaring two tasks
    under one id would parse into a graph with fewer nodes than tasks —
    losing a task with no diagnostic at all."""
    with pytest.raises(ValueError, match="Duplicate task id 'dup'"):
        make_dag([
            {"id": "dup", "task": "first", "depends_on": []},
            {"id": "dup", "task": "second", "depends_on": []},
        ])


def test_duplicate_id_check_does_not_reject_repeated_dependencies(make_dag):
    """Two tasks may legitimately depend on the same predecessor."""
    parsed = make_dag([
        {"id": "root", "task": "t", "depends_on": []},
        {"id": "a", "task": "t", "depends_on": ["root"]},
        {"id": "b", "task": "t", "depends_on": ["root"]},
    ])
    assert parsed.dag.get_node_count() == 3
