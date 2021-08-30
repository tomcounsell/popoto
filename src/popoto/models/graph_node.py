import logging
from abc import ABC
import redis
from redisgraph import Node, Edge, Graph, Path
from ..models.key_value import KeyValueModel
from ..redis_db import POPOTO_REDIS_DB
from redisgraph import Node as GraphNode
from redisgraph import Edge as GraphEdge
from ..exceptions import ModelException

logger = logging.getLogger(__name__)


class GraphNodeException(ModelException):
    pass


class GraphNodeModel(KeyValueModel):
    """
    stores things and their relationships in redis graph database
    otherwise acts as KeyValueModel
    recommend to uniquely identify the instance with a key prefix or suffix
    prefixes are for more specific categories of objects (eg. mammal:human:woman:Lisa )
    suffixes are for specific attributes (eg. Lisa:eye_color, Lisa:age, etc)
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.graph = Graph(self.__class__.__name__, POPOTO_REDIS_DB)
        

    @property
    def graph_node(self):
        return GraphNode(
            alias=self._db_key,
            label=self.__class__.__name__,
            properties=self.value if isinstance(self.value, dict) else {'value': self.value}
        )

    @property
    def relationships(self, relationship_type: str, filters: dict = None):
        # params = {'purpose': "pleasure"}
        # query = """MATCH (p:person)-[v:visited {purpose:$purpose}]->(c:country)
        # 		   RETURN p.name, p.age, v.purpose, c.name"""

        self.query(f"""MATCH (from:{self.__class__.__name__})-[rel:{relationship_type}]->(to:{self.__class__.__name__})
                    RETURN from.db_key, rel.db_key, to.db_key """)


    def save(self, pipeline=None, *args, **kwargs):
        super().save(pipeline, *args, **kwargs)
        self.graph.add_node(self.graph_node)
        self.graph.commit()


    def query(self, query_string: str, params: dict = None):
        result = self.graph.query(query_string, params)
        result_db_keys = [record[2] for record in result.result_set]
        return [self.__class__.get(db_key) for db_key in result_db_keys]


    def _set_relationship_to_graphnode(self, context: dict, graph_node: GraphNode) -> None:
        self.graph_edges.append(
            GraphEdge(self.graph_node, 'correlation', graph_node, properties=context)
        )

    def save(self):
        self.graph.add_node(self.graph_node)
        for edge in self.graph_edges:
            self.graph.add_edge(edge)
        self.graph.commit()


# class GraphRelationship:
#     def __init__(self):
#         self.__name__
