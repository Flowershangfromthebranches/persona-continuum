from __future__ import annotations

from typing import Any

from persona_continuum.application._utils import new_id
from persona_continuum.world.models import (
    CausalEdge,
    CausalGraph,
    CausalNode,
    CausalNodeType,
    CausalPathStep,
    CausalRelationType,
    TimelineEvent,
)


class CausalGraphEngine:
    """Maintains a queryable Directed Acyclic Graph (DAG) of counterfactual causality,

    enabling backward causal chain explanation for any historical outcome.
    """

    def __init__(self, graph: CausalGraph | None = None) -> None:
        self.graph = graph or CausalGraph()

    def add_node(self, node: CausalNode) -> CausalNode:
        self.graph.nodes[node.id] = node
        return node

    def add_edge(self, edge: CausalEdge) -> CausalEdge:
        self.graph.edges.append(edge)
        return edge

    def record_node(
        self,
        node_type: CausalNodeType,
        name: str,
        timestamp: str,
        properties: dict[str, Any] | None = None,
        node_id: str | None = None,
    ) -> CausalNode:
        nid = node_id or new_id("cnode")
        node = CausalNode(
            id=nid,
            node_type=node_type,
            name=name,
            timestamp=timestamp,
            properties=properties or {},
        )
        return self.add_node(node)

    def link(
        self,
        source_id: str,
        target_id: str,
        relation_type: CausalRelationType = CausalRelationType.CAUSES,
        weight: float = 1.0,
        properties: dict[str, Any] | None = None,
    ) -> CausalEdge:
        edge = CausalEdge(
            id=new_id("cedge"),
            source_id=source_id,
            target_id=target_id,
            relation_type=relation_type,
            weight=weight,
            properties=properties or {},
        )
        return self.add_edge(edge)

    def record_event(
        self,
        event: TimelineEvent,
        antecedent_node_ids: list[str] | None = None,
    ) -> CausalNode:
        """Indexes a timeline event into the causal graph and links its causal antecedents."""
        node = self.record_node(
            node_type=CausalNodeType.EVENT,
            name=event.cause or event.effect,
            timestamp=event.event_time,
            properties={
                "event_id": event.id,
                "effect": event.effect,
                "actors": event.actors,
                "confidence": event.confidence,
                "importance": event.importance,
            },
            node_id=f"node_evt_{event.id}",
        )

        if antecedent_node_ids:
            for ant_id in antecedent_node_ids:
                if ant_id in self.graph.nodes:
                    self.link(ant_id, node.id, CausalRelationType.CAUSES)

        return node

    def query_causal_chain(
        self,
        target_query: str,
        max_depth: int = 15,
    ) -> list[CausalPathStep]:
        """Performs backward graph traversal starting from target node matching the query,

        returning the sequential antecedent steps explaining why this outcome occurred.
        """
        # Find target node by ID or name match
        target_node: CausalNode | None = None
        if target_query in self.graph.nodes:
            target_node = self.graph.nodes[target_query]
        else:
            q_lower = target_query.lower()
            for n in self.graph.nodes.values():
                if q_lower in n.name.lower() or q_lower in str(n.properties).lower():
                    target_node = n
                    break

        if not target_node:
            return []

        path: list[CausalPathStep] = []
        visited: set[str] = set()
        queue: list[tuple[CausalNode, str | None]] = [(target_node, None)]

        while queue and len(path) < max_depth:
            current, rel = queue.pop(0)
            if current.id in visited:
                continue
            visited.add(current.id)
            path.append(CausalPathStep(node=current, relation_to_next=rel))

            # Find all incoming edges (sources that caused/influenced this current node)
            incoming = [
                e
                for e in self.graph.edges
                if e.target_id == current.id and e.source_id in self.graph.nodes
            ]
            # Prioritize CAUSES and DEPENDS_ON over INFLUENCES
            incoming.sort(
                key=lambda e: (0 if e.relation_type == CausalRelationType.CAUSES else 1, -e.weight)
            )

            for edge in incoming:
                src_node = self.graph.nodes[edge.source_id]
                if src_node.id not in visited:
                    queue.append((src_node, str(edge.relation_type)))

        return path

    def export_dict(self) -> dict[str, Any]:
        return {
            "nodes": {nid: n.model_dump() for nid, n in self.graph.nodes.items()},
            "edges": [e.model_dump() for e in self.graph.edges],
        }

    def import_dict(self, data: dict[str, Any]) -> None:
        nodes: dict[str, CausalNode] = {}
        for nid, ndata in data.get("nodes", {}).items():
            nodes[nid] = CausalNode.model_validate(ndata)
        edges: list[CausalEdge] = [
            CausalEdge.model_validate(edata) for edata in data.get("edges", [])
        ]
        self.graph = CausalGraph(nodes=nodes, edges=edges)
