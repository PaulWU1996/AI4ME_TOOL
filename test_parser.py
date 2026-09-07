from worker.dag.parser import Parser
import os

# Define the path
workflow_path = 'full_workflow.json'

try:
    print(f"Parsing workflow from: {workflow_path}")
    
    # Check if file exists
    if not os.path.exists(workflow_path):
        print(f"Error: File {workflow_path} not found!")
    else:
        parser = Parser(workflow_path)
        
        print("Successfully parsed DAG!")
        print(f"Node count: {parser.dag.get_node_count()}")
        print(f"Edge count: {parser.dag.get_edge_count()}")
        print(f"Topological order: {parser.dag.topological_sort()}")
        
        # Print node details
        for node in parser.dag.get_all_nodes():
            print(f"Node: {node}, Attributes: {parser.dag.get_node_attributes(node)}")
            
        # Print edges
        print("\nEdges:")
        for edge in parser.dag.get_all_edges():
            print(f"Edge: {edge[0]} -> {edge[1]}, Attributes: {parser.dag.get_edge_attributes(edge[0], edge[1])}")
        
        # Check for cycles
        if parser.dag.has_cycle():
            print("Warning: DAG contains cycles!")
        else:
            print("DAG is valid (no cycles)")
            
except Exception as e:
    print(f"Error parsing DAG: {e}")
