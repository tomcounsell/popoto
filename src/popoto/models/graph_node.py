import logging
from abc import ABC
import redis
from redisgraph import Node, Edge, Graph, Path
from abc import ABC
from redisgraph import Node as GraphNode
from redisgraph import Edge as GraphEdge

from ..redis_db import POPOTO_REDIS_DB
from ..exceptions import ModelException

logger = logging.getLogger(__name__)


class AbstractGraphNodeModel(ABC):
    graph_node = GraphNode()
    graph_edges = []

    def __init__(self, name, *args, **kwargs):
        self.base_class = self.__class__.__name__
        self.graph_node = GraphNode(
            label=name,
            properties={'id': 'uuid', 'importance': 1, 'active': True}
        )

    def _set_relationship_to_graphnode(self, context: dict, graph_node: GraphNode) -> None:
        self.graph_edges.append(
            GraphEdge(self.graph_node, 'correlation', graph_node, properties=context)
        )

    def save(self):
        pass
        # redis_graph = Graph(self.base_class, redis_db)
        # redis_graph.add_node(self.graph_node)
        # for e in self.graph_edges:
        #     redis_graph.add_edge(e)
        # redis_graph.commit()


class GraphNodeModel(AbstractGraphNodeModel):
    """
    stores things in redis database given a key and value
    by default uses the instance class name as the key
    recommend to uniquely identify the instance with a key prefix or suffix
    prefixes are for more specific categories of objects (eg. mammal:human:woman:Lisa )
    suffixes are for specific attributes (eg. Lisa:eye_color, Lisa:age, etc)
    """

    def __init__(self, name):
        self.parent_class = None
        super(GenericNode, self).__init__(self, name)
