#!/usr/bin/env python
"""kv-cache-resume 实验脚本的共用件。

被 `03_verify_exact.py`（AC1）与 `04_bench.py`（AC2/AC3）共用：
服务进程的生命周期管理（**能真杀**）、slot API 客户端、token 序列比对。

`SlotClient` 的 `trust_env=False` 是硬要求 —— 沙箱代理会让请求 200/404 交替
（T-02 结论 C6，见 `docs/phase0-slot-api-findings.md` §5）。
"""

from __future__ import annotations

import hashlib
import json
import socket
import struct
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import httpx

LLAMA_BIN_DEFAULT = r"C:\Tools\llama.cpp\bin"
MODEL_DEFAULT = r"C:\Tools\llama.cpp\models\qwen2.5-3b-instruct-q4_k_m.gguf"


# --------------------------------------------------------------------------------------
# 通用小工具
# --------------------------------------------------------------------------------------


def sha256_ints(values: list[int]) -> str:
    """对整数序列取 sha256（用十进制文本流，跨语言可复算）。"""
    payload = ",".join(str(v) for v in values).encode("ascii")
    return hashlib.sha256(payload).hexdigest()


def port_open(host: str, port: int, timeout: float = 0.4) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        return sock.connect_ex((host, port)) == 0


def wait_port_free(host: str, port: int, timeout: float = 30.0) -> bool:
    """等端口真正释放 —— 否则重启会 bind 失败，被误判成「机制不成立」。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not port_open(host, port):
            return True
        time.sleep(0.2)
    return not port_open(host, port)


def wait_health(base_url: str, timeout: float = 240.0) -> bool:
    """轮询 `/health` —— **不要用「端口可连」当就绪信号**。

    llama-server 会**先绑定端口再加载模型**，端口可连时模型可能还没读完，
    此时单发 `/health` 直接失败（实测踩过，见 `docs/phase0-ac1-report.md` §4.3）。
    """
    deadline = time.monotonic() + timeout
    with httpx.Client(timeout=5.0, trust_env=False) as client:
        while time.monotonic() < deadline:
            try:
                if client.get(f"{base_url}/health").status_code == 200:
                    return True
            except httpx.HTTPError:
                pass
            time.sleep(0.3)
    return False


def tail_text(path: Path, lines: int = 15) -> str:
    try:
        content = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return "(日志不可读)"
    return "\n".join("    " + line for line in content[-lines:])


def gpu_memory_mib() -> dict | None:
    """问 nvidia-smi 要显存占用。取不到就返回 None（不要编数字）。"""
    try:
        out = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,memory.total,memory.used",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0 or not out.stdout.strip():
        return None
    line = out.stdout.strip().splitlines()[0]
    parts = [p.strip() for p in line.split(",")]
    if len(parts) < 3:
        return None
    try:
        return {"name": parts[0], "total_mib": int(parts[1]), "used_mib": int(parts[2])}
    except ValueError:
        return None


# --------------------------------------------------------------------------------------
# GGUF 元数据 —— AC3 要「解释偏差」，就得知道真实的层数/头数，不能凭印象
# --------------------------------------------------------------------------------------

_GGUF_SCALAR = {
    0: ("<B", 1),
    1: ("<b", 1),
    2: ("<H", 2),
    3: ("<h", 2),
    4: ("<I", 4),
    5: ("<i", 4),
    6: ("<f", 4),
    7: ("<?", 1),
    10: ("<Q", 8),
    11: ("<q", 8),
    12: ("<d", 8),
}


def _gguf_read_value(handle, vtype: int):
    """读一个 GGUF 值。返回 (值, 是否可序列化)。"""
    if vtype == 8:  # STRING
        (length,) = struct.unpack("<Q", handle.read(8))
        return handle.read(length).decode("utf-8", errors="replace"), True
    if vtype == 9:  # ARRAY
        (elem_type,) = struct.unpack("<I", handle.read(4))
        (count,) = struct.unpack("<Q", handle.read(8))
        values = []
        for _ in range(count):
            item, _ = _gguf_read_value(handle, elem_type)
            values.append(item)
        return (values if len(values) <= 8 else f"<数组 {count} 项>"), len(values) <= 8
    if vtype in _GGUF_SCALAR:
        fmt, size = _GGUF_SCALAR[vtype]
        return struct.unpack(fmt, handle.read(size))[0], True
    raise ValueError(f"未知的 GGUF 值类型 {vtype}")


GGUF_KEYS_OF_INTEREST = (
    # 注意：GGUF 的键带架构前缀（如 `qwen2.attention.head_count_kv`），
    # 故这里用**短名**匹配 `key.rsplit(".", 1)[-1]`。曾因为写成带点的
    # `attention.head_count_kv` 而全部落空 —— 理论值算不出来，偏差就无从解释。
    "architecture",
    "name",
    "type",
    "file_type",
    "size_label",
    "block_count",
    "context_length",
    "embedding_length",
    "head_count",
    "head_count_kv",
    "key_length",
    "value_length",
)


def read_gguf_metadata(path: Path, keys_of_interest: tuple[str, ...] = GGUF_KEYS_OF_INTEREST) -> dict:
    """读 GGUF 头部元数据（只挑关心的键，但数组要完整跳过才能继续往后读）。"""
    wanted: dict = {}
    with path.open("rb") as handle:
        magic = handle.read(4)
        if magic != b"GGUF":
            raise ValueError(f"{path} 不是 GGUF（magic={magic!r}）")
        (version,) = struct.unpack("<I", handle.read(4))
        (tensor_count,) = struct.unpack("<Q", handle.read(8))
        (kv_count,) = struct.unpack("<Q", handle.read(8))
        wanted["_gguf_version"] = version
        wanted["_tensor_count"] = tensor_count
        for _ in range(kv_count):
            (key_len,) = struct.unpack("<Q", handle.read(8))
            key = handle.read(key_len).decode("utf-8", errors="replace")
            (vtype,) = struct.unpack("<I", handle.read(4))
            value, _ = _gguf_read_value(handle, vtype)
            short = key.rsplit(".", 1)[-1]
            if key in keys_of_interest or short in keys_of_interest:
                wanted[key] = value
    return wanted


def kv_elements_per_token(meta: dict) -> dict:
    """按架构公式算「每 token 每层 K+V 元素数」→ 每 token 字节数。

    `n_layer × (n_kv_head × key_length + n_kv_head × value_length)`
    """
    arch = meta.get("general.architecture", "qwen2")
    prefix = f"{arch}."

    def pick(name: str, default=None):
        return meta.get(f"{prefix}{name}", meta.get(name, default))

    n_layer = pick("block_count")
    n_kv_head = pick("attention.head_count_kv")
    head_k = pick("attention.key_length")
    head_v = pick("attention.value_length")
    head_count = pick("attention.head_count")
    n_embd = pick("embedding_length")
    if head_k is None or head_v is None:
        if head_count and n_embd:
            derived = n_embd // head_count
            head_k = head_k or derived
            head_v = head_v or derived
    if not all(isinstance(v, int) for v in (n_layer, n_kv_head, head_k, head_v)):
        return {"complete": False, "meta": meta}
    per_layer = n_kv_head * head_k + n_kv_head * head_v
    per_token_elements = n_layer * per_layer
    return {
        "complete": True,
        "architecture": arch,
        "n_layer": n_layer,
        "n_kv_head": n_kv_head,
        "head_k": head_k,
        "head_v": head_v,
        "elements_per_token": per_token_elements,
        "bytes_per_token_f16": per_token_elements * 2,
        "formula": f"{n_layer} 层 × ({n_kv_head} kv_head × ({head_k}+{head_v}) 维) × 2 字节 = {per_token_elements * 2} B/token",
    }


# --------------------------------------------------------------------------------------
# 服务进程管理 —— 「真杀」，不是假装重启
# --------------------------------------------------------------------------------------


@dataclass
class ProcessRecord:
    """一个 llama-server 进程的完整生命周期，作为「确实杀了进程」的证据。"""

    generation: int
    pid: int | None
    command: list[str]
    started_at: float
    ready_at: float | None = None
    killed_at: float | None = None
    returncode: int | None = None
    log_path: str | None = None
    kill_method: str | None = None
    kill_note: str | None = None

    @property
    def lifetime_s(self) -> float | None:
        if self.killed_at is None:
            return None
        return round(self.killed_at - self.started_at, 3)


class ServerHandle:
    """自己拉起 / 杀掉 llama-server（拿到 pid 与 returncode 才有说服力）。"""

    def __init__(
        self,
        bin_dir: Path,
        model: Path,
        port: int,
        ctx: int,
        ngl: int,
        slot_dir: Path | None,
        log_dir: Path,
        tag: str = "srv",
        extra_args: list[str] | None = None,
    ) -> None:
        self.bin_dir = bin_dir
        self.model = model
        self.port = port
        self.ctx = ctx
        self.ngl = ngl
        self.slot_dir = slot_dir
        self.log_dir = log_dir
        self.tag = tag
        self.extra_args = extra_args or []
        self.proc: subprocess.Popen | None = None
        self.record: ProcessRecord | None = None
        self.records: list[ProcessRecord] = []
        self._log_handle = None
        self._generation = 0

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def command(self) -> list[str]:
        cmd = [
            str(self.bin_dir / "llama-server.exe"),
            "-m",
            str(self.model),
            "-ngl",
            str(self.ngl),
            "-c",
            str(self.ctx),
            "--port",
            str(self.port),
            "-np",
            "1",
        ]
        if self.slot_dir is not None:
            cmd += ["--slot-save-path", str(self.slot_dir)]
        cmd += self.extra_args
        return cmd

    def start(self, timeout: float = 300.0) -> ProcessRecord:
        self._generation += 1
        cmd = self.command()
        self.log_dir.mkdir(parents=True, exist_ok=True)
        log_path = self.log_dir / f"{self.tag}-p{self.port}-gen{self._generation}.log"
        # 必须切到 bin 目录，否则同目录的 ggml-cuda.dll / cudart64_12.dll 找不到
        self._log_handle = log_path.open("wb")
        started = time.time()
        self.proc = subprocess.Popen(
            cmd,
            cwd=str(self.bin_dir),
            stdout=self._log_handle,
            stderr=subprocess.STDOUT,
        )
        record = ProcessRecord(
            generation=self._generation,
            pid=self.proc.pid,
            command=cmd,
            started_at=started,
            log_path=str(log_path),
        )
        self.record = record
        self.records.append(record)

        if not wait_health(self.base_url, timeout):
            alive = self.proc.poll()
            self.stop()
            detail = f"进程提前退出，returncode={alive}" if alive is not None else "/health 一直不通"
            raise RuntimeError(f"llama-server 未能就绪（{detail}），日志尾部：\n{tail_text(log_path)}")
        record.ready_at = time.time()
        return record

    def stop(self) -> ProcessRecord | None:
        record = self.record
        if self.proc is None or record is None:
            return None
        if self.proc.poll() is None:
            self.proc.terminate()  # Windows: TerminateProcess —— 真杀
            record.kill_method = "terminate"
            try:
                self.proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                record.kill_method = "kill(强制)"
                self.proc.wait(timeout=15)
        record.returncode = self.proc.returncode
        record.killed_at = time.time()
        record.kill_note = (
            "Windows 下 terminate() 即 TerminateProcess（强制终止），returncode=1 表示"
            "「被强杀」而非「崩溃」—— 这正是本票要的：进程状态一并消失，"
            "不是靠重启 HTTP 连接来假装。"
        )
        if self._log_handle is not None:
            self._log_handle.close()
            self._log_handle = None
        freed = wait_port_free("127.0.0.1", self.port)
        if not freed:
            raise RuntimeError(f"端口 {self.port} 在 kill 后仍未释放 —— 重启不可信")
        self.proc = None
        self.record = None
        return record

    def restart(self, timeout: float = 300.0) -> ProcessRecord:
        self.stop()
        return self.start(timeout)


def process_lifecycle(records: list[ProcessRecord]) -> list[dict]:
    """把进程记录转成可序列化 dict。`lifetime_s` 是 property，`asdict` 带不出来，要显式补。"""
    out = []
    for record in records:
        item = asdict(record)
        item["lifetime_s"] = record.lifetime_s
        out.append(item)
    return out


# --------------------------------------------------------------------------------------
# slot API 客户端
# --------------------------------------------------------------------------------------


class SlotClient:
    """瘦封装。`trust_env=False` 是硬要求（T-02 结论 C6）。"""

    def __init__(self, base_url: str, timeout: float = 900.0) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(base_url=self.base_url, timeout=timeout, trust_env=False)

    def close(self) -> None:
        self._client.close()

    def health(self) -> bool:
        try:
            return self._client.get("/health").status_code == 200
        except httpx.HTTPError:
            return False

    def tokenize(self, content: str) -> list[int]:
        return self._client.post("/tokenize", json={"content": content}).json()["tokens"]

    def generate(
        self,
        prompt: str | list[int],
        n_predict: int,
        seed: int,
        cache_prompt: bool = True,
        slot_id: int = 0,
        temperature: float = 0.0,
    ) -> dict:
        """`temperature=0` + 固定 `seed` —— 采样参数必须钉死。"""
        body = {
            "prompt": prompt,
            "n_predict": n_predict,
            "temperature": temperature,
            "seed": seed,
            "id_slot": slot_id,
            "cache_prompt": cache_prompt,
            "return_tokens": True,
            "stream": False,
        }
        response = self._client.post("/completion", json=body)
        response.raise_for_status()
        data = response.json()
        timings = data.get("timings", {})
        return {
            "prompt_tokens": data.get("tokens_evaluated"),
            "tokens": data["tokens"],
            "content": data["content"],
            "tokens_predicted": data["tokens_predicted"],
            "tokens_evaluated": data["tokens_evaluated"],
            "stop_type": data.get("stop_type"),
            "cache_n": timings.get("cache_n"),
            "prompt_ms": round(timings.get("prompt_ms", 0.0), 3),
            "predicted_ms": round(timings.get("predicted_ms", 0.0), 3),
            "predicted_per_second": round(timings.get("predicted_per_second", 0.0), 2),
            "raw_timings": timings,
        }

    def slot_action(self, action: str, filename: str, slot_id: int = 0) -> dict:
        response = self._client.post(
            f"/slots/{slot_id}",
            params={"action": action},
            json={"filename": filename},
        )
        result: dict = {"http_status": response.status_code, "ok": response.status_code == 200}
        try:
            result["body"] = response.json()
        except ValueError:
            result["body"] = response.text[:400]
        return result

    def slots(self) -> list[dict]:
        return self._client.get("/slots").json()


# --------------------------------------------------------------------------------------
# 比对
# --------------------------------------------------------------------------------------


def compare_tokens(expected: list[int], actual: list[int], context: int = 5) -> dict:
    """逐 token 比对。不一致时给出首个分歧位置与两侧各 `context` 个 token 的上下文。"""
    n = min(len(expected), len(actual))
    first = next((i for i in range(n) if expected[i] != actual[i]), None)
    result = {
        "equal": first is None and len(expected) == len(actual),
        "expected_len": len(expected),
        "actual_len": len(actual),
        "first_divergence": first,
        "expected_sha256": sha256_ints(expected),
        "actual_sha256": sha256_ints(actual),
    }
    if first is None:
        result["common_prefix_len"] = n
        result["common_prefix_sha256"] = sha256_ints(expected[:n])
        if len(expected) != len(actual):
            result["note"] = "前缀完全一致但长度不同"
    else:
        result["common_prefix_len"] = first
        result["common_prefix_sha256"] = sha256_ints(expected[:first])
        lo = max(0, first - context)
        hi = min(len(expected), first + context + 1)
        result["expected_window"] = {"from": lo, "to": hi - 1, "tokens": expected[lo:hi]}
        result["actual_window"] = {"from": lo, "to": hi - 1, "tokens": actual[lo:hi]}
    return result


def write_report(path: Path, report: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


def build_prompt_tokens(client: SlotClient, target_len: int, seed_text: str) -> list[int]:
    """构造**恰好 `target_len` 个 token** 的 prompt。

    用 token 数组而不是文本：长度精确可控，prefill 量的名义值与实际值才一致。
    """
    tokens: list[int] = []
    filler = client.tokenize(seed_text)
    while len(tokens) < target_len:
        tokens.extend(filler)
    return tokens[:target_len]


def linear_fit(xs: list[float], ys: list[float]) -> dict:
    """最小二乘线性拟合 + R²。手算，不引 numpy（spike 不新增依赖）。"""
    n = len(xs)
    if n < 2:
        return {"ok": False, "reason": "样本不足"}
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    sxx = sum((x - mean_x) ** 2 for x in xs)
    sxy = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    if sxx == 0:
        return {"ok": False, "reason": "x 无变化"}
    slope = sxy / sxx
    intercept = mean_y - slope * mean_x
    ss_tot = sum((y - mean_y) ** 2 for y in ys)
    ss_res = sum((y - (slope * x + intercept)) ** 2 for x, y in zip(xs, ys))
    r2 = 1.0 if ss_tot == 0 else 1 - ss_res / ss_tot
    residuals = []
    for x, y in zip(xs, ys):
        pred = slope * x + intercept
        residuals.append(
            {
                "x": x,
                "y": y,
                "predicted": pred,
                "abs_error": abs(pred - y),
                "rel_error_pct": (abs(pred - y) / y * 100) if y else None,
            }
        )
    max_rel = max((r["rel_error_pct"] for r in residuals if r["rel_error_pct"] is not None), default=None)
    return {
        "ok": True,
        "n": n,
        "slope": slope,
        "intercept": intercept,
        "r2": r2,
        "max_rel_error_pct": max_rel,
        "residuals": residuals,
        "formula": f"bytes = {slope:.3f} × tokens + {intercept:.1f}",
    }


@dataclass
class Sample:
    """一条原始样本。票面要求**每条都留**，不只留中位数。"""

    label: str
    context_tokens: int
    elapsed_ms: float
    extra: dict = field(default_factory=dict)
