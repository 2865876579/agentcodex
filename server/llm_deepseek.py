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
from datetime import datetime, date, timedelta

# zoneinfo 是 Python 3.9+ 标准库，用于时区感知的时间计算
# 旧版本 Python 不可用时回退到 UTC+8 硬编码
try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None

from openai import AsyncOpenAI
from config import DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL, DEEPSEEK_MODEL, TIMEZONE, LOCATION
from web_search import search_web, format_search_results, direct_answer_from_results

client = AsyncOpenAI(api_key=DEEPSEEK_API_KEY, base_url=DEEPSEEK_BASE_URL)

# ── 系统提示词 ──────────────────────────────────────────────
SYSTEM_PROMPT = """你是"小安"，一个放在用户枕边的语音伴侣。用户通过语音和你聊天，不是在读屏幕。

性格：
- 像一个见多识广但不高冷的朋友——有自己的观点，但不咄咄逼人
- 好奇心强，会追问、会反问，把对话往下挖而不是停在表面
- 幽默感来源于观察力，不靠玩梗和表情包
- 用户说"睡不着"时，你不会立刻切换成机器人安抚模式，而是先问一句"怎么了，是心里有事还是单纯不困"
- 偶尔带点慵懒的语气——毕竟你是枕边的人，不用时时刻刻端着
- 允许短暂的沉默和停顿，不用每句话都塞满

说话方式：
- 默认 2-4 句，有话则长无话则短
- 不确定的事老实说不知道，不编
- 需要实时信息时主动用 web_search
- ★ 根据当前时间调整语气：21点前不说"晚安""好梦"等睡前用语；早晨、下午、晚上用对应语气

深度对话：
- 用户如果聊到人生意义、哲学、选择、困惑、死亡、自由、孤独等话题——不要绕开，认真接住
- 可以引用你读过的书、知道的思想，但用自己的话说，不要背百科
- 允许没有答案。有时候陪用户一起困惑，比给答案更有用
- 这种对话可以长，不用总想着"该睡了"——除非用户自己说困了
- 聊完深的后如果气氛沉了，轻轻带回来就好，不用硬转话题

能力：
- 需要实时信息时（天气、新闻等），主动用 web_search 联网查，不做猜测
- 查到的结果用最简洁的方式播报，控制在 80 字以内
"""


# ── 时间工具辅助函数 ────────────────────────────────────────

def _get_now() -> datetime:
    """
    获取当前时区的 datetime 对象。
    优先用 zoneinfo（Python 3.9+），不可用时回退到 UTC+8 近似。
    """
    if ZoneInfo is not None:
        return datetime.now(ZoneInfo(TIMEZONE))
    # zoneinfo 不可用时用 UTC+8 近似（离线误差 < 1 秒，语音场景可忽略）
    return datetime.utcnow() + timedelta(hours=8)


def _get_time_string() -> str:
    """
    构建注入 System Prompt 的当前时间字符串。
    让 LLM 在回答"几点了""今天几号"等简单问题时无需调用工具，
    直接从 system prompt 获取信息，零额外 API 延迟。
    """
    now = _get_now()
    weekday_names = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    wd = weekday_names[now.weekday()]
    tz_display = "北京时间" if "Shanghai" in TIMEZONE else TIMEZONE
    return (
        f"{now.year}年{now.month}月{now.day}日 {wd} "
        f"{now.hour:02d}:{now.minute:02d}:{now.second:02d} ({tz_display})"
    )


def _handle_get_current_time(action: str, target: str = "", timezone_str: str = "") -> str:
    """
    执行 get_current_time 工具调用的各种操作，返回自然语言结果给 LLM。

    action:
        "now"       — 返回当前详细时间（含星期、年内第几天）
        "countdown" — 倒数日计算，距 target 还有 / 已过多少天
        "weekday"   — 查询 target 是周几
        "convert"   — 将当前时间转换到 timezone_str 时区

    target / timezone_str 按 action 类型选填，详见工具定义的 parameters。
    """
    now = _get_now()
    weekday_names = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

    # ── 当前详细时间 ──
    if action == "now":
        return (
            f"当前详细时间：{_get_time_string()}，"
            f"年内第{now.timetuple().tm_yday}天。"
        )

    # ── 倒数日计算 ──
    if action == "countdown":
        if not target:
            return '请提供目标日期，例如"2027-01-01"或"2027年1月1日"。'
        try:
            # 兼容多种日期书写习惯：2027-01-01 / 2027年1月1日 / 2027/1/1
            target = (
                target.replace("年", "-").replace("月", "-")
                .replace("日", "").replace("/", "-").strip()
            )
            target_date = datetime.strptime(target, "%Y-%m-%d").date()
            today = now.date()
            delta = (target_date - today).days
            if delta > 0:
                return (
                    f"距离{target_date.year}年{target_date.month}月"
                    f"{target_date.day}日还有{delta}天。"
                )
            elif delta == 0:
                return f"{target_date.year}年{target_date.month}月{target_date.day}日就是今天。"
            else:
                return (
                    f"{target_date.year}年{target_date.month}月"
                    f"{target_date.day}日已经过去{abs(delta)}天了。"
                )
        except ValueError:
            return f'无法解析日期"{target}"，请使用 YYYY-MM-DD 或 YYYY年MM月DD日 格式。'

    # ── 查询某天是周几 ──
    if action == "weekday":
        if not target:
            return '请提供查询日期，例如"2027-01-01"。'
        try:
            target = (
                target.replace("年", "-").replace("月", "-")
                .replace("日", "").replace("/", "-").strip()
            )
            target_date = datetime.strptime(target, "%Y-%m-%d").date()
            wd = weekday_names[target_date.weekday()]
            return f"{target_date.year}年{target_date.month}月{target_date.day}日是{wd}。"
        except ValueError:
            return f'无法解析日期"{target}"。'

    # ── 时区转换 ──
    if action == "convert":
        if not timezone_str:
            return (
                "请提供目标时区，例如 America/New_York（纽约）"
                "或 Europe/London（伦敦）。"
            )
        if ZoneInfo is None:
            return "时区转换功能需要 Python 3.9+ 的 zoneinfo 模块，当前环境不支持。"
        try:
            target_tz = ZoneInfo(timezone_str)
            target_time = datetime.now(target_tz)
            wd = weekday_names[target_time.weekday()]
            return (
                f"{timezone_str} 当前时间：{target_time.year}年"
                f"{target_time.month}月{target_time.day}日 {wd} "
                f"{target_time.hour:02d}:{target_time.minute:02d}。"
            )
        except Exception:
            return (
                f'无法识别的时区"{timezone_str}"，请使用标准 IANA 时区名称，'
                f'如 America/New_York、Europe/London、Asia/Tokyo。'
            )

    return f"未知操作：{action}"


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
    {
        "type": "function",
        "function": {
            "name": "get_current_time",
            "description": (
                "获取时间相关信息。适用场景："
                "1) 倒数日计算——离某个日期还有多少天；"
                "2) 时区转换——某地现在几点；"
                "3) 查询某天是周几；"
                "4) 需要精确时间计算的复杂问题。"
                '注意：简单的"现在几点""今天几号"无需调用此工具，'
                "当前时间已在对话开头提供。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["now", "countdown", "weekday", "convert"],
                        "description": (
                            "now=当前详细时间（含星期、年内天数）；"
                            "countdown=倒数日，距target还有/已过多少天；"
                            "weekday=查询target是周几；"
                            "convert=将当前时间转换到timezone时区"
                        ),
                    },
                    "target": {
                        "type": "string",
                        "description": (
                            "目标日期。countdown/weekday 需要此参数。"
                            "格式如'2027-01-01'或'2027年1月1日'"
                        ),
                    },
                    "timezone": {
                        "type": "string",
                        "description": (
                            "目标时区（IANA名称）。convert需要此参数。"
                            "如'America/New_York'、'Europe/London'"
                        ),
                    },
                },
                "required": ["action"],
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

    elif name == "get_current_time":
        # 时间工具：now / countdown / weekday / convert
        action = arguments.get("action", "now")
        target = arguments.get("target", "")
        tz = arguments.get("timezone", "")
        result = _handle_get_current_time(action, target, tz)
        print(f"[Tool] get_current_time action={action!r} target={target!r} tz={tz!r}")
        return result

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
        # ★ 注入当前时间和用户所在地到 System Prompt
        system_with_time = SYSTEM_PROMPT + (
            f"\n\n当前时间：{_get_time_string()}"
            f"\n用户所在地：{LOCATION}"
        )
        response = await client.chat.completions.create(
            model=DEEPSEEK_MODEL,
            messages=[{"role": "system", "content": system_with_time}] + history,
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
