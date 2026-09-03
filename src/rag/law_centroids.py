"""
法律质心向量表（B2 二阶段：法名推断的在线底座）。

每部法律的质心 = 其全部 article chunk 向量（pgvector 已有，与查询同空间）
的均值 + L2 归一化。B2 spike 实测质心法 Recall@3 70.3% 达标
（docs/B2-法名推断spike报告-2026-08-30.md），比描述文本法高 18.9 点。

设计：
- **惰性加载 + 进程内缓存**（单例）：首次使用时从 PG 聚合一次（约几秒），
  之后所有请求共享；加载失败不抛出——is_ready=False，上层加权自动跳过
  （横切组件故障不阻断主链路，与预算/确认存储同款原则）；
- 测试可注入 rows=[(law_name, vector), ...]，不触达 PG；
- 转正形态：物化为 PG 小表随入库增量维护，本类的加载逻辑届时替换，
  接口不变。

norm_law_name：法名归一化（去版本后缀 + 去"中华人民共和国"前缀）——
原标注/检索返回/质心表三方的法名形式不一致，比较前必须归一化。
"""

from __future__ import annotations

import logging
import re
import threading
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)


def norm_law_name(name: str) -> str:
    s = re.sub(r"[（(][^）)]*[）)]", "", name or "")
    return re.sub(r"^中华人民共和国", "", s).strip()


class LawCentroids:
    """法律质心矩阵（惰性从 PG 聚合，或测试注入）。

    Args:
        rows: 测试注入的 (law_name, vector) 列表；None = 运行时从 PG 惰性加载
    """

    def __init__(self, rows: list[tuple[str, Any]] | None = None):
        self._lock = threading.Lock()
        self._loaded = False
        self._names: list[str] = []  # 归一化法名（与 matrix 行对齐）
        self._shorts: list[str] = []  # 简称（查询门控：含法名的查询不激活加权）
        self._matrix: np.ndarray | None = None
        if rows is not None:
            self._build(rows)

    # ------------------------------------------------------------------
    # 加载
    # ------------------------------------------------------------------

    def ensure_loaded(self) -> None:
        if self._loaded:
            return
        with self._lock:
            if self._loaded:
                return
            try:
                self._build(self._load_rows())
            except Exception as e:
                # 加载失败保持未就绪：加权自动跳过，主链路不受影响
                logger.warning(f"法律质心加载失败（法名加权将跳过）: {e}")

    @staticmethod
    def _load_rows() -> list[tuple[str, Any]]:
        from src.db.pool import db_connection

        sums: dict[str, np.ndarray] = {}
        counts: dict[str, int] = {}
        with db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT metadata->>'law_name', embedding FROM document_chunks "
                    "WHERE chunk_type='article' AND metadata->>'law_name' IS NOT NULL"
                )
                while True:
                    rows = cur.fetchmany(5000)
                    if not rows:
                        break
                    for law_name, emb in rows:
                        v = np.asarray([float(x) for x in str(emb).strip("[]").split(",")], dtype=np.float32)
                        sums[law_name] = sums.get(law_name, np.zeros_like(v)) + v
                        counts[law_name] = counts.get(law_name, 0) + 1
        return [(name, sums[name] / max(counts[name], 1)) for name in sums]

    def _build(self, rows: list[tuple[str, Any]]) -> None:
        names, mat = [], []
        for law_name, vec in rows:
            v = np.asarray(vec, dtype=np.float32)
            n = np.linalg.norm(v)
            if n == 0:
                continue
            names.append(norm_law_name(law_name))
            mat.append(v / n)
        if not mat:
            logger.warning("法律质心为空（法名加权将跳过）")
            return
        self._names = names
        self._shorts = [norm_law_name(n) for n in names]
        self._matrix = np.stack(mat)
        self._loaded = True
        logger.info(f"法律质心就绪: {self._matrix.shape[0]} 部 dim={self._matrix.shape[1]}")

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    @property
    def is_ready(self) -> bool:
        return self._loaded and self._matrix is not None

    def top_laws(self, query_vec: Any, k: int = 3) -> list[str]:
        """归一化查询向量 → 相似度最高的 k 部法律（归一化法名列表）。"""
        if not self.is_ready:
            return []
        q = np.asarray(query_vec, dtype=np.float32)
        n = np.linalg.norm(q)
        if n == 0:
            return []
        sims = self._matrix @ (q / n)
        idx = np.argsort(-sims)[:k]
        return [self._names[i] for i in idx]

    def contains_law_name(self, query: str) -> bool:
        """查询是否已包含任一已知法名（含简称）——是则不激活加权（精确路径优先）。"""
        if not self.is_ready:
            return False
        q = query or ""
        return any(name and name in q for name in self._shorts)


# ---------------------------------------------------------------------------
# 全局单例（检索链与运维脚本共用）
# ---------------------------------------------------------------------------

_centroids: "LawCentroids | None" = None
_centroids_lock = threading.Lock()


def get_law_centroids() -> LawCentroids:
    global _centroids
    if _centroids is None:
        with _centroids_lock:
            if _centroids is None:
                _centroids = LawCentroids()  # 惰性 PG 加载
    return _centroids


def reset_law_centroids() -> None:
    global _centroids
    with _centroids_lock:
        _centroids = None
