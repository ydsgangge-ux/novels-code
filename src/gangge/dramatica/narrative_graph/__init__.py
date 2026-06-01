"""
叙事知识图谱 — 借鉴 CodeGraph 的图谱架构，为小说写作构建知识图谱。

核心能力：
  - 节点：角色、地点、事件、伏笔、物品
  - 边：因果关系、角色关系、事件触发、伏笔埋收、位置变化
  - FTS5 全文搜索：内容检索、风格一致性检查
  - BFS/DFS 遍历：因果链追踪、角色关系网络、伏笔传播路径
  - 一致性检查：基于图谱检测矛盾（角色同时出现在两个地方等）

架构参考 CodeGraph：
  CodeGraph nodes → 叙事节点（Character/Location/Event/Hook/Item）
  CodeGraph edges → 叙事边（causes/participates/located_at/foreshadows/relationship）
  CodeGraph FTS5  → 叙事全文搜索
  CodeGraph BFS/DFS → 叙事图谱遍历
"""

from .schema import NarrativeGraphDB
from .indexer import NarrativeIndexer
from .queries import NarrativeQueries
from .traversal import NarrativeTraversal
from .consistency import ConsistencyChecker

__all__ = [
    "NarrativeGraphDB",
    "NarrativeIndexer",
    "NarrativeQueries",
    "NarrativeTraversal",
    "ConsistencyChecker",
]
