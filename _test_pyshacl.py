from pyshacl import validate
from rdflib import Graph

# Create a simple data graph
dg = Graph()
ttl_data = '<http://ex.org/S> <http://ex.org/p> "hello" .'
dg.parse(data=ttl_data, format='turtle')

# Create a simple shapes graph
sg = Graph()
ttl_shapes = '''
@prefix sh: <http://www.w3.org/ns/shacl#> .
<http://ex.org/Shape> a sh:NodeShape ;
  sh:targetClass <http://ex.org/T> .
'''
sg.parse(data=ttl_shapes, format='turtle')

result = validate(dg, shacl_graph=sg)
print(type(result))
print(result)
if isinstance(result, tuple):
    print("length:", len(result))
    for i, item in enumerate(result):
        print(f"  [{i}] {type(item).__name__}: {str(item)[:100]}")
