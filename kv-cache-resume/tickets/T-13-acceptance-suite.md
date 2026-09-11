# T-13 · 验收套件与报告（AC4~AC7）

| 项 | 值 |
| :--- | :--- |
| 状态 | ⬜ 未开始 |
| Epic | E1 · 策略层（SPEC Phase 1） |
| 阻塞于 | T-09、T-10、T-11、T-12；T-03（软，脚本复用） |
| 阻塞 | T-14 |
| SPEC 依据 | AC4~AC7、REQ-O1、G1~G4 |
| 预估 | 4~5 h |

## 要做出什么

**一条命令**跑完离线验收，产出一份能直接对照 SPEC §6 的报告。同时把 `VERIFY_EXACT` 从 T-03 的裸脚本**产品化**成策略层的一个开关模式（REQ-O1）。

本票不写新功能——只做「把已有能力变成可验收的证据」。

## 范围

**做**

- **REQ 覆盖反查**：按 [`README.md`](./README.md) 的 REQ 追溯矩阵逐条检查 18 条需求是否有测试或实测证据。**缺一条即本票未完成**，不许用「大体覆盖了」糊过去
- **AC4 复现**：100 次同前缀重发，统计命中率 ≥99%
  - 离线 harness（mock engine）跑，快且确定
  - 真机验证可选（标 `@pytest.mark.integration`），跑了就在报告里额外记一笔
- **`VERIFY_EXACT` 产品化**（REQ-O1）：把 T-03 的验证链接进策略层，成为可开关的验收模式
  - 复用 T-03 的脚本逻辑（不要复制粘贴一份新的），差异只在「走策略层」还是「裸 API」
  - 输出仍为 token 级 diff，结论与 T-03 可比
- `scripts/verify_acceptance.py`：一条命令跑完，输出汇总
- `docs/phase1-验收报告.md`：
  - AC1~AC7 逐条：判据 / 目标值 / **实测值** / 证据路径 / 结论
  - **未达标项与原因**（不许美化，不许把「未验证」写成「通过」）
  - 数据来源逐项标路径（Phase 0 报告或本票离线数据）
  - 遗留问题清单
- `docs/phase1-design.md` 定稿：把各票补章合并成完整设计文档

**不做**

- 不修 bug（发现的问题开新票或记入遗留）
- 不改 SPEC（若实测与 SPEC 冲突，**在这里记录冲突**，单独提改动，不在本票顺手改）

## 交付物

| 路径 | 内容 |
| :--- | :--- |
| `kv-cache-resume/scripts/verify_acceptance.py` | 一条命令跑完验收 |
| `kv-cache-resume/tests/` | 补齐至 REQ 全覆盖 |
| `kv-cache-resume/docs/phase1-验收报告.md` | 对照 SPEC §6 的验收报告 |
| `kv-cache-resume/docs/phase1-design.md` | 定稿版完整设计 |

## 验收清单

- [ ] **REQ 对照表**：18 条 REQ 每条都有测试或实测证据（列表逐条打勾，可核对）
- [ ] **AC4 ≥99%**（100 次同前缀重发，报告给出实际数字与失败样本）
- [ ] **AC5 / AC6 / AC7** 由离线套件复现，报告引用测试文件名与用例名
- [ ] **AC1 / AC2 / AC3** 引用 Phase 0 报告数据（若 E0 未做，标注「未验证」并说明原因）
- [ ] **REQ-O1**：`VERIFY_EXACT` 开关能跑通完整 save→restart→restore→续生成 diff
- [ ] 报告明确列出**未达标 / 未验证项**与原因
- [ ] `pytest -q` 全绿；`ruff check` + `ruff format --check` 通过
- [ ] `verify_acceptance.py` 一条命令可复现（重跑结论一致）

## 备注

- **本票的价值全在「诚实」**。SPEC §8.1 已经明说这个 spike 不承诺线上收益，所以报告里出现「某条 AC 没达标」是**正常且被允许**的结论——把没达标的写成达标，才会让整个项目的可信度归零。
- AC4 用 mock 跑是刻意的：真实 100 次重发要占 GPU 且慢，而 AC4 验的是**策略层的命中判定**，mock 恰恰是最精确的验证（能断言「第 2 次一定走 restore」）。
- `VERIFY_EXACT` 产品化时注意：它是**验收模式**，默认关闭（`VERIFY_EXACT=false`）。开着跑会显著变慢，不能进常规路径。

## 完成后

更新 [`README.md`](./README.md) 状态表 → 提交 PR 合回 `main`；若继续，进入 [T-14](./T-14-phase2-decision-frozen.md)（需先解冻）
