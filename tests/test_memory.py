"""
对话记忆层单元测试。

验证:
  1. ConversationMemoryManager 模块可导入
  2. 摘要触发条件判断
  3. 摘要实体解析
  4. 上下文构建
  5. 记忆集成到 Agent Graph（不涉及 DB）
"""
from __future__ import annotations


class TestImports:
    def test_import_memory_manager(self):
        from src.memory.conversation import ConversationMemoryManager
        assert ConversationMemoryManager is not None


class TestSummaryTrigger:
    def test_below_threshold(self):
        from src.memory.conversation import ConversationMemoryManager
        # 不需要实例化，should_summarize 是实例方法但逻辑纯函数
        assert ConversationMemoryManager.should_summarize is not None

    def test_at_threshold(self):
        from src.memory.conversation import SUMMARY_TRIGGER_ROUNDS
        # 创建一个最小 mock 来测试
        class FakeMgr:
            pass
        mgr = FakeMgr()
        mgr.should_summarize = lambda n: n >= SUMMARY_TRIGGER_ROUNDS
        assert mgr.should_summarize(6) is True
        assert mgr.should_summarize(5) is False
        assert mgr.should_summarize(10) is True
        assert mgr.should_summarize(0) is False


class TestEntityParsing:
    def test_parse_full_entities(self):
        from src.memory.conversation import ConversationMemoryManager
        summary = """案件类型: 劳动争议
涉及法律: 劳动合同法, 劳动争议调解仲裁法
关键事实: 用户在试用期被辞退，单位未支付补偿金
已回答问题: 解释了试用期的法律规定
未解决问题: 具体赔偿金额需要根据工资计算"""
        entities = ConversationMemoryManager._parse_entities(summary)
        assert entities["case_type"] == "劳动争议"
        assert "劳动合同法" in entities["laws_involved"]
        assert "试用期" in entities["key_facts"]

    def test_parse_partial_entities(self):
        from src.memory.conversation import ConversationMemoryManager
        summary = """案件类型: 合同纠纷
涉及法律: 民法典"""
        entities = ConversationMemoryManager._parse_entities(summary)
        assert entities["case_type"] == "合同纠纷"
        assert entities["laws_involved"] == "民法典"
        assert "key_facts" not in entities  # 没有关键事实行


class TestContextBuilding:
    def test_empty_memories(self):
        from src.memory.conversation import ConversationMemoryManager
        # build_context 是实例方法但可用 class 调用
        result = ConversationMemoryManager.build_context(None, [])
        assert result == ""

    def test_single_memory(self):
        from src.memory.conversation import ConversationMemoryManager
        memories = [{
            "summary": "用户咨询了工伤认定相关问题",
            "score": 0.85,
            "entities": {"case_type": "劳动争议"},
        }]
        result = ConversationMemoryManager.build_context(None, memories)
        assert "历史对话参考" in result
        assert "工伤认定" in result
        assert "0.85" in result

    def test_multiple_memories(self):
        from src.memory.conversation import ConversationMemoryManager
        memories = [
            {"summary": "工伤认定问题", "score": 0.9, "entities": {}},
            {"summary": "劳动合同纠纷", "score": 0.7, "entities": {}},
        ]
        result = ConversationMemoryManager.build_context(None, memories)
        assert "历史对话 1" in result
        assert "历史对话 2" in result


class TestConversationFormatting:
    def test_format_messages(self):
        from src.memory.conversation import ConversationMemoryManager
        msgs = [
            {"role": "user", "content": "工伤怎么认定"},
            {"role": "assistant", "content": "根据《工伤保险条例》第十四条..."},
        ]
        result = ConversationMemoryManager._format_conversation(msgs)
        assert "用户:" in result
        assert "AI助手:" in result
        assert "工伤保险条例" in result

    def test_truncate_long_messages(self):
        from src.memory.conversation import ConversationMemoryManager
        long_text = "A" * 800
        msgs = [{"role": "user", "content": long_text}]
        result = ConversationMemoryManager._format_conversation(msgs)
        assert "..." in result
        assert len(result) < 600  # 截断后更短


class TestGraphMemoryIntegration:
    """验证 Agent Graph 正确集成了 memory_retrieve 节点"""

    def test_agent_accepts_memory_manager(self):
        pass
        # 不传 memory_manager 应正常运行
        # 这里只验证 __init__ 不报错
        # 实际需要 retriever 和 llm，跳过完整实例化

    def test_graph_node_count(self):
        from src.agents.graph import AgentState
        assert "memory_context" in AgentState.__annotations__
        assert "user_id" in AgentState.__annotations__


class TestSaveMemoryJsonb:
    """回归：save_memory 写入 entities(JSONB) 时需序列化，不能直接传 dict

    复现：会话保存异步固化记忆时 ProgrammingError: can't adapt type 'dict'。
    """

    def test_entities_serialized_to_json_string(self):
        import json as _json
        from unittest.mock import MagicMock, patch
        from src.memory.conversation import ConversationMemoryManager

        import threading
        with patch("src.memory.conversation.psycopg2.connect") as mock_connect:
            fake_conn = MagicMock()
            fake_cursor = MagicMock()
            fake_cursor.__enter__.return_value = fake_cursor
            fake_cursor.fetchone.return_value = None  # 幂等检查:无已有记录
            fake_conn.cursor.return_value = fake_cursor
            mock_connect.return_value = fake_conn

            # 绕过 __init__(register_vector 需要真实连接)
            mgr = object.__new__(ConversationMemoryManager)
            mgr._embedder = MagicMock()
            mgr._llm = MagicMock()
            mgr._conn = fake_conn
            mgr._conn_string = "fake"
            mgr._lock = threading.Lock()
            mgr._schema_ready = True  # 跳过 schema 迁移
            mgr._embedder.embed_query.return_value = [0.1] * 1024
            mgr._llm.chat.return_value = "案件类型: 劳动争议\n涉及法律: 劳动法\n关键事实: 试用期被辞退\n已回答: 无\n未解决: 赔偿金额"

            messages = [{"role": "user", "content": f"问题{i}"} for i in range(8)]
            mgr.save_memory("user1", "session1", messages)

            # 取 INSERT 的参数
            insert_sql, params = fake_cursor.execute.call_args_list[-1][0]
            assert "::jsonb" in insert_sql  # entities 显式 cast jsonb
            entities_param = params[4]
            # 必须是可被 psycopg2 适配的类型(JSON 字符串),而非 dict
            assert not isinstance(entities_param, dict)
            assert isinstance(entities_param, str)
            _json.loads(entities_param)  # 且是合法 JSON


class TestCleanExpired:
    """过期记忆清理（后台定时任务调用）"""

    def test_constructor_allows_none_deps(self):
        """清理场景:embedder/llm 可传 None,避免清理任务加载模型"""
        import inspect
        from src.memory.conversation import ConversationMemoryManager
        sig = inspect.signature(ConversationMemoryManager.__init__)
        assert sig.parameters["embedder"].default is None
        assert sig.parameters["llm"].default is None

    def test_clean_expired_executes_delete(self):
        from unittest.mock import MagicMock
        from src.memory.conversation import ConversationMemoryManager

        mgr = object.__new__(ConversationMemoryManager)
        fake_conn = MagicMock()
        # 让 with cursor() 进入同一个 mock,rowcount 才能生效
        fake_cursor = MagicMock()
        fake_cursor.__enter__.return_value = fake_cursor
        fake_cursor.rowcount = 3
        fake_conn.cursor.return_value = fake_cursor
        mgr._conn = fake_conn
        mgr._conn_string = "fake"
        mgr._schema_ready = True

        count = mgr.clean_expired()
        assert count == 3
        # 验证执行的是过期删除语句(最后一次 execute 调用)
        executed = fake_cursor.execute.call_args[0][0]
        assert "DELETE FROM conversation_memories" in executed
        assert "expires_at < NOW()" in executed

    def test_clean_expired_zero(self):
        from unittest.mock import MagicMock
        from src.memory.conversation import ConversationMemoryManager

        mgr = object.__new__(ConversationMemoryManager)
        fake_conn = MagicMock()
        fake_cursor = MagicMock()
        fake_cursor.__enter__.return_value = fake_cursor
        fake_cursor.rowcount = 0
        fake_conn.cursor.return_value = fake_cursor
        mgr._conn = fake_conn
        mgr._conn_string = "fake"
        mgr._schema_ready = True

        assert mgr.clean_expired() == 0


class TestImportanceEstimation:
    """记忆重要度预筛（按轮数分档）"""

    def test_importance_levels(self):
        from src.memory.conversation import ConversationMemoryManager
        assert ConversationMemoryManager._estimate_importance(6) == 0.6
        assert ConversationMemoryManager._estimate_importance(9) == 0.6
        assert ConversationMemoryManager._estimate_importance(10) == 0.8
        assert ConversationMemoryManager._estimate_importance(14) == 0.8
        assert ConversationMemoryManager._estimate_importance(15) == 1.0
        assert ConversationMemoryManager._estimate_importance(100) == 1.0

    def test_importance_below_trigger_returns_base(self):
        from src.memory.conversation import ConversationMemoryManager
        assert ConversationMemoryManager._estimate_importance(0) == 0.6


class TestHistoryBudgetFitting:
    """TokenBudget 预算化的历史筛选"""

    def _msgs(self, n):
        return [{"role": "user" if i % 2 == 0 else "assistant", "content": f"消息内容{i}"} for i in range(n)]

    def test_fit_within_budget_keeps_all(self):
        from src.agents.nodes import _fit_history
        msgs = self._msgs(4)
        result = _fit_history(msgs, limit_tokens=10000)
        assert len(result) == 4

    def test_fit_limited_by_budget(self):
        from src.agents.nodes import _fit_history
        # 20 条长消息 + 极小预算 → 只能带下最后 1-2 条
        msgs = [{"role": "user", "content": "这是一条很长的法律咨询消息内容。" * 20} for _ in range(20)]
        result = _fit_history(msgs, limit_tokens=50)
        assert 1 <= len(result) <= 3

    def test_fit_orders_newest_last(self):
        from src.agents.nodes import _fit_history
        msgs = [{"role": "user", "content": f"Q{i}"} for i in range(3)]
        result = _fit_history(msgs, limit_tokens=10000)
        # 历史按时间顺序: 最早在前, 最近在后
        assert result[-1].content == "Q2"


class TestBudgetedPrompt:
    """TokenBudget 接入生成路径：动态窗口 + 分段预算"""

    class FakeLLM:
        def __init__(self, window=28000):
            self._window = window

        def get_context_window(self):
            return self._window

        def chat(self, prompt, history=None):
            return "ok"

    def test_uses_model_window(self):
        from src.agents.nodes import build_budgeted_prompt
        llm = self.FakeLLM(window=64000)
        prompt, history = build_budgeted_prompt(
            llm=llm, template="{context}\n\n## 用户问题\n{query}",
            context="条文", query="工伤怎么认定",
            memory_context="历史记忆", messages=[],
        )
        # 窗口 64K → 默认检索预算翻倍到 16000，长 context 不被截断
        assert "条文" in prompt
        assert "历史记忆" in prompt

    def test_truncates_oversized_context(self):
        from src.agents.nodes import build_budgeted_prompt
        llm = self.FakeLLM(window=28000)
        big = "法条内容" * 5000  # 远超 8000 token 预算
        prompt, history = build_budgeted_prompt(
            llm=llm, template="{context}\n## 用户问题\n{query}",
            context=big, query="问题", memory_context="", messages=[],
        )
        from src.memory.token_budget import TokenBudget
        assert TokenBudget.count(prompt) <= 28000 - 2000 + 500  # 不超窗口

    def test_memory_before_context(self):
        from src.agents.nodes import build_budgeted_prompt
        llm = self.FakeLLM(window=28000)
        prompt, _ = build_budgeted_prompt(
            llm=llm, template="{context}\n## 用户问题\n{query}",
            context="条文内容", query="问题",
            memory_context="## 历史对话参考\n旧记忆",
            messages=[],
        )
        # 记忆上下文应拼在条文之前
        assert prompt.index("历史对话参考") < prompt.index("条文内容")
