#!/usr/bin/env python
"""T-03 · 跨进程续生成 + 逐字比对（AC1）。

证明「生成 → save → **杀进程** → 重启 → restore → 续生成」得到的 token 序列，
与「不中断一次跑完」**token 级完全相同**。这是整个 spike 的地基。

本脚本是**裸 API 驱动的验证链**，不 import `kv_cache/`（T-03 范围约定）。

两条轨迹
--------
1. **基线**：一次不中断生成 N 个 token，记录 token id 序列与 sha256。
2. **分段**：生成 K 个 → `save` → 杀进程 → 重启 → `restore` → 续生成到 N 个。

采样参数钉死 `temperature=0` + 显式 `seed`（KV 里含 RNG 与 logits 状态，参数漂移会让两条
轨迹必然分叉 —— 那是脚本问题不是机制问题，但会浪费一整天）。

三个踩过的坑（都在实测里撞过，别重蹈）
--------------------------------------
1. **回灌必须用 token id 数组，不能用文本**。把生成文本拼回 prompt 再发，会走
   detokenize → retokenize 往返，边界处 token 化不保证一致。
2. **`tokens_evaluated` 不是缓存命中的指标** —— 它始终等于 prompt 的 token 总数。
   真正的指标是 `timings.cache_n`（复用了几条）。曾据此误判「缓存复用失效」。
3. **`trust_env=False` 必须开**（T-02 结论 C6）—— 否则沙箱代理让请求 200/404 交替。

用法
----
    python scripts/03_verify_exact.py --report reports/ac1-exact-match.json
    python scripts/03_verify_exact.py --ks 128,255 --n-tokens 256      # 快速自检
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

from kvbench_common import (
    LLAMA_BIN_DEFAULT,
    MODEL_DEFAULT,
    ServerHandle,
    SlotClient,
    compare_tokens,
    process_lifecycle,
    sha256_ints,
    write_report,
)

PROMPT_DEFAULT = "Write a detailed technical explanation of how a transformer language model works."


# --------------------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------------------


@dataclass
class Trial:
    k: int
    filename: str
    precondition_ok: bool = False
    precondition: dict = field(default_factory=dict)
    segment1: dict | None = None
    save: dict | None = None
    killed_pid: int | None = None
    killed_returncode: int | None = None
    restart_pid: int | None = None
    restore: dict | None = None
    segment2: dict | None = None
    concat_sha256: str | None = None
    comparison: dict | None = None
    passed: bool = False
    notes: list[str] = field(default_factory=list)


def run_trial(
    server: ServerHandle,
    client: SlotClient,
    args: argparse.Namespace,
    k: int,
    baseline: dict,
    prompt_tokens: list[int],
) -> Trial:
    trial = Trial(k=k, filename=f"ac1-K{k}.bin")

    # --- 段 1：跑 K 个 token ---
    seg1 = client.generate(args.prompt, k, args.seed)
    trial.segment1 = seg1

    # 前置条件：分段跑的前 K 个 token 必须与基线前 K 个一致。
    # 若这里就不一致，问题在「采样/请求边界」而不在 restore，不能算到本票头上。
    expected_head = baseline["tokens"][:k]
    precondition = compare_tokens(expected_head, seg1["tokens"])
    trial.precondition = precondition
    trial.precondition_ok = precondition["equal"]
    if not trial.precondition_ok:
        trial.notes.append("前置不一致：同参数分段跑的前 K 个 token 就与基线不同 —— 先查采样参数，不是 restore 的问题")

    # --- save ---
    trial.save = client.slot_action("save", trial.filename)

    # --- 杀进程（真杀） ---
    killed = server.stop()
    trial.killed_pid = killed.pid if killed else None
    trial.killed_returncode = killed.returncode if killed else None
    trial.notes.append(f"已杀进程 pid={trial.killed_pid} returncode={trial.killed_returncode}")

    # --- 重启 ---
    restarted = server.start()
    trial.restart_pid = restarted.pid

    # --- restore ---
    if not client.health():
        raise RuntimeError("重启后 /health 不通")
    trial.restore = client.slot_action("restore", trial.filename)

    # --- 段 2：用 **token 数组** 回灌（不能拼文本，见模块 docstring 坑 1） ---
    continuation_prompt = prompt_tokens + seg1["tokens"]
    seg2 = client.generate(continuation_prompt, args.n_tokens - k, args.seed)
    trial.segment2 = seg2

    concat = seg1["tokens"] + seg2["tokens"]
    trial.concat_sha256 = sha256_ints(concat)
    trial.comparison = compare_tokens(baseline["tokens"], concat)
    trial.passed = bool(trial.comparison["equal"] and trial.precondition_ok)

    # restore 是否真的生效：cache_n 应接近 save 的 n_saved（差 1 是那条尚未进 KV 的末 token）
    n_saved = (trial.save or {}).get("body", {}).get("n_saved")
    cache_n = seg2.get("cache_n")
    if n_saved is not None and cache_n is not None:
        trial.notes.append(f"restore 生效性：save n_saved={n_saved}，段2 cache_n={cache_n}（差 {n_saved - cache_n}）")
        if cache_n < max(1, n_saved - 2):
            trial.notes.append("⚠️ cache_n 明显小于 n_saved —— restore 可能没真正生效，先查这一步再谈一致性")
    return trial


def run_ctx_probe(
    server: ServerHandle,
    client: SlotClient,
    args: argparse.Namespace,
    baseline: dict,
    prompt_tokens: list[int],
    alt_ctx_values: list[int],
) -> dict:
    """T-02 移交的未验证前提：save / restore 两端的 `-c` 不一致会怎样？

    直觉上的假设是「必须完全一致」。但真正可能起作用的约束也许只是
    「恢复时 `n_saved ≤ n_ctx`」—— 这两种假设对 T-07 的键设计影响完全不同：
    前者要把 `n_ctx` 计入键，后者不用。故必须实测。
    """
    probe: dict = {"save_ctx": args.ctx, "cases": []}
    k = min(args.n_tokens // 2, 256)
    filename = "ac1-ctxprobe.bin"

    # 阶段 1：在 args.ctx 下生成并 save
    server.ctx = args.ctx
    server.start()
    if not client.health():
        raise RuntimeError("/health 不通（ctx 探针 阶段1）")
    seg1 = client.generate(args.prompt, k, args.seed)
    save = client.slot_action("save", filename)
    server.stop()
    n_saved = save.get("body", {}).get("n_saved")
    probe["segment1"] = seg1
    probe["save"] = save
    probe["n_saved"] = n_saved

    for alt_ctx in alt_ctx_values:
        case: dict = {"restore_ctx": alt_ctx, "n_saved": n_saved, "fits": n_saved is not None and n_saved <= alt_ctx}
        try:
            server.ctx = alt_ctx
            server.start()
            if not client.health():
                raise RuntimeError("/health 不通")
            case["restore"] = client.slot_action("restore", filename)
            if case["restore"]["ok"]:
                # restore 声称成功 —— 追一刀：续生成是否仍与基线一致。
                # 尾部刻意取短（默认 64）：alt_ctx 很小的时候，生成太长会触发上下文滚动，
                # 那样分叉的原因是「装不下」而不是 restore 本身，结论就没法解释了。
                tail = min(64, args.n_tokens - k)
                seg2 = client.generate(prompt_tokens + seg1["tokens"], tail, args.seed)
                case["segment2"] = seg2
                concat = seg1["tokens"] + seg2["tokens"]
                case["comparison"] = compare_tokens(baseline["tokens"][: len(concat)], concat)
            server.stop()
        except Exception as exc:  # noqa: BLE001 —— 起不来也是有效结论
            case["error"] = f"{type(exc).__name__}: {exc}"
            server.stop()
        probe["cases"].append(case)
        verdict = (
            "restore 失败"
            if not case.get("restore", {}).get("ok")
            else ("续生成一致" if case.get("comparison", {}).get("equal") else "续生成**不一致**")
        )
        print(f"[ctx 探针] restore 端 -c={alt_ctx}（n_saved={n_saved}，装得下={case['fits']}）→ {verdict}")

    server.ctx = args.ctx
    return probe


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="T-03 · 跨进程续生成逐字比对（AC1）")
    parser.add_argument("--llama-bin", default=LLAMA_BIN_DEFAULT)
    parser.add_argument("--model", default=MODEL_DEFAULT)
    parser.add_argument("--port", type=int, default=8083)
    parser.add_argument("--ctx", type=int, default=4096)
    parser.add_argument("--ngl", type=int, default=99)
    parser.add_argument("--kv-dir", default="kv")
    parser.add_argument("--prompt", default=PROMPT_DEFAULT)
    parser.add_argument("--n-tokens", type=int, default=512, help="基线总长度 N")
    parser.add_argument("--ks", default="256,511", help="分段点 K，逗号分隔（票面要求至少两个点）")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--report", default="reports/ac1-exact-match.json")
    parser.add_argument("--log-dir", default="reports/logs")
    parser.add_argument(
        "--ctx-probe",
        default="512,128",
        help="额外探测 restore 端换 -c 的影响（逗号分隔，空串关闭）。默认 512 装得下 n_saved、128 装不下",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    ks = [int(x) for x in args.ks.split(",") if x.strip()]
    for k in ks:
        if not 0 < k < args.n_tokens:
            print(f"[参数错误] K={k} 必须落在 (0, N={args.n_tokens}) 内")
            return 2

    spike_root = Path(__file__).resolve().parent.parent
    slot_dir = (spike_root / args.kv_dir).resolve()
    slot_dir.mkdir(parents=True, exist_ok=True)
    log_dir = (spike_root / args.log_dir).resolve()

    server = ServerHandle(
        bin_dir=Path(args.llama_bin),
        model=Path(args.model),
        port=args.port,
        ctx=args.ctx,
        ngl=args.ngl,
        slot_dir=slot_dir,
        log_dir=log_dir,
    )
    client = SlotClient(f"http://127.0.0.1:{args.port}")

    report: dict = {
        "ticket": "T-03",
        "ac": "AC1",
        "params": {
            "model": args.model,
            "llama_bin": args.llama_bin,
            "ctx": args.ctx,
            "ngl": args.ngl,
            "n_tokens": args.n_tokens,
            "ks": ks,
            "seed": args.seed,
            "temperature": 0.0,
            "cache_prompt": True,
            "prompt": args.prompt,
            "slot_dir": str(slot_dir),
        },
        "process_lifecycle": [],
    }

    trials: list[Trial] = []
    baseline: dict | None = None
    prompt_tokens: list[int] = []
    error: str | None = None

    try:
        # ---------- 基线：全新进程，一次不中断生成 N 个 ----------
        print(f"[基线] 起服务 port={args.port} ctx={args.ctx} ...")
        server.start()
        if not client.health():
            raise RuntimeError("/health 不通")
        prompt_tokens = client.tokenize(args.prompt)
        report["prompt_tokens"] = prompt_tokens
        print(f"[基线] prompt = {len(prompt_tokens)} token，生成 N={args.n_tokens} ...")
        baseline = client.generate(args.prompt, args.n_tokens, args.seed)
        baseline["sha256"] = sha256_ints(baseline["tokens"])
        baseline["len"] = len(baseline["tokens"])
        report["baseline"] = baseline
        print(f"[基线] 完成：{baseline['len']} token，sha256={baseline['sha256'][:16]}…")
        server.stop()

        # ---------- 各 K 值分段试验 ----------
        for k in ks:
            print(f"\n[K={k}] 分段试验 ...")
            server.start()
            if not client.health():
                raise RuntimeError("/health 不通")
            trial = run_trial(server, client, args, k, baseline, prompt_tokens)
            server.stop()
            trials.append(trial)
            print(f"[K={k}] {'✅ 一致' if trial.passed else '❌ 不一致'}")
            for note in trial.notes:
                print(f"        {note}")
            if trial.comparison and not trial.comparison["equal"]:
                print(f"        首个分歧位置 = {trial.comparison['first_divergence']}")

        # ---------- 附加：ctx 不匹配探针（T-02 移交的前提条件） ----------
        alt_ctx_values = [int(x) for x in args.ctx_probe.split(",") if x.strip()]
        if alt_ctx_values:
            print(f"\n[ctx 探针] save 端 -c={args.ctx}，逐一看 restore 端换 -c 的后果 ...")
            report["ctx_probe"] = run_ctx_probe(server, client, args, baseline, prompt_tokens, alt_ctx_values)
    except Exception as exc:  # noqa: BLE001 —— 任何异常都要落盘现场，否则白跑
        error = f"{type(exc).__name__}: {exc}"
        print(f"[异常] {error}")
    finally:
        client.close()
        server.stop()
        report["process_lifecycle"] = process_lifecycle(server.records)

    report["trials"] = [asdict(t) for t in trials]
    report["error"] = error
    ctx_probe = report.get("ctx_probe")
    report["conclusion"] = {
        "ac1_pass": error is None and len(trials) == len(ks) and all(t.passed for t in trials),
        "trials_requested": len(ks),
        "trials_run": len(trials),
        "trials_passed": sum(1 for t in trials if t.passed),
        "baseline_sha256": baseline["sha256"] if baseline else None,
        "ks": ks,
        "n_tokens": args.n_tokens,
    }
    if ctx_probe:
        report["conclusion"]["ctx_probe"] = [
            {
                "restore_ctx": case["restore_ctx"],
                "fits": case["fits"],
                "restore_ok": case.get("restore", {}).get("ok"),
                "continuation_equal": case.get("comparison", {}).get("equal"),
                "error": case.get("error"),
            }
            for case in ctx_probe["cases"]
        ]

    report_path = spike_root / args.report
    write_report(report_path, report)

    print("\n=== 汇总 ===")
    if baseline:
        print(f"  基线 N={baseline['len']} token  sha256={baseline['sha256']}")
    for trial in trials:
        verdict = "一致" if trial.passed else "不一致"
        cmp_ = trial.comparison or {}
        print(
            f"  K={trial.k}: {verdict}"
            f"  基线={cmp_.get('expected_len')} 分段={cmp_.get('actual_len')} 共同前缀={cmp_.get('common_prefix_len')}"
        )
        print(f"        基线段 sha256 = {cmp_.get('expected_sha256')}")
        print(f"        分段拼 sha256 = {cmp_.get('actual_sha256')}")
        if cmp_.get("first_divergence") is not None:
            print(f"        首个分歧位置 = {cmp_['first_divergence']}")
            print(f"          基线窗口 = {cmp_.get('expected_window')}")
            print(f"          分段窗口 = {cmp_.get('actual_window')}")
    if ctx_probe:
        print("  --- ctx 不匹配探针（诊断，不计入 AC1） ---")
        for case in ctx_probe["cases"]:
            print(
                f"    restore 端 -c={case['restore_ctx']}（n_saved={ctx_probe['n_saved']}，装得下={case['fits']}）："
                f" restore_ok={case.get('restore', {}).get('ok')}"
                f" 续生成一致={case.get('comparison', {}).get('equal')}"
                + (f" error={case['error']}" if case.get("error") else "")
            )
    print(f"  AC1 = {'通过' if report['conclusion']['ac1_pass'] else '未通过'}" + (f"（{error}）" if error else ""))
    print(f"报告已写入 {report_path}")

    if error:
        return 2
    return 0 if report["conclusion"]["ac1_pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
