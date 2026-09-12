"""离线单测的公共夹具（T-05）。

两条纪律：
1. **全程离线** —— 不连真实 llama-server、不碰真实网络（E1 的定义就是「mock engine 可测」）。
2. **不依赖 cwd** —— 这里把 `kv-cache-resume/` 塞进 `sys.path`，这样从仓库根
   （`pytest kv-cache-resume/tests`）或子目录（`pytest`）跑都能 import `kv_cache`。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from kv_cache.config import KVCacheConfig  # noqa: E402


@pytest.fixture
def env() -> dict[str, str]:
    """空环境 —— 用于验证「全部走默认值」。

    刻意用**空 dict 而不是 `os.environ`**：真实进程环境里可能残留 `KV_*`，
    那会让「默认值」测试假失败，也会让别人的实验污染你的单测。
    """
    return {}


@pytest.fixture
def config(tmp_path: Path) -> KVCacheConfig:
    """指向临时目录的配置，测试里可以放心写文件。"""
    return KVCacheConfig(cache_dir=tmp_path / "kv")


@pytest.fixture
def make_config(tmp_path: Path):
    """按需覆写字段的配置工厂（避免每条用例都手搓十个参数）。"""

    def _make(**overrides) -> KVCacheConfig:
        params = {"cache_dir": tmp_path / "kv"}
        params.update(overrides)
        return KVCacheConfig(**params)

    return _make
