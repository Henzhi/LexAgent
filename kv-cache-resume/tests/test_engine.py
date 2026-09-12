"""`kv_cache.engine` 的全离线单测（T-06）。

**不产生任何真实网络请求**：所有用例都走 `httpx.MockTransport`。
（唯一例外是标了 `@pytest.mark.integration` 的那条，默认不跑。）

契约来自 T-02 实测（`docs/phase0-slot-api-findings.md`），响应体一律照抄实测原文，
这样将来 llama.cpp 改行为时，是这些测试先红，而不是线上先坏。
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from kv_cache.engine import (
    PROBE_ACTION,
    SLOT_SAVE_PATH_FLAG,
    LlamaSlotClient,
    is_valid_slot_filename,
)

BASE_URL = "http://127.0.0.1:9999"

# ---- 实测原文（T-02，照抄，别改写） ----------------------------------------------------

HEALTH_OK = '{"status":"ok"}'
# findings §2：-np 1 时只有 slot 0
SLOTS_ONE = '[{"id":0,"n_ctx":4096,"speculative":false,"is_processing":false}]'
# findings §1.1：save 成功
SAVE_OK = '{"id_slot":0,"filename":"slotcache_abc.bin","n_saved":268,"n_written":9884748,"timings":{"save_ms":37.6}}'
# findings §1.1：restore 成功（C9：只有这些字段，/slots 观测不到）
RESTORE_OK = (
    '{"id_slot":0,"filename":"slotcache_abc.bin","n_restored":268,"n_read":9884748,"timings":{"restore_ms":18.1}}'
)
# findings §1.1：C5 —— 服务端消息自带 flag 名
UNAVAILABLE_501 = (
    '{"error":{"code":501,"message":"This server does not support slots action. '
    'Start it with `--slot-save-path`","type":"not_supported_error"}}'
)
# findings §6.3：400 把「空间不足」与「文件无效」混在一句里
RESTORE_400 = (
    '{"error":{"code":400,"message":"Unable to restore slot: No available space in KV cache '
    'or invalid slot save file","type":"invalid_request_error"}}'
)
# findings §6.1：缺 filename → 500
MISSING_FILENAME_500 = '{"error":{"code":500,"message":"[json.exception.out_of_range.403] key \'filename\' not found","type":"server_error"}}'
NOT_FOUND_404 = '{"error":{"message":"File Not Found","type":"not_found_error","code":404}}'
INVALID_ACTION_400 = '{"error":{"code":400,"message":"Invalid action","type":"invalid_request_error"}}'


# ---- 夹具 -----------------------------------------------------------------------------


class Recorder:
    """记录每次请求，用来断言「不会每次调用都重探」。"""

    def __init__(self, responses: list[tuple[int, str]] | None = None, default: tuple[int, str] = (200, HEALTH_OK)):
        self.requests: list[httpx.Request] = []
        self._responses = list(responses or [])
        self._default = default

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        status, body = self._responses.pop(0) if self._responses else self._default
        return httpx.Response(status, text=body, request=request)

    @property
    def count(self) -> int:
        return len(self.requests)

    def paths(self) -> list[str]:
        return [f"{r.method} {r.url.path}" for r in self.requests]


def make_client(
    responses: list[tuple[int, str]] | None = None,
    *,
    default: tuple[int, str] = (200, HEALTH_OK),
    clock=None,
    capability_ttl_s: float = 300.0,
) -> tuple[LlamaSlotClient, Recorder]:
    recorder = Recorder(responses, default)
    transport = httpx.MockTransport(recorder)
    http = httpx.Client(base_url=BASE_URL, transport=transport, trust_env=False)
    kwargs: dict[str, Any] = {"capability_ttl_s": capability_ttl_s}
    if clock is not None:
        kwargs["clock"] = clock
    return LlamaSlotClient(BASE_URL, 10.0, client=http, **kwargs), recorder


def route_handler(routes: dict[tuple[str, str], tuple[int, str]], recorder: Recorder):
    """按 (method, path) 分派的 handler —— 路径要对得上才回，避免「碰巧通过」。"""

    def handler(request: httpx.Request) -> httpx.Response:
        recorder.requests.append(request)
        key = (request.method, request.url.path)
        if key not in routes:
            return httpx.Response(404, text=NOT_FOUND_404, request=request)
        status, body = routes[key]
        return httpx.Response(status, text=body, request=request)

    return handler


def client_with_routes(routes: dict[tuple[str, str], tuple[int, str]], clock=None):
    recorder = Recorder()
    transport = httpx.MockTransport(route_handler(routes, recorder))
    http = httpx.Client(base_url=BASE_URL, transport=transport, trust_env=False)
    kwargs: dict[str, Any] = {}
    if clock is not None:
        kwargs["clock"] = clock
    return LlamaSlotClient(BASE_URL, 10.0, client=http, **kwargs), recorder


# ---- C6：代理陷阱 ----------------------------------------------------------------------


class TestTransportHardening:
    def test_default_client_disables_trust_env(self):
        """C6：自建的 client 必须 `trust_env=False`。

        不这么做，代理会接管 127.0.0.1 的请求，表现为 200/404 严格交替 ——
        症状是「缓存命中率永远只有一半」，而且排查时根本不会怀疑到代理头上。
        """
        client = LlamaSlotClient(BASE_URL)
        try:
            assert client._client.trust_env is False
        finally:
            client.close()

    def test_client_is_context_manager(self):
        with LlamaSlotClient(BASE_URL) as client:
            assert client is not None


# ---- 探活 -----------------------------------------------------------------------------


class TestHealth:
    def test_health_ok(self):
        client, _ = make_client([(200, HEALTH_OK)])
        assert client.health() is True

    def test_health_not_ok_is_false_not_exception(self):
        """探活失败就是「不健康」—— 这是它的语义，不该抛异常。"""
        client, _ = make_client([(503, "nope")])
        assert client.health() is False

    def test_health_connection_error_is_false(self):
        def boom(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=request)

        http = httpx.Client(base_url=BASE_URL, transport=httpx.MockTransport(boom), trust_env=False)
        client = LlamaSlotClient(BASE_URL, client=http)
        assert client.health() is False


# ---- 能力探测（T-02 判别式的补测） ------------------------------------------------------


class TestCapabilities:
    def test_501_means_unavailable_and_message_names_the_flag(self):
        """验收：501 抛/记为不可用时，消息必须含 `--slot-save-path`（REQ-E3 人可读）。"""
        client, recorder = make_client([(501, UNAVAILABLE_501)])
        capability = client.capabilities()
        assert capability.available is False
        assert SLOT_SAVE_PATH_FLAG in capability.detail
        assert capability.probe_status == 501
        assert recorder.count == 1
        assert PROBE_ACTION in str(recorder.requests[0].url)

    def test_400_means_available(self):
        """配了 flag 时非法 action 返回 400（T-06 实测补测，findings §1）。"""
        client, _ = make_client([(400, INVALID_ACTION_400)])
        capability = client.capabilities()
        assert capability.available is True
        assert capability.probe_status == 400

    def test_probe_is_side_effect_free(self):
        """判别式必须是只读的 —— 不能靠真写一个文件来试探。"""
        client, recorder = make_client([(400, INVALID_ACTION_400)])
        client.capabilities()
        body = json.loads(recorder.requests[0].content)
        assert body == {"filename": "probe.bin"}
        assert "action" in str(recorder.requests[0].url)
        assert PROBE_ACTION in str(recorder.requests[0].url)

    def test_result_is_cached_second_call_does_not_reprobe(self):
        """验收：结论被缓存，**不会每次调用都重探**（断言请求次数）。"""
        client, recorder = make_client([(501, UNAVAILABLE_501)], default=(400, INVALID_ACTION_400))
        first = client.capabilities()
        second = client.capabilities()
        third = client.capabilities()
        assert first == second == third
        assert recorder.count == 1, "第二次起应该直接吃缓存，不该再发请求"

    def test_force_reprobes(self):
        client, recorder = make_client([(501, UNAVAILABLE_501), (400, INVALID_ACTION_400)])
        assert client.capabilities().available is False
        assert client.capabilities(force=True).available is True
        assert recorder.count == 2

    def test_expired_ttl_reprobes(self):
        """TTL 到期后重探 —— 用注入时钟，不睡真实时间。"""
        now = [1000.0]
        client, recorder = make_client(
            [(400, INVALID_ACTION_400), (501, UNAVAILABLE_501)],
            clock=lambda: now[0],
            capability_ttl_s=60.0,
        )
        assert client.capabilities().available is True
        now[0] += 30
        client.capabilities()
        assert recorder.count == 1, "窗口内不该重探"
        now[0] += 31  # 累计 61s > 60s
        assert client.capabilities().available is False
        assert recorder.count == 2

    def test_zero_ttl_never_expires(self):
        now = [0.0]
        client, recorder = make_client([(501, UNAVAILABLE_501)], clock=lambda: now[0], capability_ttl_s=0.0)
        client.capabilities()
        now[0] += 10_000
        client.capabilities()
        assert recorder.count == 1

    def test_reset_clears_cached_capability(self):
        client, recorder = make_client([(501, UNAVAILABLE_501), (400, INVALID_ACTION_400)])
        client.capabilities()
        client.reset()
        assert client.cached_capability is None
        assert client.capabilities().available is True
        assert recorder.count == 2

    def test_unexpected_status_raises_and_is_not_cached(self):
        """既不是 501 也不是 400 → 结论不明，抛错且不缓存（不能把「不知道」记成「不支持」）。"""
        client, recorder = make_client([(500, MISSING_FILENAME_500), (400, INVALID_ACTION_400)])
        assert client.cached_capability is None
        with pytest.raises(Exception) as excinfo:
            client.capabilities()
        assert "500" in str(excinfo.value)
        assert client.cached_capability is None, "结论不明时不许写缓存"
        # 下一次仍会重探
        assert client.capabilities().available is True
        assert recorder.count == 2

    def test_probe_network_error_not_cached(self):
        def boom(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused", request=request)

        http = httpx.Client(base_url=BASE_URL, transport=httpx.MockTransport(boom), trust_env=False)
        client = LlamaSlotClient(BASE_URL, client=http)
        with pytest.raises(Exception):
            client.capabilities()
        assert client.cached_capability is None

    def test_probe_does_not_use_get_slots(self):
        """判别式不依赖 /slots（那个在两种配置下都返回 200，区分不出来）。"""
        client, recorder = make_client([(501, UNAVAILABLE_501)])
        client.capabilities()
        assert "GET /slots" not in recorder.paths()


# ---- save / restore -------------------------------------------------------------------


def ok_routes():
    return {
        ("GET", "/slots"): (200, SLOTS_ONE),
        ("POST", "/slots/0"): (200, SAVE_OK),
    }


class TestSaveRestore:
    def test_save_ok_returns_parsed_fields(self):
        client, recorder = client_with_routes(ok_routes())
        result = client.save("slotcache_abc.bin")
        assert result.action == "save"
        assert result.slot_id == 0
        assert result.http_status == 200
        assert result.n_tokens == 268
        assert result.n_bytes == 9_884_748
        assert result.server_ms == pytest.approx(37.6)

    def test_restore_ok_returns_parsed_fields(self):
        routes = ok_routes()
        routes[("POST", "/slots/0")] = (200, RESTORE_OK)
        client, _ = client_with_routes(routes)
        result = client.restore("slotcache_abc.bin")
        assert result.action == "restore"
        assert result.n_tokens == 268
        assert result.n_bytes == 9_884_748
        assert result.server_ms == pytest.approx(18.1)

    def test_action_is_post_with_query_param(self):
        """C5 的端点形态：`POST /slots/{id}?action=save`，body 带 filename。"""
        client, recorder = client_with_routes(ok_routes())
        client.save("slotcache_abc.bin")
        post = next(r for r in recorder.requests if r.method == "POST")
        assert post.url.path == "/slots/0"
        assert post.url.params["action"] == "save"
        assert json.loads(post.content) == {"filename": "slotcache_abc.bin"}

    def test_slot_id_resolved_from_slots_and_cached(self):
        """C1：slot id 从 /slots 取；C8：越界 id 不被拒绝，所以不能由调用方随便传。"""
        client, recorder = client_with_routes(ok_routes())
        assert client.resolve_slot_id() == 0
        client.save("a.bin")
        client.restore("a.bin")
        assert recorder.paths().count("GET /slots") == 1, "slot id 应只解析一次"

    def test_slot_id_never_hardcoded_forwarded(self):
        """显式传 slot_id 时**跳过 `/slots` 解析**，直接用传入值（供测试/特殊场景）。

        注意这里**故意不写** `("GET", "/slots")`：一旦实现退化成「无论怎样都先查一次 /slots」，
        请求会落到 404 分支而让用例变红 —— 这正是断言「没走解析」的手段。
        """
        routes = {("POST", "/slots/3"): (200, SAVE_OK)}
        client, recorder = client_with_routes(routes)
        client.save("a.bin", slot_id=3)
        assert "GET /slots" not in recorder.paths()
        assert recorder.paths() == ["POST /slots/3"]


# ---- 错误映射 -------------------------------------------------------------------------


class TestErrorMapping:
    def test_501_raises_unavailable_with_flag_in_message(self):
        """验收：501 → SlotApiUnavailable，消息含 `--slot-save-path`（REQ-E3）。"""
        routes = {("GET", "/slots"): (200, SLOTS_ONE), ("POST", "/slots/0"): (501, UNAVAILABLE_501)}
        client, _ = client_with_routes(routes)
        from kv_cache.errors import SlotApiUnavailable

        with pytest.raises(SlotApiUnavailable) as excinfo:
            client.save("a.bin")
        message = str(excinfo.value)
        assert SLOT_SAVE_PATH_FLAG in message
        assert "501" in message

    def test_501_on_restore_too(self):
        routes = {("GET", "/slots"): (200, SLOTS_ONE), ("POST", "/slots/0"): (501, UNAVAILABLE_501)}
        client, _ = client_with_routes(routes)
        from kv_cache.errors import SlotApiUnavailable

        with pytest.raises(SlotApiUnavailable):
            client.restore("a.bin")

    def test_400_keeps_raw_body(self):
        """验收：其它非 2xx 抛 SlotApiError 且**带原始响应体**（不丢现场）。"""
        routes = {("GET", "/slots"): (200, SLOTS_ONE), ("POST", "/slots/0"): (400, RESTORE_400)}
        client, _ = client_with_routes(routes)
        from kv_cache.errors import SlotApiError

        with pytest.raises(SlotApiError) as excinfo:
            client.restore("a.bin")
        error = excinfo.value
        assert error.status_code == 400
        assert "No available space in KV cache" in error.body
        assert "No available space in KV cache" in str(error)
        assert error.timeout is False

    def test_500_keeps_raw_body(self):
        routes = {("GET", "/slots"): (200, SLOTS_ONE), ("POST", "/slots/0"): (500, MISSING_FILENAME_500)}
        client, _ = client_with_routes(routes)
        from kv_cache.errors import SlotApiError

        with pytest.raises(SlotApiError) as excinfo:
            client.save("a.bin")
        assert excinfo.value.status_code == 500
        assert "filename" in excinfo.value.body

    def test_404_maps_to_slot_api_error(self):
        routes = {("GET", "/slots"): (200, SLOTS_ONE), ("POST", "/slots/0"): (404, NOT_FOUND_404)}
        client, _ = client_with_routes(routes)
        from kv_cache.errors import SlotApiError

        with pytest.raises(SlotApiError) as excinfo:
            client.save("a.bin")
        assert excinfo.value.status_code == 404

    def test_malformed_json_raises_with_body(self):
        """200 但 body 不是 JSON —— 不能让它变成裸 ValueError 穿透出去。"""
        routes = {("GET", "/slots"): (200, SLOTS_ONE), ("POST", "/slots/0"): (200, "<html>oops</html>")}
        client, _ = client_with_routes(routes)
        from kv_cache.errors import SlotApiError

        with pytest.raises(SlotApiError) as excinfo:
            client.save("a.bin")
        assert "oops" in str(excinfo.value)

    def test_empty_body_raises(self):
        routes = {("GET", "/slots"): (200, SLOTS_ONE), ("POST", "/slots/0"): (200, "")}
        client, _ = client_with_routes(routes)
        from kv_cache.errors import SlotApiError

        with pytest.raises(SlotApiError):
            client.save("a.bin")

    def test_json_array_instead_of_object_raises(self):
        routes = {("GET", "/slots"): (200, SLOTS_ONE), ("POST", "/slots/0"): (200, "[1,2,3]")}
        client, _ = client_with_routes(routes)
        from kv_cache.errors import SlotApiError

        with pytest.raises(SlotApiError) as excinfo:
            client.save("a.bin")
        assert "list" in str(excinfo.value)

    def test_list_slots_non_array_raises(self):
        routes = {("GET", "/slots"): (200, "{}")}
        client, _ = client_with_routes(routes)
        from kv_cache.errors import SlotApiError

        with pytest.raises(SlotApiError):
            client.list_slots()

    def test_list_slots_empty_raises(self):
        routes = {("GET", "/slots"): (200, "[]")}
        client, _ = client_with_routes(routes)
        from kv_cache.errors import SlotApiError

        with pytest.raises(SlotApiError) as excinfo:
            client.resolve_slot_id()
        assert "槽位" in str(excinfo.value)

    def test_timeout_is_flagged(self):
        """超时是独立语义：服务端可能只是慢，不是坏（T-09 要分别处置）。"""

        def boom(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("timed out", request=request)

        http = httpx.Client(base_url=BASE_URL, transport=httpx.MockTransport(boom), trust_env=False)
        client = LlamaSlotClient(BASE_URL, 1.0, client=http)
        from kv_cache.errors import SlotApiError

        with pytest.raises(SlotApiError) as excinfo:
            client.save("a.bin")
        assert excinfo.value.timeout is True

    def test_connect_error_is_not_marked_timeout(self):
        def boom(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused", request=request)

        http = httpx.Client(base_url=BASE_URL, transport=httpx.MockTransport(boom), trust_env=False)
        client = LlamaSlotClient(BASE_URL, client=http)
        from kv_cache.errors import SlotApiError

        with pytest.raises(SlotApiError) as excinfo:
            client.save("a.bin")
        assert excinfo.value.timeout is False

    def test_all_errors_are_kv_cache_error_subclasses(self):
        """缓存层故障不能穿透到主链路：所有异常都必须能被 `KVCacheError` 兜住。"""
        from kv_cache.errors import KVCacheError

        assert issubclass(__import__("kv_cache.errors", fromlist=["SlotApiError"]).SlotApiError, KVCacheError)
        assert issubclass(
            __import__("kv_cache.errors", fromlist=["SlotApiUnavailable"]).SlotApiUnavailable, KVCacheError
        )


# ---- filename 校验（C3） ---------------------------------------------------------------


class TestFilenameValidation:
    @pytest.mark.parametrize(
        "filename",
        ["sub/nested.bin", "sub\\nested.bin", "../escape.bin", "a/../b.bin", "", ".", "..", "./a.bin"],
    )
    def test_rejects_path_like_names(self, filename: str):
        """C3 实测：带子目录/路径穿越一律 400 Invalid filename。"""
        assert is_valid_slot_filename(filename) is False

    @pytest.mark.parametrize("filename", ["a.bin", "slotcache_deadbeef.bin", "x", "a.b.c.bin"])
    def test_accepts_plain_names(self, filename: str):
        assert is_valid_slot_filename(filename) is True

    def test_save_rejects_before_sending_any_request(self):
        """非法文件名在**发请求之前**就拒绝 —— 不打无谓的 RTT，也不靠服务端兜底。"""
        client, recorder = client_with_routes(ok_routes())
        with pytest.raises(ValueError) as excinfo:
            client.save("sub/nested.bin")
        assert "纯文件名" in str(excinfo.value)
        assert recorder.count == 0, "非法文件名不该产生任何请求"

    def test_does_not_sanitize_silently(self):
        """**故意不净化**：净化会把两个不同的键悄悄映射到同一个文件（REQ-W2）。"""
        client, _ = client_with_routes(ok_routes())
        with pytest.raises(ValueError):
            client.save("dir/../../slotcache_x.bin")


# ---- from_config ----------------------------------------------------------------------


class TestFromConfig:
    def test_from_config_maps_fields(self, make_config):
        config = make_config(restore_timeout_s=7.5, capability_ttl_s=42.0, server_base_url="http://127.0.0.1:8123")
        client = LlamaSlotClient.from_config(config)
        try:
            assert client.base_url == "http://127.0.0.1:8123"
            assert client.timeout == 7.5
            assert client.capability_ttl_s == 42.0
        finally:
            client.close()


# ---- 可选：真实服务冒烟（默认不跑） ------------------------------------------------------


@pytest.mark.integration
def test_integration_against_real_server():
    """真实 llama-server 冒烟（可选验收项）。需要 `KV_TEST_SERVER` 指向一个**配了 `--slot-save-path`** 的实例。

    默认不跑：`pyproject.toml` 的 `addopts` 里带了 `-m "not integration"`；
    要跑就 `pytest -m integration`（命令行参数在 addopts 之后，会覆盖它）。

    跑的是**完整往返**（探活 → 能力 → slot 解析 → save → restore），
    和 T-06 手工实测那次一致，这样「验证过」是可复现的，而不是一次性口头结论。
    """
    import os

    base_url = os.environ.get("KV_TEST_SERVER")
    if not base_url:
        pytest.skip("未设置 KV_TEST_SERVER，跳过真实服务冒烟")
    client = LlamaSlotClient(base_url, timeout=60.0)
    try:
        assert client.health() is True
        capability = client.capabilities()
        assert capability.available is True, capability.detail
        assert capability.probe_status == 400
        assert client.resolve_slot_id() == 0

        filename = "t06_integration_probe.bin"
        saved = client.save(filename)
        assert saved.http_status == 200
        assert saved.n_tokens is not None and saved.n_tokens > 0
        assert saved.n_bytes is not None and saved.n_bytes > 0
        assert saved.server_ms is not None

        restored = client.restore(filename)
        assert restored.http_status == 200
        # 同一份文件，读写两侧的 token 数与字节数必须一致 —— 这是 restore 真的读到了东西的最低证据
        assert restored.n_tokens == saved.n_tokens
        assert restored.n_bytes == saved.n_bytes
    finally:
        client.close()
