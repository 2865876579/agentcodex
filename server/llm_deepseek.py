"""
DeepSeek 对话模块 —— 基于 Function Calling

架构：
  用户输入 → chat() → DeepSeek（带工具定义）→ 模型决策调哪个工具 → 执行 → 回传结果 → 最终回复

工具扩展方法：
  1. 在 TOOLS 列表里新增一个工具定义（name/description/parameters）
  2. 在 _dispatch_tool() 里加对应的 elif 分支处理逻辑
  3. chat() 主循环不需要动
"""
import json
from openai import AsyncOpenAI
from config import DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL, DEEPSEEK_MODEL
from web_search import search_web, format_search_results, direct_answer_from_results

client = AsyncOpenAI(api_key=DEEPSEEK_API_KEY, base_url=DEEPSEEK_BASE_URL)

# ── 系统提示词 ──────────────────────────────────────────────
SYSTEM_PROMPT = """你是“小智”，一个放在用户枕边的语音陪伴助手。用户正通过语音和你连续对话，不是在读屏幕。

性格：
- 温柔、安静、有耐心，但不要像朗读文章
- 说话像真实口语，先回应用户当下这句话
- 用户睡不着时，可以主动给呼吸法、渐进式放松、轻声聊天或睡前小故事
- 不确定的信息要说明不确定；实时信息优先用 web_search 查询

说话方式：
- 默认 1-2 句，最长不超过 60 字
- 每句话尽量短，适合语音播报
- 少用书面总结腔，不说“以下是”“首先其次”，除非用户明确要步骤
- 可以用“嗯”“好”“我在”这类短口语，但不要每句都加
- 不带 emoji，不使用 Markdown
- 需要追问时，只问一个短问题

能力：
- 需要实时信息时（天气、新闻等），主动用 web_search 联网查，不做猜测
- 查到的结果用最简洁的方式播报，控制在 80 字以内
- 能聊睡前话题：明天的天气、助眠音乐推荐、睡前小故事、冥想引导
"""
# ── 工具定义 ────────────────────────────────────────────────
# 每个工具对应一个真实能力，description 要让模型能准确判断何时调用
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": (
                "联网搜索实时信息。适用于：天气、气温、新闻、热点、金价、股票、"
                "汇率、油价、赛事比分、航班、政策等需要最新数据的问题。"
                "不确定信息是否实时时，优先调用此工具而不是凭记忆回答。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "搜索关键词，尽量简洁，如『北京天气』、『今日黄金价格』",
                    }
                },
                "required": ["query"],
            },
        },
    },
    # 后续在这里追加更多工具：pc_command、device_control 等
]

MAX_HISTORY = 20   # 保留最近 N 条消息，避免 token 超限
MAX_TURNS = 5      # 单次请求最多允许模型连续调用工具的轮数，防止死循环


async def _dispatch_tool(name: str, arguments: dict) -> str:
    """
    执行模型请求的工具调用，返回结果字符串给模型。

    扩展：加新工具时在这里加 elif 分支即可，chat() 不需要动。
    """
    if name == "web_search":
        query = arguments.get("query", "")
        print(f"[Tool] web_search query={query!r}")
        results = await search_web(query)
        if not results:
            print(f"[Tool] web_search 无结果")
            return "没有搜到可靠结果。"
        direct = direct_answer_from_results(query, results)
        answer = direct if direct else format_search_results(query, results)
        print(f"[Tool] web_search result_len={len(answer)} direct={direct is not None}")
        return answer

    # 后续在此处添加更多工具，例如：
    # elif name == "pc_command":
    #     ...
    return f"工具 {name} 暂未实现"


async def chat_stream(user_text: str, history: list[dict] | None = None):
    """
    ★ xiaozhi 风格流式对话，支持 Function Calling。

    跨多轮 LLM 调用连续 yield token，中间插工具执行也不打断流。
    调用方只需一个 async for，不需要关心工具调用细节。
    """
    if history is None:
        history = []

    history.append({"role": "user", "content": user_text})
    if len(history) > MAX_HISTORY:
        history[:] = history[-MAX_HISTORY:]

    for _ in range(MAX_TURNS):
        response = await client.chat.completions.create(
            model=DEEPSEEK_MODEL,
            messages=[{"role": "system", "content": SYSTEM_PROMPT}] + history,
            max_tokens=1024,
            tools=TOOLS,
            stream=True,
        )

        content_parts: list[str] = []
        tool_calls: dict[int, dict] = {}

        async for chunk in response:
            delta = chunk.choices[0].delta

            if delta.content:
                content_parts.append(delta.content)
                yield delta.content  # ← 立刻流出，不等待

            if delta.tool_calls:
                for tc in delta.tool_calls:
                    idx = tc.index
                    if idx not in tool_calls:
                        tool_calls[idx] = {"id": "", "function": {"name": "", "arguments": ""}}
                    if tc.id:
                        tool_calls[idx]["id"] = tc.id
                    if tc.function:
                        if tc.function.name:
                            tool_calls[idx]["function"]["name"] += tc.function.name
                        if tc.function.arguments:
                            tool_calls[idx]["function"]["arguments"] += tc.function.arguments

        content = "".join(content_parts).strip()

        # 有工具调用 → 执行并继续下一轮
        if tool_calls:
            tc_list = []
            for idx in sorted(tool_calls.keys()):
                tc = tool_calls[idx]
                tc_list.append({
                    "id": tc["id"],
                    "type": "function",
                    "function": tc["function"],
                })

            history.append({
                "role": "assistant",
                "content": content or None,
                "tool_calls": tc_list,
            })

            for tc in tc_list:
                args = json.loads(tc["function"]["arguments"])
                result = await _dispatch_tool(tc["function"]["name"], args)
                history.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": result,
                })
            continue  # ← 下一轮 LLM，继续 yield

        # 纯文本回复，没有工具调用
        reply = content
        history.append({"role": "assistant", "content": reply})
        if len(history) > MAX_HISTORY:
            history[:] = history[-MAX_HISTORY:]
        return

    # 超过最大轮数
    fallback = "抱歉，我刚才有点转不过来，能再说一遍吗？"
    yield fallback
    history.append({"role": "assistant", "content": fallback})
