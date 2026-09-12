#!/usr/bin/env python3
"""llama-server slot API 冒烟 + 501 基线（T-02）。

三个模式：

| 模式 | 用途 |
| :--- | :--- |
| `smoke` | 正常服务上走一遍：health → /slots → 预热生成 → save → 校验文件 → restore |
| `restore-only` | **Q2 探针**：服务刚起（slot 全空）时直接 restore，验证「空闲 slot 能否被恢复」 |
| `expect501` | **REQ-E3 基线**：服务未配 `--slot-save-path` 时，save/restore 必须返回 501，且与 500/404 可区分 |

为什么用 Python 而不是 curl：本沙箱下 curl 的 `-o` 写文件会以 exit 23（write error）失败，
而 200 响应体也需要结构化留存（findings 要求附原始片段）。
另外 httpx 已是本目录的既定依赖，不引入新东西。

用法::

    python scripts/slot_smoke.py smoke --kv-dir kv --report reports/t02-smoke.json
    python scripts/slot_smoke.py restore-only --filename smoke.bin
    python scripts/slot_smoke.py expect501 --base-url http://127.0.0.1:8081
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import httpx

# --------------------------------------------------------------------------------------
# 状态码分类 —— T-02 验收第 2 条要求「脚本能把 501 与 500/404 区分开」
# --------------------------------------------------------------------------------------

STATUS_OK = "OK"
STATUS_UNAVAILABLE = "SLOT_API_UNAVAILABLE"  # 501：服务端没配 --slot-save-path（REQ-E3）
STATUS_NOT_FOUND = "NOT_FOUND"  # 404
STATUS_BAD_REQUEST = "BAD_REQUEST"  # 400
STATUS_SERVER_ERROR = "SERVER_ERROR"  # 5xx（501 已单独归类）
STATUS_OTHER = "OTHER"


def classify(status: int) -> str:
    if 200 <= status < 300:
        return STATUS_OK
    if status == 501:
        return STATUS_UNAVAILABLE
    if status == 404:
        return STATUS_NOT_FOUND
    if status == 400:
        return STATUS_BAD_REQUEST
    if 500 <= status < 600:
        return STATUS_SERVER_ERROR
    return STATUS_OTHER


@dataclass
class Probe:
    """一次 HTTP 探测的完整现场 —— findings 里的「原始响应片段」就取自这里。"""

    label: str
    method: str
    url: str
    status: int | None = None
    classification: str = STATUS_OTHER
    body: str = ""
    elapsed_ms: float | None = None
    error: str | None = None

    def note(self) -> str:
        if self.error:
            return f"{self.label}: 请求异常 {self.error}"
        return f"{self.label}: HTTP {self.status} [{self.classification}] {self.body[:200]}"


@dataclass
class Report:
    mode: str
    base_url: str
    slot_dir: str | None = None
    filename: str = "smoke.bin"
    probes: list[Probe] = field(default_factory=list)
    slots_before: Any = None
    slots_after: Any = None
    file_exists: bool | None = None
    file_bytes: int | None = None
    failures: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["probes"] = [asdict(p) for p in self.probes]
        return payload


# --------------------------------------------------------------------------------------
# 探测原语
# --------------------------------------------------------------------------------------


class Client:
    """llama-server 的 HTTP 客户端。

    ⚠️ **`trust_env=False` 是必须的，不是可选优化**（T-02 实测踩到）：

    本机环境注入了 `HTTP_PROXY=http://127.0.0.1:63791`（沙箱代理）。httpx 默认
    `trust_env=True`，于是**连 127.0.0.1 也被丢给代理**，而代理在 keep-alive 复用连接上
    会出现帧错位 —— 表现为同一持久连接上「**200 / 404 / 200 / 404 交替**」：

        trust_env=True : [200, 404, 200, 404, 200]
        trust_env=False: [200, 200, 200, 200, 200]

    这个症状极具误导性：看起来像 llama.cpp 的 slot API 时好时坏，实际是代理层的事。
    上游 `/health`、`/props`、`/slots` 都会被影响。T-06 的 Engine Adapter 必须照此处理。
    """

    def __init__(self, base_url: str, timeout: float = 60.0) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(base_url=self.base_url, timeout=timeout, trust_env=False)

    def close(self) -> None:
        self._client.close()

    def call(self, label: str, method: str, path: str, **kwargs: Any) -> tuple[Probe, httpx.Response | None]:
        url = f"{self.base_url}{path}"
        probe = Probe(label=label, method=method, url=url)
        started = time.perf_counter()
        try:
            response = self._client.request(method, path, **kwargs)
        except Exception as exc:  # 网络/超时/连接被拒都记现场，不让脚本崩
            probe.error = f"{type(exc).__name__}: {exc}"
            probe.elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
            return probe, None
        probe.elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
        probe.status = response.status_code
        probe.classification = classify(response.status_code)
        probe.body = response.text[:600]
        return probe, response

    def get(self, label: str, path: str) -> tuple[Probe, httpx.Response | None]:
        return self.call(label, "GET", path)

    def post(self, label: str, path: str, **kwargs: Any) -> tuple[Probe, httpx.Response | None]:
        return self.call(label, "POST", path, **kwargs)


def file_info(path: Path) -> tuple[bool, int | None]:
    if not path.exists():
        return False, None
    return True, path.stat().st_size


def file_mtime(path: Path) -> float | None:
    """取 mtime（纳秒精度），用于判定「同名重复 save 是否真的重写了文件」。

    只比字节数是**不够**的：字节数相同既可能是覆盖写，也可能是服务端直接跳过。
    """
    if not path.exists():
        return None
    return path.stat().st_mtime_ns


# --------------------------------------------------------------------------------------
# 模式实现
# --------------------------------------------------------------------------------------


def mode_smoke(args: argparse.Namespace, report: Report) -> None:
    client = Client(args.base_url, timeout=args.timeout)
    slot_dir = Path(args.kv_dir).resolve() if args.kv_dir else None
    if slot_dir:
        report.slot_dir = str(slot_dir)
        slot_dir.mkdir(parents=True, exist_ok=True)
    target = (slot_dir / report.filename) if slot_dir else None

    try:
        # 1. 探活
        probe, _ = client.get("health", "/health")
        report.probes.append(probe)
        if probe.classification != STATUS_OK:
            report.failures.append(f"health 不是 2xx：{probe.classification}")

        # 2. Q1：/slots 结构
        probe, response = client.get("slots(before)", "/slots")
        report.probes.append(probe)
        slots = response.json() if response is not None and response.status_code == 200 else None
        report.slots_before = slots
        slot_ids = [s.get("id") for s in slots] if isinstance(slots, list) else []
        report.notes.append(f"GET /slots 返回 {len(slot_ids)} 个槽位，id={slot_ids}")

        slot_id = args.slot_id
        if slot_id not in slot_ids:
            report.failures.append(f"目标 slot_id={slot_id} 不在 /slots 返回的 {slot_ids} 中")
            return

        # 3. 预热：往该 slot 灌一段上下文，让它有 KV 可存
        prompt = args.prompt
        body = {
            "prompt": prompt,
            "n_predict": args.n_predict,
            "id_slot": slot_id,
            "temperature": 0,
            "seed": 42,
            "cache_prompt": True,
        }
        probe, response = client.post("completion(预热)", "/completion", json=body)
        report.probes.append(probe)
        if probe.classification != STATUS_OK:
            report.failures.append(f"预热生成失败：{probe.classification} {probe.body[:200]}")
            return
        if response is not None:
            payload = response.json()
            report.notes.append(
                "预热返回 tokens_predicted={} tokens_evaluated={} slot_id={} stop_type={}".format(
                    payload.get("tokens_predicted"),
                    payload.get("tokens_evaluated"),
                    payload.get("slot_id"),
                    (payload.get("stop_type") or {}).get("value")
                    if isinstance(payload.get("stop_type"), dict)
                    else payload.get("stop_type"),
                )
            )

        # 4. Q1 追问：灌完再读 /slots，看 id 是否随请求变化、槽位里多了什么字段
        probe, response = client.get("slots(after)", "/slots")
        report.probes.append(probe)
        slots_after = response.json() if response is not None and response.status_code == 200 else None
        report.slots_after = slots_after
        if isinstance(slots_after, list):
            ids_after = [s.get("id") for s in slots_after]
            report.notes.append(
                f"预热后 GET /slots 槽位 id={ids_after}（与请求前{'一致' if ids_after == slot_ids else '不同'}）"
            )
            for entry in slots_after:
                n_past = entry.get("n_past")
                report.notes.append(
                    f"  slot {entry.get('id')} n_past={n_past} is_processing={entry.get('is_processing')}"
                )

        # 5. save
        probe, _ = client.post(
            "slots/save",
            f"/slots/{slot_id}",
            params={"action": "save"},
            json={"filename": report.filename},
        )
        report.probes.append(probe)
        if probe.classification == STATUS_UNAVAILABLE:
            report.failures.append("save 返回 501：服务端未配置 --slot-save-path（本模式要求已配置）")
        elif probe.classification != STATUS_OK:
            report.failures.append(f"save 未成功：{probe.classification} {probe.body[:200]}")

        # 6. 校验落盘
        if target is not None:
            exists, size = file_info(target)
            report.file_exists = exists
            report.file_bytes = size
            if not exists:
                report.failures.append(f"save 返回 2xx 但文件不存在：{target}")
            else:
                report.notes.append(f"落盘文件 {target.name} = {size:,} bytes")

        # 7. restore
        probe, _ = client.post(
            "slots/restore",
            f"/slots/{slot_id}",
            params={"action": "restore"},
            json={"filename": report.filename},
        )
        report.probes.append(probe)
        if probe.classification != STATUS_OK:
            report.failures.append(f"restore 未成功：{probe.classification} {probe.body[:200]}")

        if args.probe_filename and slot_dir is not None:
            probe_filename_semantics(client, report, slot_id, slot_dir)

        if args.probe_edge:
            probe_edge_cases(client, report, slot_id, slot_dir)
    finally:
        client.close()


def probe_filename_semantics(client: Client, report: Report, slot_id: int, slot_dir: Path) -> None:
    """Q3：save 的 filename 是否只接受纯文件名、能否带子目录、是否覆盖。

    每组都记录落盘路径是否出现 —— 「能不能」由文件系统说话，不靠猜。
    """
    report.notes.append("— Q3 filename 语义探针 —")

    cases: list[tuple[str, str, Path]] = [
        ("反斜杠子目录", "sub\\nested.bin", slot_dir / "sub" / "nested.bin"),
        ("正斜杠子目录", "sub2/nested2.bin", slot_dir / "sub2" / "nested2.bin"),
        ("路径穿越", "../escape.bin", slot_dir.parent / "escape.bin"),
    ]
    for label, filename, expected in cases:
        probe, _ = client.post(
            f"save(filename={filename})",
            f"/slots/{slot_id}",
            params={"action": "save"},
            json={"filename": filename},
        )
        report.probes.append(probe)
        existed, size = file_info(expected)
        report.notes.append(
            f"  {label}: {filename!r} → HTTP {probe.status} [{probe.classification}]；"
            f"落盘到 {expected}：{'是' if existed else '否'}{f'（{size:,} B）' if existed else ''}"
        )

    # 覆盖语义：同名再存一次。只看字节数无法区分「覆盖写」与「直接跳过」，
    # 故记录 mtime 前后变化 —— mtime 变了才叫真的重写了。
    target_path = slot_dir / report.filename
    mtime_before = file_mtime(target_path)
    probe, _ = client.post(
        "save(覆盖探测)",
        f"/slots/{slot_id}",
        params={"action": "save"},
        json={"filename": report.filename},
    )
    report.probes.append(probe)
    mtime_after = file_mtime(target_path)
    if probe.classification != STATUS_OK:
        report.failures.append(f"同名重复 save 未成功：{probe.classification} {probe.body[:200]}")
    elif mtime_before is None or mtime_after is None:
        report.failures.append(f"覆盖探测前置条件不成立：{target_path} 不存在，无法比较 mtime")
    elif mtime_after == mtime_before:
        report.failures.append(
            f"同名重复 save 返回 200 但 {target_path.name} 的 mtime 未变（{mtime_before}） —— 无法证明是覆盖写"
        )
    report.notes.append(
        f"  同名重复 save → HTTP {probe.status} [{probe.classification}]；"
        f"mtime {'未变' if mtime_after == mtime_before else '已更新'}"
        f"（{mtime_before} → {mtime_after}）⇒ "
        + ("**覆盖写**成立" if mtime_after != mtime_before else "**覆盖写未证实**")
    )


def probe_edge_cases(client: Client, report: Report, slot_id: int, slot_dir: Path | None) -> None:
    """§6 的三条边界探针：缺 filename、越界 slot id、restore 不存在文件。

    这三条都是 T-06 错误码映射的直接依据，必须**可复现**（有原始状态码与响应体），
    不能只留在某次手工 curl 的记忆里。期望值以 findings §6 为准，不符即计入 failures。
    """
    report.notes.append("— §6 边界探针 —")

    # 6.1 缺 filename：服务端不校验 body，直接抛异常 → 500（不是 400）
    probe, _ = client.post(
        "save(body 缺 filename)",
        f"/slots/{slot_id}",
        params={"action": "save"},
        json={},
    )
    report.probes.append(probe)
    report.notes.append(
        f"  缺 filename → HTTP {probe.status} [{probe.classification}]"
        "（期望 500：服务端用 server_error 表达请求方 bug，Adapter 必须始终带 filename）"
    )
    if probe.status != 500:
        report.failures.append(f"缺 filename 期望 500，实际 {probe.status} [{probe.classification}]")

    # 6.2 越界 slot id：静默夹取到有效槽位 → 200（不能靠它兜底）
    probe, response = client.post(
        "save(slot_id=9999 越界)",
        "/slots/9999",
        params={"action": "save"},
        json={"filename": report.filename},
    )
    report.probes.append(probe)
    echoed = response.json().get("id_slot") if response is not None else None
    report.notes.append(
        f"  越界 slot id=9999 → HTTP {probe.status} [{probe.classification}]，"
        f"响应体回显 id_slot={echoed!r}"
        "（回显的是**请求里的 id 原样**，既不改写也不报错 —— 因此响应体无法证明它到底写到了哪个槽位，"
        "slot id 必须由我们自己从 /slots 取，绝不能把调用方任意 id 直接转发）"
    )

    # 6.3 restore 不存在的文件：400，且消息把「空间不足」与「文件无效」混在一句里
    ghost = "definitely-not-exists-slot.bin"
    ghost_path = (slot_dir / ghost) if slot_dir else None
    if ghost_path is not None and ghost_path.exists():
        report.failures.append(f"探针前置条件不成立：{ghost_path} 竟然存在，换名重跑")
    probe, _ = client.post(
        "restore(不存在的文件)",
        f"/slots/{slot_id}",
        params={"action": "restore"},
        json={"filename": ghost},
    )
    report.probes.append(probe)
    report.notes.append(
        f"  restore 不存在文件 {ghost!r} → HTTP {probe.status} [{probe.classification}]"
        "（期望 400：消息同时含「空间不足」与「文件无效」，无法据此判根因，"
        "T-09 只当 MISS(restore_failed)，不解析 message）"
    )
    if probe.status != 400:
        report.failures.append(f"restore 不存在文件期望 400，实际 {probe.status} [{probe.classification}]")


def mode_restore_only(args: argparse.Namespace, report: Report) -> None:
    """Q2 探针：服务**刚起**（所有 slot 空闲、无任何 KV）时，直接 restore 会怎样？

    这一条直接决定 T-06 的接口形态：若必须先「占位」再 restore，Engine Adapter 就要做两步。
    """
    client = Client(args.base_url, timeout=args.timeout)
    slot_dir = Path(args.kv_dir).resolve() if args.kv_dir else None
    if slot_dir:
        report.slot_dir = str(slot_dir)
    target = (slot_dir / report.filename) if slot_dir else None

    try:
        probe, _ = client.get("health", "/health")
        report.probes.append(probe)

        probe, response = client.get("slots(冷启动)", "/slots")
        report.probes.append(probe)
        if response is not None and response.status_code == 200:
            report.slots_before = response.json()
            report.notes.append(f"冷启动 /slots = {json.dumps(report.slots_before, ensure_ascii=False)}")

        if target is not None:
            exists, size = file_info(target)
            report.notes.append(f"待恢复文件 {target}：{'存在，' + f'{size:,} B' if exists else '不存在'}")
            if not exists:
                report.failures.append(f"待恢复文件不存在：{target}（先跑 smoke 模式产出它）")

        probe, _ = client.post(
            "restore(空闲 slot)",
            f"/slots/{args.slot_id}",
            params={"action": "restore"},
            json={"filename": report.filename},
        )
        report.probes.append(probe)

        # 再读一次槽位状态，看是否真被装进去了
        probe, response = client.get("slots(restore 后)", "/slots")
        report.probes.append(probe)
        if response is not None and response.status_code == 200:
            report.slots_after = response.json()
            report.notes.append(f"restore 后 /slots = {json.dumps(report.slots_after, ensure_ascii=False)}")
    finally:
        client.close()


def mode_expect501(args: argparse.Namespace, report: Report) -> None:
    """REQ-E3 基线：未配 --slot-save-path 时 save/restore 必须 501，且与 500/404 可区分。"""
    client = Client(args.base_url, timeout=args.timeout)
    try:
        probe, _ = client.get("health", "/health")
        report.probes.append(probe)
        if probe.classification != STATUS_OK:
            report.failures.append(f"health 不是 2xx：{probe.classification} —— 服务没起来？")

        for action in ("save", "restore"):
            probe, _ = client.post(
                f"slots/{action}(无 slot-save-path)",
                f"/slots/{args.slot_id}",
                params={"action": action},
                json={"filename": report.filename},
            )
            report.probes.append(probe)
            if probe.classification != STATUS_UNAVAILABLE:
                report.failures.append(
                    f"{action} 期望 501 实际 {probe.status} [{probe.classification}] —— 501 基线不成立"
                )

        # 顺带确认其它错误码不会被误判成 501
        probe, _ = client.post(
            "slots/不存在的action",
            f"/slots/{args.slot_id}",
            params={"action": "definitely-not-an-action"},
            json={"filename": report.filename},
        )
        report.probes.append(probe)
        report.notes.append(
            f"对照样本（非法 action）：HTTP {probe.status} [{probe.classification}]。"
            "实测：未配 `--slot-save-path` 时，**整个 /slots action 路由被禁用**，"
            "连非法 action 也返回 501；配了该 flag 后非法 action 才返回 400「Invalid action」。"
            "即 501 是**路由级**缺失，不是 action 级校验。"
        )

        probe, _ = client.get("slots/99999", "/slots/99999")
        report.probes.append(probe)
        report.notes.append(f"对照样本（不存在的 slot 路径）：HTTP {probe.status} [{probe.classification}]")
    finally:
        client.close()


# --------------------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="llama-server slot API 冒烟（T-02）")
    parser.add_argument("mode", choices=("smoke", "restore-only", "expect501"))
    parser.add_argument("--base-url", default="http://127.0.0.1:8080")
    parser.add_argument("--kv-dir", default="kv", help="slot 落盘目录（默认 spike 根下的 kv/）")
    parser.add_argument("--filename", default="smoke.bin")
    parser.add_argument("--slot-id", type=int, default=0)
    parser.add_argument("--prompt", default="The capital of France is")
    parser.add_argument("--n-predict", type=int, default=16)
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--report", default="", help="把完整现场写成 JSON")
    parser.add_argument("--probe-filename", action="store_true", help="smoke 模式下追加 Q3 filename 语义探针")
    parser.add_argument(
        "--probe-edge", action="store_true", help="smoke 模式下追加 §6 边界探针（缺 filename/越界 id/缺文件）"
    )
    args = parser.parse_args(argv)

    report = Report(mode=args.mode, base_url=args.base_url, filename=args.filename)

    handlers = {"smoke": mode_smoke, "restore-only": mode_restore_only, "expect501": mode_expect501}
    handlers[args.mode](args, report)

    print(f"=== slot_smoke · {args.mode} ===")
    print(f"base_url = {args.base_url}")
    for probe in report.probes:
        print("  " + probe.note())
    if report.notes:
        print("--- 观察 ---")
        for note in report.notes:
            print("  " + note)

    if args.report:
        out = Path(args.report)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"报告已写入 {out}")

    if report.failures:
        print("--- 失败项 ---")
        for failure in report.failures:
            print("  ✗ " + failure)
        print(f"结论：未通过（{len(report.failures)} 项）")
        return 1
    print("结论：通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
