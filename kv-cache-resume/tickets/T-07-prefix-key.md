# T-07 · prefix key 计算与归一化（AC7）

| 项 | 值 |
| :--- | :--- |
| 状态 | ⬜ 未开始 |
| Epic | E1 · 策略层（SPEC Phase 1） |
| 阻塞于 | T-05 |
| 阻塞 | T-09 |
| SPEC 依据 | REQ-U1、REQ-U2、REQ-W2、AC7、R1、§4.3 |
| 预估 | 3~4 h |

## 要做出什么

给定一次请求（模型 + 量化 + 完整 token 序列），算出一个**稳定的缓存键**；同时提供一组**归一化约定**，让「逻辑上相同的 prompt」不会因为无害的序列化差异算出不同的键。

正面：同前缀必命中。
反面：**任何一处真实差异都必须判 miss**——宁可重算，绝不用错的 KV 产出错的结果（REQ-W2）。

## 范围

**做**

- `kv_cache/prefix.py`
  - `compute_key(model_id, quant, token_ids) -> str`：`sha256(model_id ‖ quant ‖ token_ids)[:16]`
    - **编码要定死**：分隔符、`token_ids` 的序列化方式（建议紧凑二进制或定长拼接，不要 `repr(list)`——`str([1, 2])` 与 `str([1,2])` 不同会导致同输入不同键）
  - `tokenize(messages)` 接口：本票只定签名 + 一个可注入的实现（真实 tokenizer 由调用方给），因为策略层不该自己管模型词表
  - `canonicalize(messages)` —— **R1 的缓解措施**：对已知不稳定字段做归一化
    - JSON key 顺序稳定（`sort_keys=True` 那类处理）—— **直接相关**：LexAgent 的工具 schema 由 pydantic 从类型注解推导，key 顺序可能变
    - 空白/换行归一（chat template 渲染差异）
    - **不稳定字段检测**：时间戳 / 随机 ID / 动态内容 → 检测到就**拒绝缓存该请求**（而不是硬归一化后假装安全）
- `tests/test_prefix.py`
  - 幂等：同输入两次算出同键
  - **AC7 三组扰动样本**，每组都必须判 miss：
    1. schema JSON key 顺序变动
    2. 空白 / 换行差异
    3. 时间戳变动
  - REQ-U2 前置守护：`model_id` 变 → 键必变；`quant` 变 → 键必变
  - `canonicalize` 的正面用例：等价输入归一化后 token 序列一致

**不做**

- 不实现真实 tokenizer（模型相关，留给接入侧）
- 不做存储 / 不碰磁盘（那是 T-08）
- 不做「模糊匹配」「前缀截断复用」—— 本票是**精确匹配**语义，SPEC 的设计就是这样

## 交付物

| 路径 | 内容 |
| :--- | :--- |
| `kv-cache-resume/kv_cache/prefix.py` | 键计算 + 归一化 + 不稳定字段检测 |
| `kv-cache-resume/tests/test_prefix.py` | 含 AC7 三组扰动样本 |
| `docs/phase1-design.md` 补章 | 「哪些字段被归一化、哪些被拒绝」的显式清单 |

## 验收清单

- [ ] **AC7：三组扰动全部判定为 miss**，且断言**没有使用旧 KV**（不只是键变了，是走了 miss 分支）
- [ ] 幂等：同输入两次计算结果相同（含 bytes 级比较）
- [ ] REQ-U2 守护：模型标识或量化变化 → 键必变
- [ ] 不稳定字段（时间戳/随机 ID）被**检测并拒绝缓存**，有单测覆盖「检测到 → 拒绝」路径
- [ ] 代码里没有 `repr()` / `str()` 之类的隐式序列化（用显式编码，避免同输入不同键）
- [ ] `docs/phase1-design.md` 的归一化清单与实际实现一致（不许多写或少写）

## 备注

- **本票最容易踩的坑是「看起来对」**：`hash(str(token_ids))` 能跑、大多数时候也能命中，但在 Python 版本 / 分隔符 / 空格细节上会悄悄出错。键必须每次都能复现，所以序列化方式要在代码里写死并配注释解释。
- AC7 的第 3 组（时间戳）是**唯一一个无法靠归一化解决**的：时间戳一变，token 序列就真变了，缓存就不该命中——除非把时间戳从 prompt 里挪走（那是上游的事）。本票要做到的是「**正确判 miss**」，不是「想办法命中」。
- 归一化清单写进 `docs/phase1-design.md` 是给 T-13 验收用的：验收时按清单逐条构造样本。

## 完成后

更新 [`README.md`](./README.md) 状态表 → [T-09](./T-09-hit-and-restore.md)
