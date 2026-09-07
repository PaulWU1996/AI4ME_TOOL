import networkx as nx
import json

class DAG:
    def __init__(self):
        self.graph = nx.DiGraph()

    def add_node(self, node_id, **attributes):
        """Add a node to the DAG with optional attributes."""
        self.graph.add_node(node_id, **attributes)

    def add_edge(self, from_node, to_node, **attributes):
        """Add an edge between two nodes in the DAG."""
        self.graph.add_edge(from_node, to_node, **attributes)

    def has_cycle(self):
        """Check if the DAG contains any cycles."""
        try:
            nx.find_cycle(self.graph, orientation='original')
            return True
        except nx.NetworkXNoCycle:
            return False

    def topological_sort(self):
        """Return a list of nodes in topological order."""
        return list(nx.topological_sort(self.graph))

    def get_predecessors(self, node_id):
        """Get all predecessors of a node."""
        return list(self.graph.predecessors(node_id))

    def get_successors(self, node_id):
        """Get all successors of a node."""
        return list(self.graph.successors(node_id))

    def get_all_nodes(self):
        """Get all nodes in the DAG."""
        return list(self.graph.nodes())

    def get_all_edges(self):
        """Get all edges in the DAG."""
        return list(self.graph.edges())

    def get_node_attributes(self, node_id):
        """Get attributes of a specific node."""
        return self.graph.nodes[node_id]

    def get_edge_attributes(self, from_node, to_node):
        """Get attributes of a specific edge."""
        try:
            return self.graph[from_node][to_node]
        except KeyError:
            return {}

    def remove_node(self, node_id):
        """Remove a node and all its edges."""
        self.graph.remove_node(node_id)

    def remove_edge(self, from_node, to_node):
        """Remove an edge between two nodes."""
        self.graph.remove_edge(from_node, to_node)

    def is_valid_dag(self):
        """Check if the graph is a valid DAG (no cycles)."""
        return not self.has_cycle()

    def get_node_count(self):
        """Get the number of nodes in the DAG."""
        return self.graph.number_of_nodes()

    def get_edge_count(self):
        """Get the number of edges in the DAG."""
        return self.graph.number_of_edges()

    def get_ancestors(self, node_id):
        """Get all ancestors of a node (excluding the node itself)."""
        return nx.ancestors(self.graph, node_id)

    def get_descendants(self, node_id):
        """Get all descendants of a node (excluding the node itself)."""
        return nx.descendants(self.graph, node_id)

class Parser:
    def __init__(self, json_path):
        self.dag = DAG()
        self.parse(json_path)

    def parse(self, json_path):
        """parse a JSON file and build the DAG from it.

        Args:
            json_path (str): path to the JSON file containing the DAG definition.
        """

        with open(json_path, 'r') as f:
            workflow = json.load(f)

        tasks = workflow.get('tasks', [])

        for task in tasks:
            node_attributes = dict(task.get('attributes', {}))
            if 'task' in task:
                node_attributes['task'] = task['task']
            self.dag.add_node(task['id'], **node_attributes)

        declared_ids = {task['id'] for task in tasks}

        for task in tasks:
            # Handle 'depends_on' field properly (as used in your JSON)
            dependencies = task.get('depends_on', [])
            for dependency in dependencies:
                if dependency not in declared_ids:
                    raise ValueError(
                        f"Task '{task['id']}' depends on undeclared task '{dependency}'."
                    )
                self.dag.add_edge(dependency, task['id'])  # Add edge with dependency

        if not self.dag.is_valid_dag():
            raise ValueError("The provided workflow contains cycles and is not a valid DAG.")
