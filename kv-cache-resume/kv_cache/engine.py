"""llama.cpp `/slots` 客户端（T-06）—— **本包唯一碰 HTTP 的地方**。

R6：上游 API 未承诺稳定，所以将来 llama.cpp 改接口**只改这一个文件**。
别在别处直接发 HTTP。

契约来源是 [T-02 实测记录](../docs/phase0-slot-api-findings.md)，不是 SPEC 的转述。
下面每条实现都标了对应结论编号（C1~C9），改动前先回去读那一节。

**与 findings 的已知偏离（1 处，有意为之）**：C3 的影响列写的是「Adapter 侧做文件名净化」，
这里改成**直接拒绝非法文件名**（`is_valid_slot_filename` → `ValueError`）。理由见该函数 docstring，
已在 `docs/phase0-slot-api-findings.md` §0 同步标注。

本层是**纯 IO，不做任何决策** —— 要不要降级、要不要继续，是 T-09 的事。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable

import httpx

from .errors import SlotApiError, SlotApiUnavailable

#: 能力探测用的非法 action（C5 的判别式，见 capabilities()）
PROBE_ACTION = "__kv_cache_capability_probe__"

#: 服务端缺失该 flag 时返回 501 的根因（C5：消息里本来就写着 flag 名）
SLOT_SAVE_PATH_FLAG = "--slot-save-path"

#: 响应体截断长度 —— 保留现场但别把日志撑爆
BODY_LIMIT = 800


def _truncate(text: str, limit: int = BODY_LIMIT) -> str:
    return text if len(text) <= limit else text[:limit] + f"…（截断，原长 {len(text)}）"


def _read_body(response: httpx.Response) -> str:
    try:
        return response.text
    except Exception:  # noqa: BLE001 —— 读取响应体失败不能盖过原始错误
        return "<响应体不可读>"


def is_valid_slot_filename(filename: str) -> bool:
    """`filename` 是否满足服务端的约束（C3：只接受**纯文件名**）。

    服务端实测行为：带子目录、`/`、`\\`、`..` 一律 400 `Invalid filename`。

    本函数**故意不做「净化」**（不去掉路径分量、不替换字符）：净化会把两个不同的键
    悄悄映射到同一个文件，那正是 REQ-W2 说的「用错的 KV 产出错的结果」。
    宁可让调用方拿到一个明确的失败。
    """
    if not filename or filename in {".", ".."}:
        return False
    if "/" in filename or "\\" in filename:
        return False
    return not any(part in {"", ".", ".."} for part in filename.split("."))


@dataclass(frozen=True)
class SlotCapability:
    """服务端是否具备 slot 落盘能力。

    探测**无副作用**（不写文件）：发一个非法 action，
    没有 `--slot-save-path` 时整个 `/slots` action 路由被禁用 → 501（C5）；
    配了该 flag 时只有 action 校验失败 → 400（findings §1 端点清单，T-02 已实测）。
    """

    available: bool
    detail: str
    checked_at: float
    ttl_s: float
    probe_status: int | None = None
    probe_body: str | None = None

    def is_fresh(self, now: float) -> bool:
        """结论是否还在有效窗口内。`ttl_s <= 0` 视为永不过期。"""
        if self.ttl_s <= 0:
            return True
        return (now - self.checked_at) < self.ttl_s

    @property
    def expired(self) -> bool:
        return not self.is_fresh(time.monotonic())


@dataclass(frozen=True)
class SlotActionResult:
    """一次 save / restore 的结果。

    C9：restore 成功**无法从 `/slots` 观测**，所以这里把服务端原始字段都带出来，
    让 T-09 能用 `n_read` 与 meta 里的 `bytes` 交叉核对（C9 建议的第 3 条路径）。
    """

    action: str
    filename: str
    slot_id: int
    http_status: int
    body: dict[str, Any]
    elapsed_ms: float

    def _timing(self, key: str) -> float | None:
        timings = self.body.get("timings")
        if isinstance(timings, dict):
            value = timings.get(key)
            if isinstance(value, (int, float)):
                return float(value)
        return None

    @property
    def n_tokens(self) -> int | None:
        """save 是 `n_saved`，restore 是 `n_restored`。"""
        for key in ("n_saved", "n_restored"):
            value = self.body.get(key)
            if isinstance(value, int):
                return value
        return None

    @property
    def n_bytes(self) -> int | None:
        """save 是 `n_written`，restore 是 `n_read`。"""
        for key in ("n_written", "n_read"):
            value = self.body.get(key)
            if isinstance(value, int):
                return value
        return None

    @property
    def server_ms(self) -> float | None:
        """服务端自报耗时（`save_ms` / `restore_ms`）。"""
        return self._timing(f"{self.action}_ms")

    @property
    def total_ms(self) -> float:
        """含网络的端到端耗时。"""
        return self.elapsed_ms


class LlamaSlotClient:
    """`llama-server` 的 `/slots` 客户端。

    参数：
        base_url: 形如 `http://127.0.0.1:8080`。
        timeout: 单次请求超时（秒）。
        capability_ttl_s: 能力探测结论的缓存窗口；`<= 0` 表示永不过期。
        client: 注入 `httpx.Client`（单测用 `MockTransport`）。**注意**：
            注入的 client 也必须是 `trust_env=False` 的，否则会踩 C6 那个坑。
        clock: 注入时钟（单测控制 TTL 用）。
    """

    def __init__(
        self,
        base_url: str,
        timeout: float = 10.0,
        *,
        capability_ttl_s: float = 300.0,
        client: httpx.Client | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.capability_ttl_s = capability_ttl_s
        self._clock = clock
        self._capability: SlotCapability | None = None
        self._slot_id: int | None = None
        self._closed = False
        if client is not None:
            self._client = client
        else:
            # C6：**必须 trust_env=False**。否则沙箱/公司代理会接管 127.0.0.1 的请求，
            # 在 keep-alive 连接上表现为 200/404 严格交替 —— 症状是「缓存命中率永远只有一半」，
            # 而且排查时根本不会怀疑到代理头上。实测证据见 findings §5。
            self._client = httpx.Client(base_url=self.base_url, timeout=timeout, trust_env=False)

    # ---------------------------------------------------------------- 生命周期

    @classmethod
    def from_config(cls, config: Any) -> LlamaSlotClient:
        """从 `KVCacheConfig` 构造（字段名见 `config.py`）。"""
        return cls(
            config.server_base_url,
            timeout=config.restore_timeout_s,
            capability_ttl_s=config.capability_ttl_s,
        )

    def close(self) -> None:
        if not self._closed:
            self._client.close()
            self._closed = True

    def __enter__(self) -> LlamaSlotClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def reset(self) -> None:
        """清空缓存的 slot id 与能力结论（服务端重启/改配后调用）。"""
        self._slot_id = None
        self._capability = None

    # ---------------------------------------------------------------- 内部请求

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        url = f"{self.base_url}{path}"
        try:
            response = self._client.request(method, path, **kwargs)
        except httpx.TimeoutException as exc:
            raise SlotApiError(
                f"请求超时（{self.timeout}s）：{method} {url}。"
                "服务端可能只是慢 —— 这不代表它坏了（处置见 REQ-W1：回退冷 prefill）。",
                status_code=None,
                timeout=True,
                url=url,
            ) from exc
        except httpx.HTTPError as exc:
            raise SlotApiError(
                f"请求失败：{method} {url} —— {type(exc).__name__}: {exc}",
                status_code=None,
                url=url,
            ) from exc
        return response

    def _raise_for_status(self, response: httpx.Response, url: str, what: str) -> None:
        status = response.status_code
        if status == 501:
            # C5：501 是**路由级**缺失 —— 整个 /slots action 路由被禁用，
            # 不是某个 action 校验失败。服务端消息自己就写了 flag 名，直接转述。
            body = _truncate(_read_body(response))
            raise SlotApiUnavailable(
                f"服务端不具备 slot 落盘能力（{what} 返回 HTTP 501）：{body}。"
                f"根因是 llama-server 启动时没有配 `{SLOT_SAVE_PATH_FLAG}`；"
                f"补上该参数（或接受无缓存模式）即可。"
                f" —— 请求：{url}"
            )
        if not 200 <= status < 300:
            # 不丢现场：原始响应体照带。T-02 §6.3 的 400 消息把「空间不足」与
            # 「文件无效」混在一句里，据此分不清根因 —— 所以上层不要解析 message 猜原因，
            # 真要定位靠自己的 store 白盒检查。
            raise SlotApiError(
                f"{what} 返回 HTTP {status}：{_truncate(_read_body(response))} —— 请求：{url}",
                status_code=status,
                body=_truncate(_read_body(response)),
                url=url,
            )

    @staticmethod
    def _parse_json(response: httpx.Response, url: str, what: str) -> dict[str, Any]:
        try:
            data = response.json()
        except ValueError as exc:
            body = _truncate(_read_body(response))
            raise SlotApiError(
                f"{what} 返回的不是合法 JSON（HTTP {response.status_code}）：{body!r} —— 请求：{url}",
                status_code=response.status_code,
                body=body,
                url=url,
            ) from exc
        if not isinstance(data, dict):
            raise SlotApiError(
                f"{what} 返回的 JSON 不是对象而是 {type(data).__name__}：{_truncate(str(data))} —— 请求：{url}",
                status_code=response.status_code,
                body=_truncate(str(data)),
                url=url,
            )
        return data

    # ---------------------------------------------------------------- 探活与能力

    def health(self) -> bool:
        """探活。**不抛异常** —— 探活失败就是「不健康」，这是它的语义。"""
        try:
            return self._client.get("/health").status_code == 200
        except httpx.HTTPError:
            return False

    def capabilities(self, *, force: bool = False) -> SlotCapability:
        """探测服务端是否支持 slot 落盘，结论按 `capability_ttl_s` 缓存。

        **不要**每次调用都重探：既省 RTT，也是为了在服务端明确不支持时不要反复戳它
        （R6：上游 API 未承诺稳定，反复探没意义）。

        探测方式是发一个非法 action（**不写任何文件**）：
        - 501 → 无能力（C5：`--slot-save-path` 未配时整个路由被禁用）
        - 400 `Invalid action` → 有能力（findings §1；T-02 实测，T-06 用真实 8085 实例复核过）

        `force=True` 强制重探（`reset()` 也会清掉缓存的结论）。
        """
        now = self._clock()
        if not force and self._capability is not None and self._capability.is_fresh(now):
            return self._capability

        url = f"{self.base_url}/slots/0?action={PROBE_ACTION}"
        try:
            response = self._request(
                "POST", "/slots/0", params={"action": PROBE_ACTION}, json={"filename": "probe.bin"}
            )
        except SlotApiError as exc:
            # 网络层失败**不缓存**：探测没结论，不能把「连不上」记成「不支持」。
            raise SlotApiError(
                f"能力探测失败（无法确定服务端是否支持 slot 落盘）：{exc}",
                status_code=exc.status_code,
                body=exc.body,
                timeout=exc.timeout,
                url=url,
            ) from exc

        status = response.status_code
        body = _truncate(_read_body(response))
        if status == 501:
            capability = SlotCapability(
                available=False,
                detail=(
                    f"服务端未配置 `{SLOT_SAVE_PATH_FLAG}` —— /slots action 路由被禁用（HTTP 501）。"
                    "缓存层应进入无缓存模式（REQ-E3），后续不再发起 restore。"
                ),
                checked_at=now,
                ttl_s=self.capability_ttl_s,
                probe_status=status,
                probe_body=body,
            )
        elif status == 400:
            capability = SlotCapability(
                available=True,
                detail=f"服务端支持 slot 落盘（非法 action 返回 400 Invalid action，说明路由是活的）。响应：{body}",
                checked_at=now,
                ttl_s=self.capability_ttl_s,
                probe_status=status,
                probe_body=body,
            )
        else:
            # 既不是 501 也不是 400：结论不明，不缓存、不猜。
            raise SlotApiError(
                f"能力探测得到意外状态码 HTTP {status}：{body} —— 无法判定是否支持 slot 落盘。请求：{url}",
                status_code=status,
                body=body,
                url=url,
            )
        self._capability = capability
        return capability

    @property
    def cached_capability(self) -> SlotCapability | None:
        """已缓存的能力结论（不触发探测）。"""
        return self._capability

    # ---------------------------------------------------------------- slots

    def list_slots(self) -> list[dict[str, Any]]:
        """`GET /slots`。

        C1：`-np 1` 时只返回 1 个槽位（`id=0`），且 **id 不随请求变化**。
        C9：`n_past` / `n_prompt_tokens` **不会**因为 restore 而出现 ——
        别指望用它来验证 restore 是否生效。
        """
        url = f"{self.base_url}/slots"
        response = self._request("GET", "/slots")
        self._raise_for_status(response, url, "GET /slots")
        data = response.json()
        if not isinstance(data, list):
            raise SlotApiError(
                f"GET /slots 返回的不是数组而是 {type(data).__name__}：{_truncate(str(data))} —— 请求：{url}",
                status_code=response.status_code,
                body=_truncate(str(data)),
                url=url,
            )
        return data

    def resolve_slot_id(self) -> int:
        """从 `/slots` 取目标槽位号，避免把调用方的任意 id 直接转发。

        C8：越界 slot id 服务端**不报错**，200 且把请求的 id 原样回显 ——
        所以「传错了会被拒」这个假设不成立，slot id 必须我们自己取。
        """
        if self._slot_id is not None:
            return self._slot_id
        slots = self.list_slots()
        ids = [item.get("id") for item in slots if isinstance(item.get("id"), int)]
        if not ids:
            raise SlotApiError(
                f"GET /slots 没有返回任何可用的槽位 id：{_truncate(str(slots))}（服务端是否以 -np 0 启动？）",
                url=f"{self.base_url}/slots",
            )
        self._slot_id = min(ids)
        return self._slot_id

    # ---------------------------------------------------------------- save / restore

    def _action(self, action: str, filename: str, *, slot_id: int | None = None) -> SlotActionResult:
        """save / restore 的共同实现。

        C1：单步调用即可，**不需要**「先占位再 restore」的两步流程（T-02 §3 实测）。
        C7：`filename` **必须始终带** —— 服务端不校验 body，缺字段直接抛 500，
        会被误判成服务端故障。
        C3：`filename` 只接受纯文件名，带子目录一律 400 —— 由调用方保证，见 `is_valid_slot_filename`。
        """
        if not is_valid_slot_filename(filename):
            raise ValueError(
                f"非法 slot 文件名 {filename!r}：只接受纯文件名（不含路径分隔符、不为 '.'/'..'）。"
                "服务端会对子目录/路径穿越返回 400 Invalid filename（C3）。"
                "这里直接拒绝而不是净化 —— 净化会把不同的键悄悄映射到同一个文件（REQ-W2）。"
            )
        resolved = self.resolve_slot_id() if slot_id is None else slot_id
        path = f"/slots/{resolved}"
        url = f"{self.base_url}{path}?action={action}"
        started = time.perf_counter()
        response = self._request("POST", path, params={"action": action}, json={"filename": filename})
        elapsed_ms = (time.perf_counter() - started) * 1000
        self._raise_for_status(response, url, f"{action}({filename})")
        body = self._parse_json(response, url, f"{action}({filename})")
        return SlotActionResult(
            action=action,
            filename=filename,
            slot_id=resolved,
            http_status=response.status_code,
            body=body,
            elapsed_ms=round(elapsed_ms, 3),
        )

    def save(self, filename: str, *, slot_id: int | None = None) -> SlotActionResult:
        """把目标槽位的 KV 落盘。

        C4：同名文件**覆盖写**（实测 mtime 会变，是真重写不是跳过），不报错也不追加。
        故「路径要隔离」的责任在客户端 —— T-08 的 `slotcache_{key}.bin` 命名是必须的。
        """
        return self._action("save", filename, slot_id=slot_id)

    def restore(self, filename: str, *, slot_id: int | None = None) -> SlotActionResult:
        """把落盘的 KV 装回目标槽位。

        C2：目标 slot 空闲时**可以直接 restore**，不必先预热占用。

        ⚠️ 前置条件（T-03 实测，AC1 报告 §5）：**恢复时 `n_saved ≤ n_ctx`**。
        装不下会返回 400，且消息与「文件不存在」共用同一句（T-02 §6.3），无法据消息分辨。
        故 T-09 应先用 `meta.n_tokens <= server_n_ctx` 预筛，省掉一次注定失败的往返。
        注意 `-c` **不必与保存时一致**（这一点曾按「必须一致」猜过，是错的）。
        """
        return self._action("restore", filename, slot_id=slot_id)


__all__ = [
    "PROBE_ACTION",
    "SLOT_SAVE_PATH_FLAG",
    "LlamaSlotClient",
    "SlotActionResult",
    "SlotCapability",
    "is_valid_slot_filename",
]
