#!/usr/bin/env python
"""T-04 · restore 提速与 KV 体积量化（AC2/AC3）。

两组数字，都要求可复现、可追溯：

- **AC2**：restore 比冷 prefill 快几倍（4K token 量级要求 ≥5×）
- **AC3**：`.bin` 字节数对 token 数**线性可预测**（R²>0.95、预测误差<20%）

做法（每档每个重复独立起进程，保证「冷」是真的冷）
--------------------------------------------------
1. 起进程 → 送 **L 个 token 的数组 prompt**、`n_predict=1` → `prompt_ms` 即**冷 prefill**
2. 记显存 → `save` → 记 `n_saved` / `n_written`
3. **杀进程**重启 → `restore` → `restore_ms`
4. 追一刀「暖 prefill」：重发同一 prompt，`cache_n` 应≈`n_saved`、`prompt_ms` 应≈0
   （证明 restore 的 KV 真的可用，而不只是返回了 200）

`bytes/token` 不只报实测值，还用 **GGUF 元数据**按架构公式反算理论值并解释偏差 ——
票面明确要求「与 §9.2 估算对照，偏差有解释」，不能只甩一个数字。

用法
----
    python scripts/04_bench.py                                   # 1K/2K/4K/8K × 3 次
    python scripts/04_bench.py --lengths 1024,4096 --repeats 1   # 快速自检
"""

from __future__ import annotations

import argparse
import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from kvbench_common import (
    LLAMA_BIN_DEFAULT,
    MODEL_DEFAULT,
    ServerHandle,
    SlotClient,
    build_prompt_tokens,
    gpu_memory_mib,
    kv_elements_per_token,
    linear_fit,
    process_lifecycle,
    read_gguf_metadata,
    sha256_ints,
    write_report,
)

SEED_TEXT = "The history of France is long and complex, spanning many centuries of change. "


@dataclass
class LengthResult:
    length: int
    filename: str
    samples: list[dict] = field(default_factory=list)
    medians: dict = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


def run_one(
    server: ServerHandle,
    client: SlotClient,
    args: argparse.Namespace,
    length: int,
    repeat: int,
    prompt_tokens: list[int],
) -> dict:
    """一次完整测量：冷 prefill → save → 杀进程 → restore → 暖 prefill。"""
    sample: dict = {"repeat": repeat, "length_requested": length}
    filename = f"bench-L{length}.bin"

    # --- 冷启动进程 ---
    server.start()
    if not client.health():
        raise RuntimeError("/health 不通")
    sample["gpu_after_load"] = gpu_memory_mib()

    # --- 1. 冷 prefill ---
    cold = client.generate(prompt_tokens, 1, args.seed)
    sample["cold_prefill"] = {
        "prompt_ms": cold["prompt_ms"],
        "cache_n": cold["cache_n"],
        "tokens_evaluated": cold["tokens_evaluated"],
        "predicted_ms": cold["predicted_ms"],
    }
    sample["gpu_after_prefill"] = gpu_memory_mib()
    prompt_sha = sha256_ints(prompt_tokens)
    sample["prompt_sha256"] = prompt_sha
    sample["prompt_len"] = len(prompt_tokens)

    # --- 2. save ---
    save = client.slot_action("save", filename)
    sample["save"] = save
    if not save["ok"]:
        raise RuntimeError(f"save 失败：{save}")
    n_saved = save["body"]["n_saved"]
    n_written = save["body"]["n_written"]
    sample["n_saved"] = n_saved
    sample["n_written"] = n_written
    sample["bytes_per_token"] = n_written / n_saved if n_saved else None

    # --- 3. 杀进程 → 重启 → restore ---
    killed = server.stop()
    sample["killed_pid"] = killed.pid if killed else None
    sample["killed_returncode"] = killed.returncode if killed else None
    server.start()
    sample["restart_pid"] = server.record.pid if server.record else None
    restore = client.slot_action("restore", filename)
    sample["restore"] = restore
    sample["restore_ms"] = restore["body"].get("timings", {}).get("restore_ms") if restore["ok"] else None
    sample["n_restored"] = restore["body"].get("n_restored") if restore["ok"] else None
    sample["n_read"] = restore["body"].get("n_read") if restore["ok"] else None

    # --- 4. 暖 prefill：证明 restore 的 KV 真的能用 ---
    warm = client.generate(prompt_tokens, 1, args.seed)
    sample["warm_prefill"] = {
        "prompt_ms": warm["prompt_ms"],
        "cache_n": warm["cache_n"],
        "tokens_evaluated": warm["tokens_evaluated"],
    }

    # 判定：restore 是否真的生效
    checks = []
    if not restore["ok"]:
        checks.append("restore 未返回 200")
    if sample["n_read"] != n_written:
        checks.append(f"n_read({sample['n_read']}) != n_written({n_written})")
    if warm["cache_n"] is None or n_saved is None or warm["cache_n"] < n_saved - 2:
        checks.append(f"暖 prefill cache_n({warm['cache_n']}) 明显小于 n_saved({n_saved}) —— restore 可能没生效")
    sample["checks_passed"] = not checks
    sample["check_failures"] = checks

    server.stop()
    return sample


def summarize(samples: list[dict]) -> dict:
    """中位数口径 —— AC2 判据就是这么定的。"""
    cold = [s["cold_prefill"]["prompt_ms"] for s in samples]
    restore = [s["restore_ms"] for s in samples if s["restore_ms"] is not None]
    warm = [s["warm_prefill"]["prompt_ms"] for s in samples]

    def med(values: list[float]) -> float | None:
        return round(statistics.median(values), 3) if values else None

    cold_med, restore_med, warm_med = med(cold), med(restore), med(warm)
    speedup = (cold_med / restore_med) if (cold_med and restore_med) else None
    warm_speedup = (cold_med / warm_med) if (cold_med and warm_med) else None
    return {
        "cold_prefill_ms_samples": cold,
        "restore_ms_samples": restore,
        "warm_prefill_ms_samples": warm,
        "cold_prefill_ms_median": cold_med,
        "restore_ms_median": restore_med,
        "warm_prefill_ms_median": warm_med,
        "speedup_vs_cold_prefill": round(speedup, 2) if speedup else None,
        "speedup_warm_vs_cold": round(warm_speedup, 2) if warm_speedup else None,
        "all_checks_passed": all(s["checks_passed"] for s in samples),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="T-04 · AC2/AC3 采样")
    parser.add_argument("--llama-bin", default=LLAMA_BIN_DEFAULT)
    parser.add_argument("--model", default=MODEL_DEFAULT)
    parser.add_argument("--port", type=int, default=8084)
    parser.add_argument(
        "--ctx",
        type=int,
        default=16384,
        help="要装得下最大档的长度（+1 生成 token）。显存不够就缩档，报告里必须写明缩到哪一档",
    )
    parser.add_argument("--ngl", type=int, default=99)
    parser.add_argument("--kv-dir", default="kv")
    parser.add_argument("--lengths", default="1024,2048,4096,8192")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--report", default="reports/ac2-ac3-measurements.json")
    parser.add_argument("--log-dir", default="reports/logs")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    lengths = [int(x) for x in args.lengths.split(",") if x.strip()]
    lengths = [v for v in lengths if v + 1 < args.ctx]
    if not lengths:
        print(f"[参数错误] 没有可比的长度档：ctx={args.ctx} 装不下任何一档")
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
        tag="bench",
    )
    client = SlotClient(server.base_url)

    report: dict = {
        "ticket": "T-04",
        "acs": ["AC2", "AC3"],
        "params": {
            "model": args.model,
            "llama_bin": args.llama_bin,
            "ctx": args.ctx,
            "ngl": args.ngl,
            "lengths": lengths,
            "repeats": args.repeats,
            "seed": args.seed,
            "temperature": 0.0,
            "n_predict_per_measure": 1,
            "seed_text": SEED_TEXT,
            "slot_dir": str(slot_dir),
        },
        "gpu_idle": gpu_memory_mib(),
    }

    # 模型结构与理论 bytes/token（不靠印象，直接读 GGUF 头）
    try:
        meta = read_gguf_metadata(Path(args.model))
        report["model_metadata"] = {k: v for k, v in meta.items() if not k.startswith("_")}
        report["model_metadata_raw"] = meta
        report["kv_theory"] = kv_elements_per_token(meta)
    except Exception as exc:  # noqa: BLE001 —— 拿不到理论值不该让整个采样失败
        report["model_metadata_error"] = f"{type(exc).__name__}: {exc}"

    results: list[LengthResult] = []
    error: str | None = None

    try:
        # tokenize 需要服务在跑 —— 先起一次取词表，随后关掉。
        # prompt 用 **token 数组** 而不是文本：长度精确可控，prefill 的名义量与实际量才一致。
        print("[准备] 起一次服务取词表 ...")
        server.start()
        if not client.health():
            raise RuntimeError("/health 不通（取词表阶段）")
        prompt_by_length: dict[int, list[int]] = {}
        for length in lengths:
            prompt_by_length[length] = build_prompt_tokens(client, length, SEED_TEXT)
            print(f"        L={length:>5} → prompt {len(prompt_by_length[length])} token")
        server.stop()
        report["prompt_lengths"] = {str(k): len(v) for k, v in prompt_by_length.items()}

        for length in lengths:
            result = LengthResult(length=length, filename=f"bench-L{length}.bin")
            prompt_tokens = prompt_by_length[length]
            if len(prompt_tokens) != length:
                result.notes.append(f"prompt 实际 {len(prompt_tokens)} token（请求 {length}）")
            for repeat in range(1, args.repeats + 1):
                started = time.time()
                sample = run_one(server, client, args, length, repeat, prompt_tokens)
                sample["wall_seconds"] = round(time.time() - started, 2)
                result.samples.append(sample)
                if not sample["checks_passed"]:
                    result.notes.append(f"repeat{repeat} 校验未过：{sample['check_failures']}")
                gpu_used = sample.get("gpu_after_prefill") or {}
                print(
                    f"[L={length:>5} r{repeat}] cold_prefill={sample['cold_prefill']['prompt_ms']:>9.1f}ms"
                    f"  restore={sample['restore_ms'] if sample['restore_ms'] is not None else float('nan'):>8.2f}ms"
                    f"  warm={sample['warm_prefill']['prompt_ms']:>7.2f}ms"
                    f"  n_saved={sample['n_saved']:>5}  bytes={sample['n_written']:>10}"
                    f"  gpu={gpu_used.get('used_mib', '?')}MiB"
                )
            result.medians = summarize(result.samples)
            results.append(result)
    except Exception as exc:  # noqa: BLE001 —— 异常也要落盘现场，别白跑
        error = f"{type(exc).__name__}: {exc}"
        print(f"[异常] {error}")
    finally:
        client.close()
        server.stop()
        report["process_lifecycle"] = process_lifecycle(server.records)

    report["results"] = [
        {
            "length": r.length,
            "filename": r.filename,
            "samples": r.samples,
            "medians": r.medians,
            "notes": r.notes,
        }
        for r in results
    ]
    report["error"] = error

    # ---------- AC3：体积线性拟合 ----------
    fit_points = []
    for r in results:
        for s in r.samples:
            fit_points.append((s["n_saved"], s["n_written"]))
    # 同一 token 数会重复测量，去重后拟合（重复点的字节数完全相同，留着只会虚高样本量）
    dedup: dict[int, int] = {}
    for tokens, nbytes in fit_points:
        dedup[tokens] = nbytes
    xs = sorted(dedup)
    fit = linear_fit([float(x) for x in xs], [float(dedup[x]) for x in xs]) if len(xs) >= 2 else {"ok": False}
    report["ac3_fit"] = fit

    # ---------- AC2 汇总 ----------
    ac2_rows = []
    for r in results:
        med = r.medians
        if med.get("speedup_vs_cold_prefill") is None:
            continue
        ac2_rows.append(
            {
                "length": r.length,
                "cold_prefill_ms_median": med["cold_prefill_ms_median"],
                "restore_ms_median": med["restore_ms_median"],
                "speedup": med["speedup_vs_cold_prefill"],
                "warm_prefill_ms_median": med["warm_prefill_ms_median"],
                "speedup_warm_vs_cold": med["speedup_warm_vs_cold"],
            }
        )
    report["ac2_rows"] = ac2_rows

    # 4K 量级单独对照 AC2 判据 ≥5×（取最接近 4096 的档）
    target = min(ac2_rows, key=lambda row: abs(row["length"] - 4096)) if ac2_rows else None
    ac2_pass = bool(target and target["speedup"] is not None and target["speedup"] >= 5.0)
    theory = report.get("kv_theory", {})
    measured_bpt = statistics.median(
        [row["bytes_per_token"] for r in results for row in r.samples if row["bytes_per_token"]]
    )
    theory_bpt = theory.get("bytes_per_token_f16")
    deviation = None
    if theory_bpt and measured_bpt:
        deviation = {
            "measured_bytes_per_token": round(measured_bpt, 1),
            "theory_bytes_per_token": theory_bpt,
            "abs_diff": round(measured_bpt - theory_bpt, 1),
            "rel_diff_pct": round((measured_bpt - theory_bpt) / theory_bpt * 100, 3),
        }

    report["conclusion"] = {
        "ac2_target_length": target["length"] if target else None,
        "ac2_speedup": target["speedup"] if target else None,
        "ac2_threshold": 5.0,
        "ac2_pass": ac2_pass,
        "ac3_r2": fit.get("r2") if fit.get("ok") else None,
        "ac3_max_rel_error_pct": fit.get("max_rel_error_pct") if fit.get("ok") else None,
        "ac3_r2_threshold": 0.95,
        "ac3_error_threshold_pct": 20.0,
        "ac3_pass": bool(
            fit.get("ok")
            and fit["r2"] > 0.95
            and fit.get("max_rel_error_pct") is not None
            and fit["max_rel_error_pct"] < 20.0
        ),
        "bytes_per_token": deviation,
        "measured_bytes_per_token_median": round(measured_bpt, 1),
        "all_sample_checks_passed": all(r.medians.get("all_checks_passed") for r in results),
        "error": error,
    }

    report_path = spike_root / args.report
    write_report(report_path, report)

    print("\n=== AC2（中位数口径，每档 %d 次）===" % args.repeats)
    print(f"{'长度':>8} {'冷 prefill(ms)':>16} {'restore(ms)':>13} {'提速':>8} {'暖 prefill(ms)':>15} {'暖/冷':>8}")
    for row in ac2_rows:
        print(
            f"{row['length']:>8} {row['cold_prefill_ms_median']:>16.1f} {row['restore_ms_median']:>13.2f}"
            f" {row['speedup']:>7.2f}× {row['warm_prefill_ms_median']:>15.2f} {row['speedup_warm_vs_cold']:>7.1f}×"
        )
    print(
        f"  AC2（{report['conclusion']['ac2_target_length']} 档，阈值 5×）= "
        f"{'通过' if ac2_pass else '未通过'}（实测 {report['conclusion']['ac2_speedup']}×）"
    )

    print("\n=== AC3（体积线性）===")
    if fit.get("ok"):
        print(f"  {fit['formula']}")
        print(f"  R² = {fit['r2']:.6f}   最大预测误差 = {fit['max_rel_error_pct']:.4f}%")
    if deviation:
        print(
            "  实测 bytes/token = %.1f ；GGUF 理论 = %d（%s）；偏差 %+.1f B（%+.3f%%）"
            % (
                deviation["measured_bytes_per_token"],
                deviation["theory_bytes_per_token"],
                theory.get("formula", "?"),
                deviation["abs_diff"],
                deviation["rel_diff_pct"],
            )
        )
    print(f"  AC3（R²>0.95 且误差<20%）= {'通过' if report['conclusion']['ac3_pass'] else '未通过'}")
    print(f"报告已写入 {report_path}")

    if error:
        return 2
    ok = report["conclusion"]["ac2_pass"] and report["conclusion"]["ac3_pass"]
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
