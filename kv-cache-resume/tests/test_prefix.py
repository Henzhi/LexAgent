"""`kv_cache.prefix` 的全离线单测（T-07）。

覆盖 AC7 的三组扰动、REQ-U2 的守护、编码无歧义性、归一化的正反两面，
以及不稳定字段的「检测到 → 拒绝缓存」路径。

AC7 的「判 miss」在本票的落点：`compute_key()` 对 token 序列**不做归一化**，
所以扰动一旦到达 token 层，键必然不同 —— 而「键不同」要有可观察的后果，
所以用一个 `{key: payload}` 的假索引来承载「旧 KV」，断言查不到。
真正的端到端分支断言在 T-13（那时有 store 与 policy）。

SPEC 依据：REQ-U1、REQ-U2、REQ-W2、AC7、R1、§4.3。
"""

from __future__ import annotations

import pytest

from kv_cache.errors import PrefixMismatch
from kv_cache.prefix import (
    KEY_HEX_LEN,
    UNSTABLE_PATTERNS,
    CanonicalJsonByteTokenizer,
    canonical_json,
    canonicalize,
    compute_key,
    detect_unstable,
    encode_key_material,
    format_findings,
    normalize_whitespace,
    prefix_key_for_messages,
    tokenize_messages,
)

MODEL = "qwen2.5-3b-instruct"
QUANT = "q4_k_m"


def fake_text_tokenizer(messages):
    """测试用的确定性 tokenizer：把消息序列化后按字节切。语义同 `CanonicalJsonByteTokenizer`。"""
    return list(canonical_json([dict(m) for m in messages]).encode("utf-8"))


def tokens_of(text: str) -> list[int]:
    """把一段文本按字节当 token —— 用于构造「至少差一个 token」的序列。"""
    return list(text.encode("utf-8"))


class FakeIndex:
    """假的缓存索引：`key -> payload`。用来观察「键不同 ⇒ 查不到旧 KV」。"""

    def __init__(self) -> None:
        self.entries: dict[str, str] = {}

    def put(self, key: str, payload: str = "OLD_KV") -> None:
        self.entries[key] = payload

    def lookup(self, key: str) -> str | None:
        """命中返回旧 KV，未命中返回 None —— 模拟「走 miss 分支」。"""
        return self.entries.get(key)


# ---- REQ-U1 / 编码 ----------------------------------------------------------------


class TestEncodingAndDeterminism:
    def test_idempotent_same_input_same_key(self):
        tokens = [1, 2, 3, 4128]
        assert compute_key(MODEL, QUANT, tokens) == compute_key(MODEL, QUANT, tokens)

    def test_idempotent_at_bytes_level(self):
        """bytes 级比较 —— 键相同必须来自**编码**相同，而不是哈希碰巧一样。"""
        tokens = [7, 8, 9]
        assert encode_key_material(MODEL, QUANT, tokens) == encode_key_material(MODEL, QUANT, tokens)

    def test_key_shape(self):
        key = compute_key(MODEL, QUANT, [1, 2, 3])
        assert len(key) == KEY_HEX_LEN
        assert key == key.lower()
        assert all(c in "0123456789abcdef" for c in key)

    def test_encoding_is_bytes_not_repr(self):
        """验收：编码产出 bytes，且**不依赖 `repr()` / `str()` 的表示法细节**。

        这里用行为断言而不是去 grep 源码里的 `repr(` ——
        grep 会被注释与无害写法骗过，行为断言不会（见下面三组碰撞用例）。
        """
        material = encode_key_material(MODEL, QUANT, [1, 2])
        assert isinstance(material, bytes)
        assert b"kvprefix" in material

    def test_no_collision_when_splitting_differently(self):
        """`model_id` 与 `quant` 的边界不能被拼接抹掉（长度前缀保证）。"""
        assert compute_key("ab", "c", [1]) != compute_key("a", "bc", [1])

    def test_no_collision_between_token_boundaries(self):
        """`[1, 2]` 与 `[12]` 在朴素拼接下都是 "12"，必须算出不同键。"""
        assert compute_key(MODEL, QUANT, [1, 2]) != compute_key(MODEL, QUANT, [12])

    def test_no_collision_between_permutations(self):
        assert compute_key(MODEL, QUANT, [1, 2]) != compute_key(MODEL, QUANT, [2, 1])

    def test_empty_token_sequence_is_valid_but_distinct(self):
        assert compute_key(MODEL, QUANT, []) != compute_key(MODEL, QUANT, [0])
        assert len(compute_key(MODEL, QUANT, [])) == KEY_HEX_LEN


# ---- REQ-U2 守护 -----------------------------------------------------------------


class TestReqU2Guards:
    def test_model_id_change_changes_key(self):
        assert compute_key("model-a", QUANT, [1, 2]) != compute_key("model-b", QUANT, [1, 2])

    def test_quant_change_changes_key(self):
        assert compute_key(MODEL, "f16", [1, 2]) != compute_key(MODEL, "q8", [1, 2])

    def test_quant_case_is_not_folded(self):
        """大小写折叠方向是反的：会把两个写法不同的输入当成同一个。

        统一大小写是 `config.KV_QUANT_CHOICES` 的职责；本函数把它当不透明的版本串。
        """
        assert compute_key(MODEL, "q4_k_m", [1]) != compute_key(MODEL, "Q4_K_M", [1])

    @pytest.mark.parametrize("bad", ["", "  "])
    def test_empty_model_id_rejected(self, bad):
        with pytest.raises(ValueError, match="model_id"):
            compute_key(bad.strip(), QUANT, [1])

    def test_empty_quant_rejected(self):
        with pytest.raises(ValueError, match="quant"):
            compute_key(MODEL, "", [1])

    @pytest.mark.parametrize("bad_tokens", [[-1], [2**32], [1, "2"], [1, 2.0], [True]])
    def test_invalid_token_ids_rejected(self, bad_tokens):
        with pytest.raises(ValueError):
            compute_key(MODEL, QUANT, bad_tokens)

    def test_bool_is_not_silently_a_token_id(self):
        """`isinstance(True, int)` 为真 —— 必须显式拒绝，否则 `True` 会变成 token `1`。"""
        with pytest.raises(ValueError, match="不是整数"):
            compute_key(MODEL, QUANT, [True])


# ---- AC7 三组扰动 -----------------------------------------------------------------


class TestAC7Perturbations:
    """三组扰动都必须判 miss。每组都断言「在假索引上查不到旧 KV」，而不只是「键变了」。"""

    def test_group1_schema_json_key_order(self):
        """第 1 组：工具 schema 的 JSON key 顺序变动（pydantic 推导顺序可能变）。"""
        index = FakeIndex()
        ordered = '{"type":"object","properties":{"a":{"type":"string"},"b":{"type":"integer"}}}'
        reordered = '{"properties":{"a":{"type":"string"},"b":{"type":"integer"}},"type":"object"}'

        old_key = compute_key(MODEL, QUANT, tokens_of(ordered))
        index.put(old_key)

        new_key = compute_key(MODEL, QUANT, tokens_of(reordered))
        assert new_key != old_key
        assert index.lookup(new_key) is None, "schema key 顺序变了却命中了旧 KV —— 必须走 miss"

    def test_group2_whitespace(self):
        """第 2 组：空白 / 换行差异 -> token 序列不同 -> 键不同 -> miss。"""
        index = FakeIndex()
        base = "system: 你是法律助手\nuser: 第 1161 条讲什么"
        variants = [
            "system: 你是法律助手\nuser:  第 1161 条讲什么",  # 行内多一个空格
            "system: 你是法律助手\n\nuser: 第 1161 条讲什么",  # 多一个空行
            "system: 你是法律助手\r\nuser: 第 1161 条讲什么",  # CRLF
        ]
        index.put(compute_key(MODEL, QUANT, tokens_of(base)))
        for variant in variants:
            assert variant != base
            assert index.lookup(compute_key(MODEL, QUANT, tokens_of(variant))) is None

    def test_group3_timestamp(self):
        """第 3 组：时间戳变动。**唯一无法靠归一化解决的一组** —— 正确做法是判 miss。"""
        index = FakeIndex()
        first = "现在是 2026-09-12T10:00:00+08:00，请回答：第 1161 条讲什么"
        second = "现在是 2026-09-12T10:00:01+08:00，请回答：第 1161 条讲什么"

        index.put(compute_key(MODEL, QUANT, tokens_of(first)))
        assert index.lookup(compute_key(MODEL, QUANT, tokens_of(second))) is None

    def test_group3_timestamp_is_also_refused_by_canonicalize(self):
        """时间戳不只要判 miss，还要**被检测出来并拒绝缓存**（R1 的显式处置）。"""
        messages = [{"role": "system", "content": "现在是 2026-09-12T10:00:00+08:00"}]
        with pytest.raises(PrefixMismatch) as excinfo:
            canonicalize(messages)
        assert "iso8601_datetime" in str(excinfo.value)

    def test_all_three_groups_end_to_end_via_prefix_key_for_messages(self):
        """三组扰动走同一条组装入口时，键也必须各不相同。

        用 `canonical=False` 明确表达「perturbation 直接进 token 层」这个前提。
        """
        base = [{"role": "user", "content": '{"a":1,"b":2} 请解释'}]
        perturbed = [
            [{"role": "user", "content": '{"b":2,"a":1} 请解释'}],  # key 顺序
            [{"role": "user", "content": '{"a":1,"b":2}  请解释'}],  # 空白
            [{"role": "user", "content": '{"a":1,"b":2} 请解释 2026-09-12T10:00:00'}],  # 时间戳
        ]
        base_key = prefix_key_for_messages(base, MODEL, QUANT, fake_text_tokenizer, canonical=False).key
        for messages in perturbed:
            key = prefix_key_for_messages(messages, MODEL, QUANT, fake_text_tokenizer, canonical=False).key
            assert key != base_key


# ---- 归一化的正面（R1 缓解措施） ---------------------------------------------------


class TestCanonicalizePositive:
    """归一化让**逻辑等价**的输入变成逐字节相同 —— 这是命中率恢复的唯一正路。"""

    def test_dict_valued_content_key_order_folded(self):
        """结构化的 message content（dict）按 key 排序 —— 与字段名无关，只看值是不是 dict。"""
        a = canonicalize([{"role": "user", "content": {"a": 1, "b": 2}}])
        b = canonicalize([{"role": "user", "content": {"b": 2, "a": 1}}])
        assert canonical_json(a) == canonical_json(b)

    def test_message_dict_key_order_folded(self):
        a = canonicalize([{"role": "user", "content": "你好"}])
        b = canonicalize([{"content": "你好", "role": "user"}])
        assert canonical_json(a) == canonical_json(b)

    def test_tool_call_arguments_json_reordered(self):
        """`arguments` 是**被序列化成字符串**的 JSON，要按 JSON 重排而不是按文本折空白。"""
        a = canonicalize([{"role": "assistant", "tool_calls": [{"function": {"arguments": '{"x":1,"y":2}'}}]}])
        b = canonicalize([{"role": "assistant", "tool_calls": [{"function": {"arguments": '{"y":2,"x":1}'}}]}])
        assert canonical_json(a) == canonical_json(b)

    def test_arguments_that_are_not_json_are_left_as_text(self):
        """`arguments` 里放自由文本时不能崩，也不能被当成 JSON 处理。"""
        result = canonicalize([{"role": "assistant", "tool_calls": [{"function": {"arguments": "检索 民法典"}}]}])
        assert result[0]["tool_calls"][0]["function"]["arguments"] == "检索 民法典"

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("a\r\nb", "a\nb"),
            ("a\rb", "a\nb"),
            ("a   \nb", "a\nb"),
            ("a\n\n\n\nb", "a\n\nb"),
            ("  a  ", "a"),
            ("a\n\nb", "a\n\nb"),  # 有意的空行要保留
            ("a  b", "a  b"),  # 行内多空格不合并
        ],
    )
    def test_normalize_whitespace_rules(self, raw, expected):
        assert normalize_whitespace(raw) == expected

    def test_canonicalize_creates_hits_for_equivalent_inputs(self):
        """等价输入归一化后应当**命中**（假索引上能查到）—— 与 AC7 的三组并不矛盾：

        AC7 要求「扰动到达 token 层必须 miss」；这里是「归一化把扰动在上游消掉，
        于是发送内容逐字节相同、真正用的是同一段前缀的 KV」。两条界见模块 docstring。

        用的是 `arguments`（结构化 JSON 落在字符串字段里）这种**能被识别的**形态。
        自由文本里内嵌的 JSON 不在归一化范围内，见下一条用例。
        """
        index = FakeIndex()
        first = [{"role": "assistant", "tool_calls": [{"function": {"arguments": '{"a":1,"b":2}'}}]}]
        second = [{"role": "assistant", "tool_calls": [{"function": {"arguments": '{"b":2,"a":1}'}}]}]

        key_a = prefix_key_for_messages(first, MODEL, QUANT, fake_text_tokenizer).key
        key_b = prefix_key_for_messages(second, MODEL, QUANT, fake_text_tokenizer).key
        index.put(key_a)
        assert key_a == key_b
        assert index.lookup(key_b) == "OLD_KV"

    def test_json_inside_free_text_is_not_reordered(self):
        """**明确的范围边界**：自由文本 `content` 里内嵌的 JSON 不做 key 排序。

        无从判断一段散文里哪一节是 JSON，硬去猜会把文本改坏。代价是这种形态下
        key 顺序变化仍算 miss —— **偏安全的方向**（多算一次，不会用错 KV）。
        要拿到归一化收益，调用方应把结构化内容放进 `arguments` 这类字段。
        """
        a = prefix_key_for_messages(
            [{"role": "user", "content": '{"a":1,"b":2} 请解释'}], MODEL, QUANT, fake_text_tokenizer
        )
        b = prefix_key_for_messages(
            [{"role": "user", "content": '{"b":2,"a":1} 请解释'}], MODEL, QUANT, fake_text_tokenizer
        )
        assert a.key != b.key

    def test_canonicalized_whitespace_variants_agree(self):
        a = prefix_key_for_messages([{"role": "user", "content": "a  \r\nb"}], MODEL, QUANT, fake_text_tokenizer)
        b = prefix_key_for_messages([{"role": "user", "content": "a\nb"}], MODEL, QUANT, fake_text_tokenizer)
        assert a.key == b.key

    def test_prefix_key_reports_metadata(self):
        result = prefix_key_for_messages([{"role": "user", "content": "hi"}], MODEL, QUANT, fake_text_tokenizer)
        assert result.model_id == MODEL
        assert result.quant == QUANT
        assert result.n_tokens > 0
        assert result.canonicalized is True
        assert len(result.key) == KEY_HEX_LEN


# ---- 不稳定字段检测 ---------------------------------------------------------------


class TestUnstableDetection:
    @pytest.mark.parametrize(
        ("text", "kind"),
        [
            ("现在是 2026-09-12T10:00:00", "iso8601_datetime"),
            ("截止 2026-09-12 10:00:00+08:00", "iso8601_datetime"),
            ("trace 550e8400-e29b-41d4-a716-446655440000", "uuid"),
            ("req a3f5b2c9d8e1f7a6", "hex_id"),
            ("<object at 0x7ffd12345678>", "memory_address"),
        ],
    )
    def test_detects(self, text, kind):
        findings = detect_unstable([{"role": "user", "content": text}])
        assert any(f.kind == kind for f in findings), findings

    @pytest.mark.parametrize(
        "text",
        [
            "《中华人民共和国民法典》第 1161 条",
            "2020 年 5 月 28 日通过",
            "合同签订日 2020-01-01",  # 裸日期是内容，不是注入的当前时间
            "联系电话 13812345678",  # 11 位纯数字
            "身份证 110101199003071234",  # 18 位纯数字
            "案号 （2023）京 01 民终 1234 号",
            "金额 1234567890123456",  # 16 位纯数字：hex_id 要求含 a-f 字母，故不误报
        ],
    )
    def test_no_false_positive_on_legal_text(self, text):
        """误报会直接把 AC4（命中率 ≥99%）打穿 —— 这些样本必须干净。

        裸日期、纯数字长串（手机号 / 身份证号 / 长编号）都在刻意排除之列，
        理由写在 `UNSTABLE_PATTERNS` 的注释里。
        """
        assert detect_unstable([{"role": "user", "content": text}]) == []

    def test_reports_path_and_sample(self):
        findings = detect_unstable([{"role": "user", "content": "id 550e8400-e29b-41d4-a716-446655440000"}])
        assert findings[0].field == "messages[0].content"
        assert findings[0].sample == "550e8400-e29b-41d4-a716-446655440000"

    def test_detects_inside_nested_structure(self):
        messages = [{"role": "assistant", "tool_calls": [{"function": {"arguments": '{"id":"a3f5b2c9d8e1f7a6"}'}}]}]
        findings = detect_unstable(messages)
        assert findings and findings[0].field == "messages[0].tool_calls[0].function.arguments"

    def test_duplicate_contained_findings_are_collapsed(self):
        """同一段文本被两条规则命中时只留最长的那条，避免噪声。

        `0xdeadbeef1234567890ab` 同时命中 `memory_address`（含 `0x` 前缀，整段 22 字符）
        与 `hex_id`（去掉前缀后 20 个十六进制字符）—— 应当只剩前者一条。
        """
        findings = detect_unstable([{"role": "user", "content": "0xdeadbeef1234567890ab"}])
        assert len(findings) == 1, [f.kind for f in findings]
        assert findings[0].kind == "memory_address"
        assert findings[0].sample == "0xdeadbeef1234567890ab"

    def test_findings_are_deterministically_ordered(self):
        text = "a 550e8400-e29b-41d4-a716-446655440000 b 2026-09-12T10:00:00"
        assert detect_unstable([{"role": "user", "content": text}]) == detect_unstable(
            [{"role": "user", "content": text}]
        )

    @pytest.mark.parametrize("pattern", UNSTABLE_PATTERNS, ids=lambda p: p.kind)
    def test_every_pattern_has_kind_and_description(self, pattern):
        """规则是数据 —— 加规则时漏写说明会让人拿到一条看不懂的告警。"""
        assert pattern.kind and pattern.description

    def test_format_findings_is_human_readable(self):
        findings = detect_unstable([{"role": "user", "content": "现在是 2026-09-12T10:00:00"}])
        text = format_findings(findings)
        assert "iso8601_datetime" in text
        assert "messages[0].content" in text


class TestCanonicalizeRefusesUnstable:
    def test_raises_prefix_mismatch(self):
        with pytest.raises(PrefixMismatch):
            canonicalize([{"role": "system", "content": "当前时间 2026-09-12T10:00:00"}])

    def test_message_explains_why_not_normalized(self):
        """验收：拒绝路径的消息要能自解释（为什么不是归一化掉、该怎么办）。"""
        with pytest.raises(PrefixMismatch) as excinfo:
            canonicalize([{"role": "user", "content": "id 550e8400-e29b-41d4-a716-446655440000"}])
        message = str(excinfo.value)
        assert "REQ-W2" in message
        assert "为什么不是" in message
        assert "check_unstable=False" in message

    def test_explicit_opt_out_is_allowed(self):
        """显式承担风险的开关要真的能用（不是装饰）。"""
        result = canonicalize([{"role": "user", "content": "现在是 2026-09-12T10:00:00"}], check_unstable=False)
        assert result[0]["content"] == "现在是 2026-09-12T10:00:00"

    def test_opt_out_still_canonicalizes(self):
        result = canonicalize([{"role": "user", "content": "a  \r\nb"}], check_unstable=False)
        assert result[0]["content"] == "a\nb"

    def test_prefix_mismatch_is_a_kv_cache_error(self):
        """必须能被 `KVCacheError` 兜住 —— 缓存层故障不能穿透到主链路。"""
        from kv_cache.errors import KVCacheError

        assert issubclass(PrefixMismatch, KVCacheError)


# ---- tokenize 接口 ----------------------------------------------------------------


class TestTokenize:
    def test_uses_injected_tokenizer(self):
        calls: list[object] = []

        def tokenizer(messages):
            calls.append(messages)
            return [1, 2, 3]

        messages = [{"role": "user", "content": "hi"}]
        assert tokenize_messages(messages, tokenizer) == [1, 2, 3]
        assert calls and list(calls[0]) == messages

    def test_empty_messages_rejected(self):
        with pytest.raises(ValueError, match="不能为空"):
            tokenize_messages([], fake_text_tokenizer)

    @pytest.mark.parametrize("bad", [[1, "2"], [1, -1], [1.5], [True]])
    def test_tokenizer_output_is_validated(self, bad):
        """注入实现返回了非法值要当场报 —— 否则会退化成「键算得出但永不命中」。"""
        with pytest.raises(ValueError):
            tokenize_messages([{"role": "user", "content": "hi"}], lambda _messages: bad)

    def test_stub_tokenizer_is_deterministic_and_injective(self):
        """替身的两条最低要求：确定性 + 单射（差一个字符必得不同序列）。"""
        stub = CanonicalJsonByteTokenizer()
        a = stub([{"role": "user", "content": "hello"}])
        b = stub([{"role": "user", "content": "hello"}])
        c = stub([{"role": "user", "content": "hellp"}])
        assert a == b
        assert a != c
        assert all(isinstance(t, int) and t >= 0 for t in c)

    def test_stub_tokenizer_documents_itself_as_a_test_double(self):
        assert "测试替身" in (CanonicalJsonByteTokenizer.__doc__ or "")
