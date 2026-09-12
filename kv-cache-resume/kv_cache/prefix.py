"""prefix key 计算与输入归一化（T-07）。

给定「模型 + 量化 + 完整 token 序列」算出一个稳定键；同时提供输入侧的归一化约定，
让逻辑上相同的 prompt 不会因为无害的序列化差异算出不同的键。

SPEC 依据：REQ-U1 / REQ-U2 / REQ-W2、AC7、R1、§4.3。

--------------------------------------------------------------------- 两条必须分清的话

**归一化改写的是「输入」，绝不是「键」。**

- `compute_key()` 对 token 序列做哈希，**一个 token 都不归一化**。
  所以任何真正改变 token 序列的扰动（schema key 顺序 / 空白 / 时间戳）必然算出不同的键
  → 必然 MISS → 满足 AC7 的字面要求，也符合 REQ-W2 的「宁可重算，绝不用错 KV」。
- `canonicalize()` 是**上游可选的输入改写**：调用方若采用它，就必须把它的输出**当作真正要发送的内容**
  去 tokenize 与请求。这样「键」与「服务端里那份 KV」依旧严格对应，没有脱钩。
  R1 的缓解措施靠这条生效 —— 生效方式是**让等价输入变成逐字节相同**，而不是让不同的输入共享键。

如果反过来做（键归一化、发送原始文本），就会出现「键命中但服务端 KV 属于另一段前缀」——
那正是 REQ-W2 要防的事：restore 返回 200、答案却是错的，而且**静默**。

AC7 第 2 组（空白）与 R1 的「空白归一」表面冲突，本文件按上面这条界处置：
perturbation 到达 token 层就是 MISS（AC7）；归一化只作用于**可证明等价的序列化差异**，
且归一化结果即发送内容（R1）。详见 `docs/phase1-design.md` §4。
"""

from __future__ import annotations

import hashlib
import json
import re
import struct
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from .errors import PrefixMismatch

# --------------------------------------------------------------------------------------
# 键
# --------------------------------------------------------------------------------------

#: 键取 sha256 前 16 个十六进制字符（= 64 bit），SPEC §4.3 指定。
#: 64 bit 在「单机几万个条目」的量级下碰撞概率可忽略：n=1e5 时约 2.7e-10。
KEY_HEX_LEN = 16

#: 编码版本标签。**编码方式一旦改动必须换这个标签**，否则新旧编码可能算出同一个键，
#: 让「换了编码」这件事悄无声息地命中旧条目。
_ENCODING_TAG = b"kvprefix\x00v1"

#: token id 用无符号 32 位（词表规模远小于 2^32，但 int 上限校验必须有）
_MAX_TOKEN_ID = 2**32 - 1


def _coerce_token_ids(token_ids: Sequence[int]) -> list[int]:
    """校验并落地成 `list[int]`。

    刻意拒绝 `bool`：`isinstance(True, int)` 为真，一个 `True` 会被当成 token id `1`
    悄悄传下去 —— 这种错必须当场报，不能等到算出个看起来正常的键。
    """
    tokens: list[int] = []
    for index, value in enumerate(token_ids):
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"token_ids[{index}]={value!r} 不是整数（期望非负整数 token id）")
        if value < 0 or value > _MAX_TOKEN_ID:
            raise ValueError(f"token_ids[{index}]={value} 超出 [0, {_MAX_TOKEN_ID}] 范围")
        tokens.append(value)
    return tokens


def _length_prefixed(payload: bytes) -> bytes:
    """4 字节小端长度 + 原文。

    长度前缀是**防歧义**的关键：直接拼 `model_id + quant` 的话
    `("ab", "c")` 与 `("a", "bc")` 会得到同一串字节，从而算出同一个键 —— 两个不同的模型
    共用一份 KV，静默出错。加长度前缀后这种碰撞在结构上不可能发生。
    """
    return struct.pack("<I", len(payload)) + payload


def encode_key_material(model_id: str, quant: str, token_ids: Sequence[int]) -> bytes:
    """把键素材编码成**无歧义、跨平台一致**的字节串。

    为什么不用 `str(list)` / `repr()`：那是依赖 Python 表示法细节的隐式序列化，
    `str([1, 2])` 与 `str([1,2])` 就不同，Python 版本变化也可能改格式。
    键必须每次都能复现，所以这里把编码方式写死成「定长小端整数 + 长度前缀」，
    与 Python 版本、平台字节序都无关。

    返回字节串（而不是直接算哈希）是为了让「编码是否无歧义」可以被单测直接断言。
    """
    if not model_id:
        raise ValueError("model_id 不能为空 —— 空标识会让所有模型的键前缀相同（REQ-U2 的意义所在）")
    if not quant:
        raise ValueError("quant 不能为空 —— 空量化标识会让不同精度的 KV 共用一个键（REQ-U2）")

    tokens = _coerce_token_ids(token_ids)
    packed = struct.pack(f"<{len(tokens)}I", *tokens) if tokens else b""
    return b"".join(
        (
            _ENCODING_TAG,
            _length_prefixed(model_id.encode("utf-8")),
            _length_prefixed(quant.encode("utf-8")),
            _length_prefixed(packed),
        )
    )


def compute_key(model_id: str, quant: str, token_ids: Sequence[int]) -> str:
    """唯一键 = `sha256(model_id ‖ quant ‖ token_ids)[:16]`（REQ-U1、SPEC §4.3）。

    **不对 token 序列做任何归一化** —— 见模块 docstring 的「两条必须分清的话」。

    ⚠️ `quant` **不做大小写折叠**。折叠会让 `"Q4_K_M"` 与 `"q4_k_m"` 共用一个键，
    方向是反的（把两个写法不同的真实输入当成同一个）；大小写统一由 `config.KV_QUANT_CHOICES`
    在配置层保证。反向失误（同一量化被当成两个）只是少一次命中，代价小得多。
    """
    return hashlib.sha256(encode_key_material(model_id, quant, token_ids)).hexdigest()[:KEY_HEX_LEN]


# --------------------------------------------------------------------------------------
# 不稳定字段检测（R1：检测到 → 拒绝缓存，而不是硬归一化后假装安全）
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class UnstablePattern:
    """一条不稳定字段的识别规则。**规则是数据**：产品要加减规则只改下面的元组，不动函数。"""

    kind: str
    regex: re.Pattern[str]
    description: str


#: 会被判为「该请求不可缓存」的模式清单。
#:
#: **一条被明确否决的候选**：裸的 10/13 位 epoch 数字。它的误报面太大 ——
#: 法律语料里 11 位手机号、18 位身份证号、长编号比比皆是，误报会让本该命中的请求
#: 全部拒绝缓存，直接打穿 AC4（命中率 ≥99%）。**「宁可重算」不等于「宁可不缓存」**：
#: 前者只是慢一次，后者是收益归零。
#:
#: 同理，ISO 8601 模式**要求带时间部分**：裸日期（`2020-01-01` 这种合同签订日）
#: 绝大多数是**内容**而不是**注入的当前时间**，纳入检测同样会误伤 AC4。
UNSTABLE_PATTERNS: tuple[UnstablePattern, ...] = (
    UnstablePattern(
        kind="iso8601_datetime",
        regex=re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?"),
        description="ISO 8601 时间戳（带时间）——「当前时间」类注入的典型形态，两次请求之间必然不同",
    ),
    UnstablePattern(
        kind="uuid",
        regex=re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"),
        description="UUID —— 请求 id / trace id",
    ),
    UnstablePattern(
        kind="hex_id",
        # 两个前瞻：① 总长 ≥16 且到词边界；② 必须含至少一个 a-f 字母。
        # 第 ② 条是为了把纯数字长串（身份证号、长编号）排除在外 —— 否则误报同上。
        regex=re.compile(r"\b(?=[0-9a-fA-F]{16,}\b)(?=[0-9a-fA-F]*[a-fA-F])[0-9a-fA-F]{16,}\b"),
        description="长十六进制串（≥16 位且含字母）—— 随机指纹 / 会话 id",
    ),
    UnstablePattern(
        kind="memory_address",
        regex=re.compile(r"0x[0-9a-fA-F]{6,}"),
        description="内存地址形态 —— Python 默认 repr（如 `<obj at 0x7f...>`）混进 prompt 时必然每次不同",
    ),
)


@dataclass(frozen=True)
class UnstableFinding:
    """一处命中。`field` 是定位路径，`sample` 是命中原文，便于人直接去看那一句。"""

    kind: str
    field: str
    sample: str
    description: str
    start: int
    end: int


def _iter_string_leaves(node: Any, path: str):
    """遍历出所有字符串叶子及其路径（`messages[2].content` 这种可读路径）。"""
    if isinstance(node, str):
        yield path, node
    elif isinstance(node, Mapping):
        for key, value in node.items():
            yield from _iter_string_leaves(value, f"{path}.{key}")
    elif isinstance(node, (list, tuple)):
        for index, value in enumerate(node):
            yield from _iter_string_leaves(value, f"{path}[{index}]")


def detect_unstable(messages: Sequence[Mapping[str, Any]]) -> list[UnstableFinding]:
    """扫出所有不稳定字段（**只报告，不抛异常**）。

    与 `canonicalize()` 分开是为了让调用方可以先看一眼「到底哪几处不稳」再决定怎么办 ——
    直接抛异常的话，调用方只能看到一个字符串。
    """
    findings: list[UnstableFinding] = []
    for path, text in _iter_string_leaves(messages, "messages"):
        for pattern in UNSTABLE_PATTERNS:
            for match in pattern.regex.finditer(text):
                findings.append(
                    UnstableFinding(
                        kind=pattern.kind,
                        field=path,
                        sample=match.group(0),
                        description=pattern.description,
                        start=match.start(),
                        end=match.end(),
                    )
                )
    return _drop_contained(findings)


def _drop_contained(findings: list[UnstableFinding]) -> list[UnstableFinding]:
    """去掉被同字段其它命中完全包含的短命中（`0x7f...` 同时被两条规则命中时只报最长的那条）。"""
    ordered = sorted(findings, key=lambda f: (f.field, f.start, -(f.end - f.start)))
    kept: list[UnstableFinding] = []
    for finding in ordered:
        if any(k.field == finding.field and k.start <= finding.start and finding.end <= k.end for k in kept):
            continue
        kept.append(finding)
    return kept


def format_findings(findings: Sequence[UnstableFinding]) -> str:
    """把命中整理成人可读的多行文本（异常消息与日志共用）。"""
    return "\n".join(f"  - {f.field} [{f.kind}] {f.sample!r} —— {f.description}" for f in findings)


# --------------------------------------------------------------------------------------
# 归一化（输入改写，opt-in）
# --------------------------------------------------------------------------------------

#: 值本身是「一段被序列化成字符串的 JSON」的字段名。这些字段要先按 JSON 解析再重排，
#: 不能直接按普通文本折空白（那会把 JSON 里的字符串内容改坏）。
#:
#: **范围边界（明确不做的事）**：自由文本 `content` 里内嵌的 JSON **不**做 key 排序。
#: 散文里哪一节算 JSON 无从判断，硬猜会把文本改坏。代价是该形态下 key 顺序变化仍算 miss，
#: 方向是安全的（多算一次，不会用错 KV）。要拿归一化收益就把结构化内容放进这些字段。
_JSON_STRING_FIELDS = frozenset({"arguments", "parameters", "schema", "input", "args"})


def normalize_whitespace(text: str) -> str:
    """折叠**可证明等价**的排版差异。规则就这四条，不多不少：

    1. `\\r\\n` / `\\r` → `\\n`（Windows 与 Unix 换行统一）
    2. 每行去掉行尾空格/tab
    3. 连续 3 个以上换行折成 2 个（保留「空一行」这个有意的分段，多余的抹平）
    4. 去掉整串首尾空白

    **刻意不做**的事：不合并行内多个空格、不删行首缩进。
    行内空格在模板渲染里可能是语义的一部分，贸然合并会造出「当前缀被改写」的假命中。
    """
    unified = text.replace("\r\n", "\n").replace("\r", "\n")
    stripped_lines = [line.rstrip(" \t") for line in unified.split("\n")]
    collapsed = re.sub(r"\n{3,}", "\n\n", "\n".join(stripped_lines))
    return collapsed.strip()


def canonical_json(value: Any) -> str:
    """稳定序列化：key 排序、无多余空格、不转义非 ASCII。

    这是 R1 里「工具 schema 的 JSON key 顺序可能变」的正面处置 ——
    pydantic 从类型注解推导出的字段顺序变了，序列化结果仍逐字节相同。
    """
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _canonical_value(value: Any, key_hint: str | None) -> Any:
    if isinstance(value, str):
        if key_hint in _JSON_STRING_FIELDS:
            try:
                parsed = json.loads(value)
            except ValueError:
                pass  # 不是 JSON（比如 arguments 里放的是自由文本），按普通文本走
            else:
                if isinstance(parsed, (Mapping, list)):
                    return canonical_json(_canonical_value(parsed, None))
        return normalize_whitespace(value)
    if isinstance(value, Mapping):
        # key 排序让 dict 的序列化结果与插入顺序无关
        return {key: _canonical_value(value[key], key) for key in sorted(value)}
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item, key_hint) for item in value]
    return value


def canonicalize(
    messages: Sequence[Mapping[str, Any]],
    *,
    check_unstable: bool = True,
) -> list[dict[str, Any]]:
    """把消息列表归一化成**稳定的等价形式**。

    ⚠️ 返回的这份内容**就是调用方应当真正发送（并 tokenize）的内容**。
    只改键不改发送内容会让键与服务端那份 KV 脱钩 —— 那是 REQ-W2 的静默错答案。

    检测到不稳定字段时抛 `PrefixMismatch`（REQ-W2 的「宁可重算」）：
    时间戳 / UUID 这类字段一变，token 序列就真变了，缓存**本来就不该命中**。
    正确做法是把它们从 prompt 里挪走（上游的事），而不是在这里归一化掉。

    `check_unstable=False` 是显式承担风险的开关：调用方确认这些字段与答案无关时才用。
    """
    if check_unstable:
        findings = detect_unstable(messages)
        if findings:
            raise PrefixMismatch(
                f"输入含 {len(findings)} 处不稳定字段，拒绝缓存该请求（REQ-W2 / R1）：\n"
                f"{format_findings(findings)}\n"
                "为什么不是「归一化掉」：这些字段一变，token 序列就真的变了，"
                "旧 KV 对应的不是这次的前缀，宁可重算（REQ-W2）。\n"
                "怎么办：把不稳定内容从 prompt 里移走（上游的事）；"
                "若确认它与答案无关，可显式传 check_unstable=False 承担该风险。"
            )
    return [dict(_canonical_value(message, None)) for message in messages]


# --------------------------------------------------------------------------------------
# tokenize 接口（签名归本层，词表归调用方）
# --------------------------------------------------------------------------------------


class MessageTokenizer(Protocol):
    """消息 → token id 序列的**可注入**实现。

    为什么签名收的是 messages 而不是 text：聊天模板渲染（把 messages 拼成一段文本）
    与分词都属于模型侧的知识，本层**不该自己管模型词表**。
    由调用方注入一个同时负责「渲染 + 分词」的实现（例如转发给 llama-server 的
    `/apply-template` + `/tokenize`）即可。
    """

    def __call__(self, messages: Sequence[Mapping[str, Any]]) -> Sequence[int]: ...


def tokenize_messages(messages: Sequence[Mapping[str, Any]], tokenizer: MessageTokenizer) -> list[int]:
    """调用注入的 tokenizer，并**校验它的输出**。

    校验不是多余的：一个返回字符串或负数的 tokenizer 会把错误推迟到「键算出来了但永远不命中」，
    那是最难查的一类问题，不如在这里当场报。
    """
    if not messages:
        raise ValueError("messages 不能为空 —— 空请求没有前缀可言，不应参与缓存")
    return _coerce_token_ids(list(tokenizer(messages)))


class CanonicalJsonByteTokenizer:
    """**测试替身 / 离线占位实现**，不是真实分词器。

    `messages → canonical_json → utf-8 bytes → token id`（每个字节当一个 token）。

    它的价值在于满足 prefix 相关逻辑对 tokenizer 的最低要求：**确定性**与**单射** ——
    同一输入恒得同一序列，输入不同（哪怕只差一个空格）序列必不同。
    真实接入时必须换成模型自己的 tokenizer（见 `MessageTokenizer`）。

    别用它算出来的键去和真实服务端对话：字节序列与模型的词表 token 完全不是一回事。
    """

    def __call__(self, messages: Sequence[Mapping[str, Any]]) -> list[int]:
        return list(canonical_json(list(messages)).encode("utf-8"))


# --------------------------------------------------------------------------------------
# 组装
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class PrefixKey:
    """一次请求的缓存键及其口径（便于遥测与排查时一起带出来）。"""

    key: str
    model_id: str
    quant: str
    n_tokens: int
    canonicalized: bool


def prefix_key_for_messages(
    messages: Sequence[Mapping[str, Any]],
    model_id: str,
    quant: str,
    tokenizer: MessageTokenizer,
    *,
    canonical: bool = True,
    check_unstable: bool = True,
) -> PrefixKey:
    """端到端组装：归一化（可选）→ tokenize → 算键。

    `canonical=True` 时返回的 token 序列对应的是**归一化后**的内容，
    调用方必须拿这份去发请求（见 `canonicalize` 的 ⚠️）。
    """
    prepared = canonicalize(messages, check_unstable=check_unstable) if canonical else [dict(m) for m in messages]
    token_ids = tokenize_messages(prepared, tokenizer)
    return PrefixKey(
        key=compute_key(model_id, quant, token_ids),
        model_id=model_id,
        quant=quant,
        n_tokens=len(token_ids),
        canonicalized=canonical,
    )


__all__ = [
    "KEY_HEX_LEN",
    "UNSTABLE_PATTERNS",
    "CanonicalJsonByteTokenizer",
    "MessageTokenizer",
    "PrefixKey",
    "UnstableFinding",
    "UnstablePattern",
    "canonical_json",
    "canonicalize",
    "compute_key",
    "detect_unstable",
    "encode_key_material",
    "format_findings",
    "normalize_whitespace",
    "prefix_key_for_messages",
    "tokenize_messages",
]
