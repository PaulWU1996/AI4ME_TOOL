import json

import networkx as nx


class DAG:
    """A directed acyclic graph of workflow nodes.

    Kept to what the controller's composer and workflow validation actually
    use: build (add_node/add_edge), validate (has_cycle/is_valid_dag), and
    traverse (topological_sort, get_predecessors, get_all_nodes,
    get_node_attributes).
    """

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

    def is_valid_dag(self):
        """Check if the graph is a valid DAG (no cycles)."""
        return not self.has_cycle()

    def topological_sort(self):
        """Return a list of nodes in topological order."""
        return list(nx.topological_sort(self.graph))

    def get_predecessors(self, node_id):
        """Get all predecessors of a node."""
        return list(self.graph.predecessors(node_id))

    def get_all_nodes(self):
        """Get all nodes in the DAG."""
        return list(self.graph.nodes())

    def get_node_attributes(self, node_id):
        """Get attributes of a specific node."""
        return self.graph.nodes[node_id]


class Parser:
    def __init__(self, json_path):
        self.dag = DAG()
        self.metadata = {}
        self.settings = {}
        self.parse(json_path)

    def parse(self, json_path):
        """Parse a JSON file and build the DAG from it.

        Args:
            json_path (str): path to the JSON file containing the DAG
                definition.
        """
        with open(json_path, 'r') as f:
            workflow = json.load(f)

        self.metadata = workflow.get('workflow', {})
        self.settings = workflow.get('settings', {})

        structural_keys = {'id', 'depends_on'}
        seen_ids = set()
        for task in workflow.get('tasks', []):
            if 'id' not in task:
                raise ValueError("Every task must have an 'id' field.")
            if task['id'] in seen_ids:
                raise ValueError(f"Duplicate task id '{task['id']}' in workflow.")
            seen_ids.add(task['id'])
            
            # store all other attributes of the task as node attributes
            node_attributes = {}
            for key, value in task.items():
                if key not in structural_keys:
                    node_attributes[key] = value
            self.dag.add_node(task['id'], **node_attributes)

        declared_ids = {task['id'] for task in workflow.get('tasks', [])}

        for task in workflow.get('tasks', []):
            dependencies = task.get('depends_on', [])
            for dependency in dependencies:
                if dependency not in declared_ids:
                    raise ValueError(
                        f"Task '{task['id']}' depends on undeclared task '{dependency}'."
                    )
                self.dag.add_edge(dependency, task['id'])

        if not self.dag.is_valid_dag():
            raise ValueError("The provided workflow contains cycles and is not a valid DAG.")
        
        
    # validate_http_node
    # ensure a node designated as an HTTP service has the required fields and valid values

    # validate_docker_node
    # ensure a node designated as a Docker service has the required fields and valid values