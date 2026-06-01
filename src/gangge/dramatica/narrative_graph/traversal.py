"""
叙事知识图谱 — BFS/DFS 遍历

借鉴 CodeGraph 的 GraphTraverser，支持：
  - BFS 广度优先遍历：找最短关系路径
  - DFS 深度优先遍历：追踪完整因果链
  - 双向 BFS：高效查找两个节点间的最短路径
  - 子图提取：提取以某节点为中心的 N 跳子图
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from .schema import NarrativeGraphDB

logger = logging.getLogger(__name__)


@dataclass
class TraversalNode:
    node_id: str
    name: str
    kind: str
    depth: int


@dataclass
class TraversalEdge:
    source: str
    target: str
    kind: str
    chapter: int
    description: str


@dataclass
class TraversalResult:
    nodes: list[TraversalNode] = field(default_factory=list)
    edges: list[TraversalEdge] = field(default_factory=list)
    paths: list[list[str]] = field(default_factory=list)


class NarrativeTraversal:
    """叙事图谱遍历器"""

    def __init__(self, db: NarrativeGraphDB):
        self.db = db

    def traverse_bfs(
        self,
        start_id: str,
        max_depth: int = 3,
        edge_kinds: list[str] | None = None,
        direction: str = "outgoing",
        limit: int = 100,
    ) -> TraversalResult:
        """
        BFS 广度优先遍历。

        direction: "outgoing"（从原因到后果）或 "incoming"（从后果到原因）或 "both"
        """
        result = TraversalResult()
        visited = set()
        queue: list[tuple[str, int]] = [(start_id, 0)]

        start_node = self.db.get_node(start_id)
        if not start_node:
            return result

        result.nodes.append(TraversalNode(
            node_id=start_id,
            name=start_node["name"],
            kind=start_node["kind"],
            depth=0,
        ))

        while queue and len(result.nodes) < limit:
            current_id, depth = queue.pop(0)
            if current_id in visited:
                continue
            visited.add(current_id)

            if depth >= max_depth:
                continue

            edges_to_follow = []
            if direction in ("outgoing", "both"):
                edges_to_follow.extend(self.db.get_outgoing_edges(current_id, edge_kinds))
            if direction in ("incoming", "both"):
                edges_to_follow.extend(self.db.get_incoming_edges(current_id, edge_kinds))

            for edge in edges_to_follow:
                neighbor_id = edge["target"] if direction != "incoming" else edge["source"]
                if direction == "both" and neighbor_id == current_id:
                    neighbor_id = edge["target"] if edge["source"] == current_id else edge["source"]

                neighbor_node = self.db.get_node(neighbor_id)
                if not neighbor_node:
                    continue

                result.edges.append(TraversalEdge(
                    source=edge["source"],
                    target=edge["target"],
                    kind=edge["kind"],
                    chapter=edge["chapter"],
                    description=edge["description"],
                ))

                if neighbor_id not in visited:
                    result.nodes.append(TraversalNode(
                        node_id=neighbor_id,
                        name=neighbor_node["name"],
                        kind=neighbor_node["kind"],
                        depth=depth + 1,
                    ))
                    queue.append((neighbor_id, depth + 1))

        return result

    def traverse_dfs(
        self,
        start_id: str,
        max_depth: int = 5,
        edge_kinds: list[str] | None = None,
        direction: str = "outgoing",
    ) -> TraversalResult:
        """
        DFS 深度优先遍历。适合追踪完整因果链。
        """
        result = TraversalResult()
        visited = set()

        def _dfs(current_id: str, depth: int):
            if depth > max_depth or current_id in visited:
                return
            visited.add(current_id)

            node = self.db.get_node(current_id)
            if not node:
                return

            result.nodes.append(TraversalNode(
                node_id=current_id,
                name=node["name"],
                kind=node["kind"],
                depth=depth,
            ))

            edges = []
            if direction in ("outgoing", "both"):
                edges.extend(self.db.get_outgoing_edges(current_id, edge_kinds))
            if direction in ("incoming", "both"):
                edges.extend(self.db.get_incoming_edges(current_id, edge_kinds))

            for edge in edges:
                neighbor_id = edge["target"] if direction != "incoming" else edge["source"]
                if direction == "both" and neighbor_id == current_id:
                    neighbor_id = edge["target"] if edge["source"] == current_id else edge["source"]

                result.edges.append(TraversalEdge(
                    source=edge["source"],
                    target=edge["target"],
                    kind=edge["kind"],
                    chapter=edge["chapter"],
                    description=edge["description"],
                ))

                if neighbor_id not in visited:
                    _dfs(neighbor_id, depth + 1)

        _dfs(start_id, 0)
        return result

    def find_shortest_path(
        self,
        from_id: str,
        to_id: str,
        edge_kinds: list[str] | None = None,
        max_depth: int = 6,
    ) -> list[str]:
        """
        双向 BFS 查找两个节点间的最短路径。

        返回节点 ID 列表，空列表表示不可达。
        """
        if from_id == to_id:
            return [from_id]

        forward_visited = {from_id: None}
        backward_visited = {to_id: None}
        forward_queue = [from_id]
        backward_queue = [to_id]

        for depth in range(max_depth):
            new_forward = []
            for node_id in forward_queue:
                for edge in self.db.get_outgoing_edges(node_id, edge_kinds):
                    target = edge["target"]
                    if target not in forward_visited:
                        forward_visited[target] = node_id
                        new_forward.append(target)
                        if target in backward_visited:
                            return self._reconstruct_path(forward_visited, backward_visited, target)
            forward_queue = new_forward

            new_backward = []
            for node_id in backward_queue:
                for edge in self.db.get_incoming_edges(node_id, edge_kinds):
                    source = edge["source"]
                    if source not in backward_visited:
                        backward_visited[source] = node_id
                        new_backward.append(source)
                        if source in forward_visited:
                            return self._reconstruct_path(forward_visited, backward_visited, source)
            backward_queue = new_backward

        return []

    def _reconstruct_path(
        self,
        forward: dict[str, str | None],
        backward: dict[str, str | None],
        meeting_point: str,
    ) -> list[str]:
        path = []
        current = meeting_point
        while current is not None:
            path.append(current)
            current = forward.get(current)
        path.reverse()

        current = backward.get(meeting_point)
        while current is not None:
            path.append(current)
            current = backward.get(current)

        return path

    def extract_subgraph(
        self,
        center_id: str,
        hops: int = 2,
        edge_kinds: list[str] | None = None,
    ) -> dict[str, Any]:
        """
        提取以某节点为中心的 N 跳子图。

        返回格式适合 GUI 可视化。
        """
        traversal = self.traverse_bfs(center_id, max_depth=hops, edge_kinds=edge_kinds, direction="both")

        nodes = []
        for tn in traversal.nodes:
            node = self.db.get_node(tn.node_id)
            nodes.append({
                "id": tn.node_id,
                "name": tn.name,
                "kind": tn.kind,
                "depth": tn.depth,
                "description": node["description"] if node else "",
            })

        edges = []
        for te in traversal.edges:
            edges.append({
                "source": te.source,
                "target": te.target,
                "kind": te.kind,
                "chapter": te.chapter,
                "description": te.description,
            })

        return {
            "center": center_id,
            "hops": hops,
            "nodes": nodes,
            "edges": edges,
        }

    def find_relationship_path(
        self,
        char_a: str,
        char_b: str,
        max_depth: int = 4,
    ) -> list[dict]:
        """
        查找两个角色之间的关系路径。

        返回路径上的每一步，包含中间角色和关系类型。
        """
        path_ids = self.find_shortest_path(
            f"char_{char_a}",
            f"char_{char_b}",
            edge_kinds=["relationship", "participates", "located_at"],
            max_depth=max_depth,
        )

        if not path_ids:
            return []

        result = []
        for i, node_id in enumerate(path_ids):
            node = self.db.get_node(node_id)
            step = {
                "id": node_id,
                "name": node["name"] if node else node_id,
                "kind": node["kind"] if node else "unknown",
            }
            if i > 0:
                prev_id = path_ids[i - 1]
                edges = self.db.get_edges_between(prev_id, node_id)
                if not edges:
                    edges = self.db.get_edges_between(node_id, prev_id)
                if edges:
                    edge = edges[0]
                    step["edge_kind"] = edge["kind"]
                    step["edge_description"] = edge["description"]
            result.append(step)

        return result
