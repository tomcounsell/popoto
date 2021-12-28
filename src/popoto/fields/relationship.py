import redis
from redisgraph import Node, Edge, Path
from ..redis_db import REDIS_GRAPH
from ..models.base import Model
from .field import Field
import logging
logger = logging.getLogger('POPOTO.GeoField')
from ..redis_db import POPOTO_REDIS_DB


class Relationship(Field):
    """
    A field that stores references to one or more other model instances.
    """
    model: Model = None
    many: bool = False
    null: bool = True


    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        relationship_field_defaults = {
            'model': None,
            'many': False,
            'null': True,
        }
        self.field_defaults.update(relationship_field_defaults)
        # set field options, let kwargs override
        for k, v in relationship_field_defaults.items():
            setattr(self, k, kwargs.get(k, v))

    def example_script:
        john = Node(label='person', properties={'name': 'John Doe', 'age': 33, 'gender': 'male', 'status': 'single'})
        REDIS_GRAPH.add_node(john)

        john = Node(label='person', properties={'name': 'John Doe', 'age': 33, 'gender': 'male', 'status': 'single'})
        REDIS_GRAPH.add_node(john)

        japan = Node(label='country', properties={'name': 'Japan'})
        REDIS_GRAPH.add_node(japan)

        edge = Edge(john, 'visited', japan, properties={'purpose': 'pleasure'})
        REDIS_GRAPH.add_edge(edge)

        REDIS_GRAPH.commit()

        query = """MATCH (p:person)-[v:visited {purpose:"pleasure"}]->(c:country)
                   RETURN p.name, p.age, v.purpose, c.name"""

        result = REDIS_GRAPH.query(query)
