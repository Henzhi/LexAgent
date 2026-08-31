"""BM25 索引启动预热（P0 提速项）回归测试。

背景：BM25 索引为懒加载，构建耗时实测 ~38s（50867 chunks），此前会**同步阻塞
首次查询**，是首响头号瓶颈（见 docs/检索质量与响应性能评估-2026-08-31.md §4.3）。
改为启动期后台线程预热后，用这几条用例守住预热逻辑本身不被后续改动破坏。
"""

from __future__ import annotations

import pytest

from src.api import dependencies as deps


class FakeBm25:
    """最小替身：只实现 warmup_bm25 依赖的 is_ready / load_index。"""

    def __init__(self, ready: bool = False, fail: bool = False):
        self._ready = ready
        self._fail = fail
        self.load_calls = 0

    def is_ready(self) -> bool:
        return self._ready

    def load_index(self, force: bool = False) -> None:
        self.load_calls += 1
        if self._fail:
            raise RuntimeError("索引构建失败")
        self._ready = True


@pytest.fixture
def patch_state(monkeypatch):
    """把 warmup_bm25 依赖的两个模块级状态替换为可控值（自动还原）。"""

    def _apply(bm25, preload=True):
        monkeypatch.setattr(deps, "_bm25", bm25)
        monkeypatch.setattr(deps, "BM25_PRELOAD", preload)

    return _apply


class TestWarmupBm25:
    def test_builds_index_when_not_ready(self, patch_state):
        """未就绪时应触发构建，并返回真实耗时（供日志观测）。"""
        fake = FakeBm25(ready=False)
        patch_state(fake)
        cost = deps.warmup_bm25()
        assert fake.load_calls == 1
        assert cost > 0

    def test_skips_when_already_ready(self, patch_state):
        """已就绪绝不重复构建——一次约 38s，重复是纯浪费。"""
        fake = FakeBm25(ready=True)
        patch_state(fake)
        assert deps.warmup_bm25() == 0.0
        assert fake.load_calls == 0

    def test_skips_when_preload_disabled(self, patch_state):
        """BM25_PRELOAD=false 时应完全不碰索引（保留懒加载行为）。"""
        fake = FakeBm25(ready=False)
        patch_state(fake, preload=False)
        assert deps.warmup_bm25() == 0.0
        assert fake.load_calls == 0

    def test_noop_without_instance(self, patch_state):
        """HYBRID_ENABLED=false 时不创建 Bm25Retriever，必须安全空转。"""
        patch_state(None)
        assert deps.warmup_bm25() == 0.0

    def test_failure_degrades_to_lazy_load(self, patch_state):
        """预热失败不得拖垮启动，应降级为首次查询时懒加载。"""
        fake = FakeBm25(fail=True)
        patch_state(fake)
        assert deps.warmup_bm25() == 0.0
        assert fake.load_calls == 1


class FakeEmbedder:
    def __init__(self, fail: bool = False):
        self._fail = fail
        self.calls = 0

    def embed_query(self, text: str):
        self.calls += 1
        if self._fail:
            raise RuntimeError("Ollama 不可用")
        return [0.0] * 8


class TestWarmupEmbedder:
    """Embedding 预热：首次 embed_query 会触发 Ollama 加载 bge-m3（~8s）。"""

    def test_calls_embed_query(self, monkeypatch):
        fake = FakeEmbedder()
        monkeypatch.setattr(deps, "_embedder", fake)
        cost = deps.warmup_embedder()
        assert fake.calls == 1
        assert cost > 0

    def test_noop_without_instance(self, monkeypatch):
        monkeypatch.setattr(deps, "_embedder", None)
        assert deps.warmup_embedder() == 0.0

    def test_failure_does_not_raise(self, monkeypatch):
        """Ollama 不可用时预热失败，应降级为由首次查询触发加载，不拖垮启动。"""
        fake = FakeEmbedder(fail=True)
        monkeypatch.setattr(deps, "_embedder", fake)
        assert deps.warmup_embedder() == 0.0
        assert fake.calls == 1
