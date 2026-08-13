"""
冒烟测试：全链路串联验证 6 条核心路径。

用法:
    uv run python scripts/smoke_test.py              # 本地运行
    uv run python scripts/smoke_test.py --base http://localhost:8000  # 指定地址

依赖: requests（已装） + 后端已启动
"""
from __future__ import annotations

import json
import sys
import time
import argparse
from typing import Optional

# Windows 控制台默认 GBK，回答可能含 emoji/特殊字符（如 ⚠），
# 强制 UTF-8 输出避免 UnicodeEncodeError
if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if sys.stderr and hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

try:
    import requests
except ImportError:
    print("[FATAL] 需要 requests 库: pip install requests")
    sys.exit(1)

# ============================================================
# 测试结果收集
# ============================================================

passed = 0
failed = 0
failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    global passed, failed
    if condition:
        passed += 1
        print(f"  [PASS] {name}")
    else:
        failed += 1
        msg = f"{name}: {detail}" if detail else name
        failures.append(msg)
        print(f"  [FAIL] {msg}")


def test_header(name: str) -> None:
    print(f"\n{'='*60}")
    print(f"  {name}")
    print(f"{'='*60}")


# ============================================================
# 主流程
# ============================================================

def run(base_url: str) -> None:
    global passed, failed, failures
    token: Optional[str] = None

    # ---- 1. 健康检查 ----
    test_header("1. GET /api/health — 健康检查")
    try:
        r = requests.get(f"{base_url}/api/health", timeout=10)
        check("HTTP 200", r.status_code == 200, f"got {r.status_code}")
        data = r.json()
        check("status=ok", data.get("status") == "ok", str(data))
        check("index_ready=True", data.get("index_ready") is True, str(data))
        check("doc_count > 0", data.get("doc_count", 0) > 0, f"doc_count={data.get('doc_count')}")
        check("llm_model 非空", bool(data.get("llm_model")), str(data))
    except Exception as e:
        check("health 请求成功", False, str(e))
        print(f"  [SKIP] 后端未启动，跳过后续测试")
        return

    # ---- 2. 注册 ----
    test_header("2. POST /api/auth/register — 用户注册")
    test_user = "smoke_test_user"
    test_pass = "test123456"
    try:
        r = requests.post(
            f"{base_url}/api/auth/register",
            json={"username": test_user, "password": test_pass},
            timeout=10,
        )
        check("注册请求成功 (200/409)", r.status_code in (200, 409),
              f"got {r.status_code}: {r.text[:100]}")
        if r.status_code == 200:
            data = r.json()
            token = data.get("token")
            check("返回 token", bool(token), str(data))
    except Exception as e:
        check("注册请求成功", False, str(e))

    # ---- 3. 登录 ----
    test_header("3. POST /api/auth/login — 用户登录")
    if not token:
        try:
            r = requests.post(
                f"{base_url}/api/auth/login",
                json={"username": test_user, "password": test_pass},
                timeout=10,
            )
            check("登录成功", r.status_code == 200, f"got {r.status_code}")
            data = r.json()
            token = data.get("token")
            check("返回 token", bool(token), str(data))
        except Exception as e:
            check("登录请求成功", False, str(e))

    headers = {"Authorization": f"Bearer {token}"} if token else {}

    # ---- 4. 非流式问答 ----
    test_header("4. POST /api/chat — 法律问答")
    try:
        t0 = time.perf_counter()
        r = requests.post(
            f"{base_url}/api/chat",
            json={"query": "治安管理处罚的种类有哪些", "top_k": 5},
            headers=headers,
            timeout=180,
        )
        elapsed = (time.perf_counter() - t0) * 1000
        check("HTTP 200", r.status_code == 200, f"got {r.status_code}")
        data = r.json()
        check("answer 非空", len(data.get("answer", "")) > 10,
              f"answer_len={len(data.get('answer',''))}")
        check("sources 非空", len(data.get("sources", [])) > 0,
              f"sources_count={len(data.get('sources',[]))}")
        check("sources 含 law_name", any(s.get("law_name") for s in data.get("sources", [])),
              str(data.get("sources", [])[:1]))
        check("有 citation", any(s.get("citation") for s in data.get("sources", [])),
              str(data.get("sources", [])[:1]))
        print(f"  [INFO] 回答耗时: {elapsed:.0f}ms")
        print(f"  [INFO] 回答预览: {data.get('answer', '')[:80]}...")
        sources_preview = ", ".join(
            s.get("citation", "?") for s in data.get("sources", [])[:3]
        )
        print(f"  [INFO] 引用来源: {sources_preview}")
    except Exception as e:
        check("问答请求成功", False, str(e))

    # ---- 5. 流式问答 ----
    test_header("5. POST /api/chat/stream — 流式问答")
    try:
        t0 = time.perf_counter()
        r = requests.post(
            f"{base_url}/api/chat/stream",
            json={"query": "行政拘留最长多少天", "top_k": 3},
            headers=headers,
            timeout=180,
            stream=True,
        )
        check("HTTP 200", r.status_code == 200, f"got {r.status_code}")

        events = []
        first_token_ms = 0
        t_first = 0
        got_token = False
        got_sources = False
        got_done = False

        for line in r.iter_lines():
            if not line:
                continue
            line = line.decode("utf-8")
            if not line.startswith("data: "):
                continue
            data_str = line[6:]
            if data_str == "[DONE]":
                got_done = True
                break
            try:
                event = json.loads(data_str)
                events.append(event)
                etype = event.get("type", "")
                if etype == "meta" and event.get("sources"):
                    got_sources = True
                if etype == "token":
                    if not got_token:
                        got_token = True
                        t_first = time.perf_counter()
                if etype == "error":
                    check("流式无错误", False, event.get("content", ""))
            except json.JSONDecodeError:
                pass

        elapsed = (time.perf_counter() - t0) * 1000
        if got_token:
            first_token_ms = (t_first - t0) * 1000

        check("收到 [DONE]", got_done)
        check("收到 sources", got_sources)
        check("收到 token", got_token)
        check("无错误事件", not any(e.get("type") == "error" for e in events))

        answer_text = "".join(
            e.get("content", "") for e in events if e.get("type") == "token"
        )
        check("流式回答非空", len(answer_text) > 10, f"len={len(answer_text)}")

        print(f"  [INFO] 总耗时: {elapsed:.0f}ms")
        print(f"  [INFO] 首字延迟: {first_token_ms:.0f}ms")
        print(f"  [INFO] 事件数: {len(events)}")
        # 预览可能含 emoji/特殊字符，GBK 控制台会报 UnicodeEncodeError
        print(f"  [INFO] 回答预览: {answer_text[:80]!r}...")
    except Exception as e:
        check("流式请求成功", False, str(e))

    # ---- 6. 多轮对话 ----

    test_header("6. POST /api/chat — 多轮对话")
    try:
        # 第一轮
        r1 = requests.post(
            f"{base_url}/api/chat",
            json={"query": "正当防卫怎么认定", "top_k": 3},
            headers=headers,
            timeout=180,
        )
        check("第1轮 200", r1.status_code == 200, f"got {r1.status_code}")

        # 第二轮：追问（带历史）
        history = [
            {"role": "user", "content": "正当防卫怎么认定"},
            {"role": "assistant", "content": r1.json().get("answer", "")[:200]},
        ]
        r2 = requests.post(
            f"{base_url}/api/chat",
            json={"query": "那防卫过当呢", "history": history, "top_k": 3},
            headers=headers,
            timeout=180,
        )
        check("第2轮 200", r2.status_code == 200, f"got {r2.status_code}")
        data2 = r2.json()
        check("追问 answer 非空", len(data2.get("answer", "")) > 5,
              f"answer_len={len(data2.get('answer',''))}")
        check("追问有 sources", len(data2.get("sources", [])) > 0)
        print(f"  [INFO] 追问回答: {data2.get('answer', '')[:80]}...")
    except Exception as e:
        check("多轮对话成功", False, str(e))

    # ---- 汇总 ----
    test_header("冒烟测试结果汇总")
    total = passed + failed
    print(f"  通过: {passed}/{total}")
    if failed:
        print(f"  失败: {failed}/{total}")
        print(f"\n  失败详情:")
        for f in failures:
            print(f"    - {f}")
    else:
        print(f"  全部通过!")

    if token:
        print(f"\n  Token: {token[:20]}...")


# ============================================================
# 入口
# ============================================================

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Law-RAG-Agent 冒烟测试")
    ap.add_argument("--base", default="http://localhost:8000",
                    help="后端地址 (默认 http://localhost:8000)")
    args = ap.parse_args()
    run(args.base)
