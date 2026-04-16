import asyncio
import sys
import os

# 添加路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_utils import (
    build_agent_run_info,
    run_agent_with_tracking,
    parse_conversation_to_history,
    print_history_stats,
    AgentHistory,
    ContextManagerConfig
)
from nexent.core.agents.agent_context import ContextManager


async def run_multi_turn(
    queries: list[str],
    base_history: list[AgentHistory],
    cm_config: ContextManagerConfig,
    max_steps: int = 5,
    debug: bool = False
) -> list:
    """
    执行自定义多轮对话测试。

    每轮对话会基于累积的 conversation_history（包含 base_history + 前几轮对话）
    运行 Agent，并将当前轮的 query 与 assistant 回复追加到 history 中，
    供下一轮使用。

    当 cm_config.enabled 为 True 时，会创建一个 conversation 级别的 ContextManager
    并在多轮之间复用，以验证跨 run 的摘要缓存机制。

    Args:
        queries: 用户 query 列表，按顺序执行
        base_history: 初始加载的历史记录（如从 history.md 解析）
        cm_config: 上下文管理器配置
        max_steps: 每轮最大步数
        debug: 是否开启调试输出

    Returns:
        每轮 AgentRunResult 的列表
    """
    conversation_history = list(base_history)  # 深拷贝避免污染原始历史
    results = []

    # 创建 conversation 级别的 ContextManager（若启用）
    shared_cm = None
    if cm_config and cm_config.enabled:
        shared_cm = ContextManager(config=cm_config, max_steps=max_steps)

    print(f"\n{'='*60}")
    print(f"开始多轮对话测试 | context_manager={'启用' if cm_config.enabled else '禁用(baseline)'}")
    print(f"初始历史轮数: {len(base_history)//2} | 预定义 query 数: {len(queries)}")
    print(f"{'='*60}")

    for turn_idx, query in enumerate(queries, start=1):
        print(f"\n--- 第 {turn_idx}/{len(queries)} 轮 ---")
        print(f"用户: {query}")

        agent_run_info = build_agent_run_info(
            query,
            conversation_history,
            max_steps=max_steps,
            context_manager_config=cm_config
        )

        # 挂载 conversation 级别的 ContextManager，实现跨 run 复用
        if shared_cm is not None:
            agent_run_info.context_manager = shared_cm

        result = await run_agent_with_tracking(agent_run_info, debug=debug)
        results.append(result)

        print(f"助手: {result.final_answer[:200]}...")
        print(f"[本轮统计] {result.message_type_count}")

        # 将本轮对话追加到累积历史
        conversation_history.append(AgentHistory(role="user", content=query))
        conversation_history.append(AgentHistory(role="assistant", content=result.final_answer))

    # 打印 ContextManager 缓存统计（若启用）
    if shared_cm is not None:
        print(f"\n[ContextManager 全局统计]")
        print(f"  {shared_cm.get_all_compression_stats()}")

    print(f"\n{'='*60}")
    print(f"多轮对话结束 | 总对话轮数: {len(conversation_history)//2}")
    print(f"{'='*60}")
    return results


async def test_previous_run_overflow_opt():
    # AgentHistory List [user, assistant, user, assistant, ... 交替]
    agent_history = parse_conversation_to_history("./history.md")
    cm_config = ContextManagerConfig(enabled=True, token_threshold=10000, keep_recent_pairs=1)

    queries = [
        "总结之前对话的主题是什么",
        "基于这个主题，帮我写一个简短的提纲",
        "把刚才的提纲翻译成英文",
    ]

    results = await run_multi_turn(
        queries=queries,
        base_history=agent_history,
        cm_config=cm_config,
        max_steps=5,
        debug=False
    )

    # 可在此处追加结果断言或对比分析
    print_history_stats(agent_history)
    return results


async def test_previous_run_overflow_baseline():
    # AgentHistory List [user, assistant, user, assistant, ... 交替]
    agent_history = parse_conversation_to_history("./history.md")
    cm_config = ContextManagerConfig(enabled=False, token_threshold=10000, keep_recent_pairs=1)

    queries = [
        "总结之前对话的主题是什么",
        "基于这个主题，帮我写一个简短的提纲",
        "把刚才的提纲翻译成英文",
    ]

    results = await run_multi_turn(
        queries=queries,
        base_history=agent_history,
        cm_config=cm_config,
        max_steps=5,
        debug=False
    )

    print_history_stats(agent_history)
    return results


async def test_custom_queries():
    """
    示例：完全自定义多轮对话 query，不依赖 history.md。
    """
    base_history = [
        AgentHistory(role="user", content="我叫李四，是一名医生。"),
        AgentHistory(role="assistant", content="你好李四，很高兴认识你！有什么可以帮您的吗？"),
    ]
    cm_config = ContextManagerConfig(enabled=True, token_threshold=10000, keep_recent_pairs=2)

    queries = [
        "你还记得我的名字和职业吗？",
        "请给我一些保持健康的小建议。",
        "把这些建议总结成一句话。",
    ]

    results = await run_multi_turn(
        queries=queries,
        base_history=base_history,
        cm_config=cm_config,
        max_steps=5,
        debug=False
    )
    return results


if __name__ == "__main__":
    # res = parse_conversation_to_history("./history.md")
    # import pdb; pdb.set_trace()

    # 1. 先跑 baseline（context_manager 禁用）
    asyncio.run(test_previous_run_overflow_baseline())

    # 2. 再跑 opt（context_manager 启用）
    asyncio.run(test_previous_run_overflow_opt())

    # 3. 自定义小历史测试（可选）
    # asyncio.run(test_custom_queries())
