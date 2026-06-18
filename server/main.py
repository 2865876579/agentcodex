"""
智能枕头云端服务 - WebSocket 主入口

整体架构：
  ESP32（录音/播放） <--WebSocket--> 本服务（STT+LLM+TTS） <--WebSocket--> PC Agent（控制电脑）

本服务负责：
  1. 接收 ESP32 上传的音频，调用讯飞 STT 转成文字
  2. 文字送 DeepSeek LLM，返回回复文本 + 可选的电脑控制命令
  3. 回复文本走 Edge TTS 合成语音，回传 ESP32 播放
  4. 如果 LLM 返回了电脑控制命令，转发给已连接的 PC Agent 执行

WebSocket 端点：
  /ws/esp32     - ESP32 设备连接入口
  /ws/pc_agent  - PC Agent 连接入口
  /health       - HTTP 健康检查
"""
import sys
import json
import asyncio
import base64
import re
import opuslib

# Windows 控制台默认 GBK，强制 UTF-8 避免 emoji 打印崩溃
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if sys.stderr.encoding and sys.stderr.encoding.lower() != "utf-8":
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
import time
import urllib.parse
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from config import SERVER_HOST, SERVER_PORT
from stt_xunfei import recognize
from llm_deepseek import chat
from tts_edge import synthesize
from web_search import direct_answer_from_results, format_search_results, search_web  # 搜索工具，供后续 function calling 工具接入时使用

app = FastAPI(title="Smart Pillow Cloud Server")
APP_VERSION = "continuous_dialog_v4_binary_tts"

WAKE_TRIGGER_TEXT = "__wake__"
WAKE_REPLY_TEXT = "我在，你说。"
EXIT_REPLY_TEXT = "好的，再见小智。"
EXIT_PHRASES = (
    "再见小智",
    "再见小知",
    "小智再见",
    "拜拜小智",
    "退出对话",
    "结束对话",
)

PCM_FRAME_BYTES = 1280
OPUS_SAMPLE_RATE = 16000
OPUS_CHANNELS = 1
OPUS_FRAME_SAMPLES = 960

# 存储已连接的 PC Agent，key 是连接 id，value 是 WebSocket 对象
# 当 LLM 返回电脑控制命令时，会从这里取一个 Agent 转发命令
pc_agents: dict[str, WebSocket] = {}

# 存储已连接的 ESP32 客户端，PC Agent 回传结果时用于播报到设备。
esp32_clients: dict[str, WebSocket] = {}
esp32_send_locks: dict[str, asyncio.Lock] = {}
esp32_sessions: dict[str, dict] = {}
pc_command_contexts: dict[str, dict] = {}
last_active_esp32_id: str | None = None


def is_exit_phrase(text: str) -> bool:
    normalized = re.sub(r"[\s，。！？、,.!?~～]", "", text or "")
    return any(phrase in normalized for phrase in EXIT_PHRASES)


async def send_json_to_esp32(client_id: str, payload: dict) -> bool:
    """串行发送一条 JSON 消息到指定 ESP32，避免多任务并发写同一 WebSocket。"""
    websocket = esp32_clients.get(client_id)
    lock = esp32_send_locks.get(client_id)
    if websocket is None or lock is None:
        return False

    async with lock:
        await websocket.send_text(json.dumps(payload, ensure_ascii=False))
    return True


async def send_tts_stream_to_esp32(
    client_id: str,
    text: str,
    *,
    source: str = "assistant",
    turn_id: int | None = None,
    end_dialog: bool = False,
) -> bool:
    """
    TTS 音频流式发送到 ESP32（binary frame + Opus 编码，对齐小智协议）。

    协议：
      - tts_audio_start：text JSON 帧，含元数据
      - [binary frame]：Opus 编码音频（每帧 60ms）
      - tts_audio_end：text JSON 帧，结束标记
    """
    websocket = esp32_clients.get(client_id)
    lock = esp32_send_locks.get(client_id)
    if websocket is None or lock is None:
        return False
    if source == "assistant" and not is_current_turn(client_id, turn_id):
        return False

    # 第一步：立即发送 start（锁内），让 ESP32 知道即将有音频
    async with lock:
        if source == "assistant" and not is_current_turn(client_id, turn_id):
            return False

        print(f"[TTS-send] 发送 tts_audio_start, text_len={len(text)}")
        await websocket.send_text(json.dumps({
            "type": "tts_audio_start",
            "codec": "opus",
            "sample_rate": 16000,
            "channels": 1,
            "frame_duration_ms": 60,
            "text": text,
            "source": source,
            "turn_id": turn_id
        }, ensure_ascii=False))

    # 第二步：后台合成 TTS，Opus 帧一产出就发送，减少合成后的等待
    queue: asyncio.Queue = asyncio.Queue()

    async def _synth_to_queue():
        try:
            async for frame in synthesize(text):
                if frame:
                    await queue.put(frame)
        finally:
            await queue.put(None)  # 完成信号

    async def _send_payload(payload) -> bool:
        async with lock:
            if source == "assistant" and not is_current_turn(client_id, turn_id):
                return False
            await websocket.send_text(json.dumps(payload, ensure_ascii=False))
        return True

    async def _send_binary_frame(frame: bytes) -> bool:
        async with lock:
            if source == "assistant" and not is_current_turn(client_id, turn_id):
                return False
            await websocket.send_bytes(frame)
        return True

    synth_task = asyncio.create_task(_synth_to_queue())
    seq = 0

    try:
        while True:
            get_task = asyncio.create_task(queue.get())
            try:
                frame = await asyncio.wait_for(get_task, timeout=0.3)
            except asyncio.TimeoutError:
                get_task.cancel()
                # 300ms 无帧 → 发心跳保活
                if not await _send_payload({"type": "ping"}):
                    return False
                continue

            if frame is None:
                break

            seq += 1
            if not await _send_binary_frame(frame):
                return False
    finally:
        if not synth_task.done():
            synth_task.cancel()

    if source == "assistant" and not is_current_turn(client_id, turn_id):
        return False

    if seq <= 0:
        return False

    if not await _send_payload({
        "type": "tts_audio_end",
        "codec": "opus",
        "text": text,
        "chunks": seq,
        "source": source,
        "turn_id": turn_id,
        "transport": "binary",
        "dialog_end": end_dialog
    }):
        return False

    print(f"[TTS-send] 全部完成, {seq} 帧")
    return True

async def send_pc_command(pc_command: dict, client_id: str, turn_id: int) -> bool:
    """把 LLM 产生的电脑控制命令转发给任意一个已连接的 PC Agent。"""
    if not pc_agents:
        return False

    command_id = f"{client_id}:{turn_id}:{int(time.time() * 1000)}"
    pc_command_contexts[command_id] = {
        "client_id": client_id,
        "turn_id": turn_id,
        "action": pc_command.get("action"),
        "created_at": time.monotonic(),
    }

    agent_ws = next(iter(pc_agents.values()))
    await agent_ws.send_text(json.dumps({
        "type": "pc_command",
        "client_id": client_id,
        "turn_id": turn_id,
        "command_id": command_id,
        "command": pc_command
    }, ensure_ascii=False))
    return True


def next_turn_id(client_id: str) -> int:
    session = esp32_sessions.setdefault(client_id, {"turn_id": 0})
    session["turn_id"] = int(session.get("turn_id", 0)) + 1
    return session["turn_id"]


def is_current_turn(client_id: str, turn_id: int | None) -> bool:
    if turn_id is None:
        return True
    session = esp32_sessions.get(client_id) or {}
    return int(session.get("turn_id", -1)) == int(turn_id)


def pick_esp32_client(client_id: str | None = None) -> str | None:
    """优先选择指定客户端，其次选择最近活跃客户端，最后选择任意在线 ESP32。"""
    if client_id and client_id in esp32_clients:
        return client_id
    if last_active_esp32_id and last_active_esp32_id in esp32_clients:
        return last_active_esp32_id
    return next(iter(esp32_clients.keys()), None)


def extract_search_query_from_url(url: str) -> str | None:
    """Return query text if URL is a search-engine result page."""
    parsed = urllib.parse.urlparse(url)
    host = parsed.netloc.lower()
    params = urllib.parse.parse_qs(parsed.query)

    search_param_names: tuple[str, ...] | None = None
    if "baidu.com" in host:
        search_param_names = ("wd", "word", "q")
    elif "bing.com" in host:
        search_param_names = ("q",)
    elif "google." in host:
        search_param_names = ("q",)
    elif "duckduckgo.com" in host:
        search_param_names = ("q",)
    elif "sogou.com" in host:
        search_param_names = ("query", "keyword", "q")
    elif "so.com" in host or "haosou.com" in host:
        search_param_names = ("q",)

    if not search_param_names:
        return None

    for name in search_param_names:
        values = params.get(name)
        if values and values[0].strip():
            return urllib.parse.unquote_plus(values[0]).strip()
    return None



async def handle_ai_result(client_id: str, user_text: str, result: dict, history: list[dict], turn_id: int) -> None:
    """处理 LLM 返回结果：执行 pc_command（如有），播报语音回复。"""
    if not is_current_turn(client_id, turn_id):
        return

    reply = result.get("reply", "")
    pc_command = result.get("pc_command")
    print(f"[LLM] reply={reply}, pc_cmd={pc_command}")

    if pc_command:
        sent = await send_pc_command(pc_command, client_id, turn_id)
        if not sent:
            await send_json_to_esp32(client_id, {
                "type": "status",
                "msg": "没有在线 PC Agent，无法执行电脑控制命令"
            })

    if reply:
        await send_tts_stream_to_esp32(client_id, reply, turn_id=turn_id)


async def cancel_active_task(client_id: str) -> None:
    session = esp32_sessions.get(client_id) or {}
    task = session.get("active_task")
    if not task or task.done():
        return

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception as exc:
        print(f"[Task] 取消旧任务时发现异常: {exc}")


async def process_text_turn(client_id: str, text: str, history: list[dict], turn_id: int) -> None:
    try:
        if text == WAKE_TRIGGER_TEXT:
            await send_tts_stream_to_esp32(client_id, WAKE_REPLY_TEXT, turn_id=turn_id)
            return

        if is_exit_phrase(text):
            await send_tts_stream_to_esp32(
                client_id, EXIT_REPLY_TEXT, turn_id=turn_id, end_dialog=True
            )
            return

        result = await chat(text, history)
        if not is_current_turn(client_id, turn_id):
            return
        await handle_ai_result(client_id, text, result, history, turn_id)
    except asyncio.CancelledError:
        print(f"[Task] 旧文本任务已取消: turn_id={turn_id}")
        raise
    except Exception as e:
        print(f"[ERROR] 处理文本消息出错: {e}")
        import traceback
        traceback.print_exc()
        if is_current_turn(client_id, turn_id):
            try:
                await send_json_to_esp32(client_id, {
                    "type": "status",
                    "msg": f"处理出错: {str(e)[:100]}",
                    "turn_id": turn_id
                })
            except Exception:
                pass


def split_pcm_frames(audio_bytes: bytes) -> list[bytes]:
    return [
        audio_bytes[i:i + PCM_FRAME_BYTES]
        for i in range(0, len(audio_bytes), PCM_FRAME_BYTES)
        if audio_bytes[i:i + PCM_FRAME_BYTES]
    ]


async def process_audio_frames_turn(client_id: str, frames: list[bytes], history: list[dict], turn_id: int, source: str) -> None:
    try:
        total_bytes = sum(len(frame) for frame in frames)
        print(f"[Audio] {source} turn_id={turn_id}, frames={len(frames)}, bytes={total_bytes}")
        if not frames:
            await send_json_to_esp32(client_id, {
                "type": "status",
                "msg": "没收到音频，请再说一次",
                "turn_id": turn_id
            })
            return

        text = await recognize(frames)
        if not is_current_turn(client_id, turn_id):
            return
        print(f"[STT] result_len={len(text.strip())}, text={text!r}")
        if not text.strip():
            await send_json_to_esp32(client_id, {
                "type": "status",
                "msg": "没听清，请再说一次",
                "turn_id": turn_id
            })
            return

        print(f"[STT] {text}")
        await send_json_to_esp32(client_id, {
            "type": "stt_result",
            "text": text,
            "turn_id": turn_id
        })

        if is_exit_phrase(text):
            await send_tts_stream_to_esp32(
                client_id, EXIT_REPLY_TEXT, turn_id=turn_id, end_dialog=True
            )
            return

        result = await chat(text, history)
        if not is_current_turn(client_id, turn_id):
            return
        await handle_ai_result(client_id, text, result, history, turn_id)
    except asyncio.CancelledError:
        print(f"[Task] 旧语音任务已取消: turn_id={turn_id}")
        raise
    except Exception as e:
        print(f"[ERROR] 处理语音消息出错: {e}")
        import traceback
        traceback.print_exc()
        if is_current_turn(client_id, turn_id):
            try:
                await send_json_to_esp32(client_id, {
                    "type": "status",
                    "msg": f"处理出错: {str(e)[:100]}",
                    "turn_id": turn_id
                })
            except Exception:
                pass


async def process_audio_turn(client_id: str, audio_b64: str, history: list[dict], turn_id: int) -> None:
    print(f"[Audio] received legacy turn_id={turn_id}, b64_len={len(audio_b64)}")
    audio_bytes = base64.b64decode(audio_b64)
    frames = split_pcm_frames(audio_bytes)
    await process_audio_frames_turn(client_id, frames, history, turn_id, "legacy_pcm")


@app.websocket("/ws/esp32")
async def esp32_endpoint(websocket: WebSocket):
    """
    ESP32 设备的 WebSocket 连接入口

    支持两种消息类型：
    1. {"type": "text", "text": "用户说的话"}
       - 文字模式，跳过 STT，直接送 LLM（用于调试和测试）

    2. {"type": "audio", "audio": "base64编码的PCM音频"}
       - 语音模式，先 STT 转文字，再送 LLM
       - 音频格式要求：16kHz 采样率，16bit，单声道，PCM 原始数据

    3. {"type": "ping"} - 心跳保活

    返回消息类型：
    - {"type": "tts_audio_start", "format": "mp3", "text": "回复文字"}
    - {"type": "tts_audio_chunk", "format": "mp3", "seq": 1, "audio": "base64音频块"}
    - {"type": "tts_audio_end", "format": "mp3", "text": "回复文字", "chunks": 12}
    - {"type": "stt_result", "text": "识别出的文字"}
    - {"type": "status", "msg": "状态提示"}
    - {"type": "pong"}
    """
    global last_active_esp32_id

    await websocket.accept()
    client_id = str(id(websocket))
    esp32_clients[client_id] = websocket
    esp32_send_locks[client_id] = asyncio.Lock()
    esp32_sessions[client_id] = {"turn_id": 0}
    last_active_esp32_id = client_id
    history: list[dict] = []
    print(f"[ESP32] 已连接 ({client_id})")

    try:
        while True:
            # 等待 ESP32 发来的消息：text JSON 控制帧 + binary Opus 音频帧
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                raise WebSocketDisconnect
            last_active_esp32_id = client_id

            try:
                if "bytes" in message and message["bytes"] is not None:
                    incoming = esp32_sessions[client_id].get("incoming_audio")
                    if not incoming:
                        print(f"[ESP32] drop binary audio without audio_start, bytes={len(message['bytes'])}")
                        continue
                    pcm = incoming["decoder"].decode(message["bytes"], OPUS_FRAME_SAMPLES)
                    incoming["pcm"].extend(pcm)
                    incoming["chunks"] += 1
                    incoming["opus_bytes"] += len(message["bytes"])
                    continue

                if "text" not in message or message["text"] is None:
                    continue

                data = json.loads(message["text"])

                # ========== 文字模式（调试用，跳过 STT）==========
                if data.get("type") == "text":
                    text = str(data["text"]).strip()
                    if not text:
                        continue
                    turn_id = next_turn_id(client_id)
                    await cancel_active_task(client_id)
                    esp32_sessions[client_id].pop("incoming_audio", None)
                    print(f"[Text] {text}")

                    task = asyncio.create_task(process_text_turn(client_id, text, history, turn_id))
                    esp32_sessions[client_id]["active_task"] = task

                # ========== 兼容旧 PCM/base64 语音消息 ==========
                elif data.get("type") == "audio":
                    audio_b64 = data["audio"]
                    turn_id = next_turn_id(client_id)
                    await cancel_active_task(client_id)
                    esp32_sessions[client_id].pop("incoming_audio", None)
                    print(f"[ESP32] legacy audio turn_id={turn_id}, b64_len={len(audio_b64)}")

                    task = asyncio.create_task(process_audio_turn(client_id, audio_b64, history, turn_id))
                    esp32_sessions[client_id]["active_task"] = task

                # ========== 新 Opus 上传：开始 ==========
                elif data.get("type") == "audio_start":
                    turn_id = next_turn_id(client_id)
                    await cancel_active_task(client_id)
                    esp32_sessions[client_id]["incoming_audio"] = {
                        "turn_id": turn_id,
                        "pcm": bytearray(),
                        "decoder": opuslib.Decoder(OPUS_SAMPLE_RATE, OPUS_CHANNELS),
                        "chunks": 0,
                        "opus_bytes": 0,
                    }
                    print(f"[ESP32] opus audio_start turn_id={turn_id}")

                # ========== 新 Opus 上传：结束 ==========
                elif data.get("type") == "audio_end":
                    incoming = esp32_sessions[client_id].pop("incoming_audio", None)
                    if not incoming:
                        print("[ESP32] audio_end without audio_start")
                        continue
                    turn_id = int(incoming["turn_id"])
                    pcm_bytes = bytes(incoming["pcm"])
                    frames = split_pcm_frames(pcm_bytes)
                    print(
                        f"[ESP32] opus audio_end turn_id={turn_id}, "
                        f"chunks={incoming['chunks']}, opus_bytes={incoming['opus_bytes']}, pcm_bytes={len(pcm_bytes)}"
                    )
                    task = asyncio.create_task(process_audio_frames_turn(client_id, frames, history, turn_id, "opus"))
                    esp32_sessions[client_id]["active_task"] = task

                # ========== 心跳 ==========
                elif data.get("type") == "ping":
                    await send_json_to_esp32(client_id, {"type": "pong"})

            except Exception as e:
                print(f"[ERROR] 处理消息出错: {e}")
                import traceback
                traceback.print_exc()
                try:
                    await send_json_to_esp32(client_id, {
                        "type": "status",
                        "msg": f"处理出错: {str(e)[:100]}"
                    })
                except Exception:
                    pass

    except WebSocketDisconnect:
        print(f"[ESP32] 已断开 ({client_id})")
    finally:
        try:
            await cancel_active_task(client_id)
        except Exception:
            pass
        esp32_clients.pop(client_id, None)
        esp32_send_locks.pop(client_id, None)
        esp32_sessions.pop(client_id, None)
        for command_id, context in list(pc_command_contexts.items()):
            if context.get("client_id") == client_id:
                pc_command_contexts.pop(command_id, None)
        if last_active_esp32_id == client_id:
            last_active_esp32_id = pick_esp32_client()


@app.websocket("/ws/pc_agent")
async def pc_agent_endpoint(websocket: WebSocket):
    """
    PC Agent 的 WebSocket 连接入口

    PC 端运行一个 Agent 程序，连接到这里等待命令。
    当 LLM 判断用户想控制电脑时，命令会通过这个连接下发。

    PC Agent 发来的消息类型：
    - {"type": "result", "result": "执行结果文字"}  执行完毕后返回结果
    - {"type": "ping"}  心跳

    服务端下发的消息类型：
    - {"type": "pc_command", "command": {"action": "...", "params": {...}}}
    - {"type": "pong"}
    """
    await websocket.accept()
    agent_id = str(id(websocket))
    pc_agents[agent_id] = websocket
    print(f"[PC Agent] 已连接 ({agent_id})")

    try:
        while True:
            message = await websocket.receive_text()
            data = json.loads(message)

            # PC Agent 执行完命令后返回结果
            if data.get("type") == "result":
                result_text = data.get("result", "")
                command_id = data.get("command_id")
                result_turn_id = data.get("turn_id")
                print(f"[PC Agent] 执行结果: {result_text}")

                context = pc_command_contexts.pop(command_id, None) if command_id else None
                target_id = pick_esp32_client(data.get("client_id") or (context or {}).get("client_id"))
                action = (context or {}).get("action")
                current_turn_id = (esp32_sessions.get(target_id or "", {}) or {}).get("turn_id")
                age = time.monotonic() - float((context or {}).get("created_at", time.monotonic()))
                is_current = (
                    target_id
                    and result_turn_id is not None
                    and current_turn_id is not None
                    and int(result_turn_id) == int(current_turn_id)
                    and age <= 8
                )

                if target_id and result_text:
                    if action in ("open_url", "open_file"):
                        print(f"[ESP32] PC Agent {action} 结果静音，不打断对话 -> {target_id}")
                    elif action == "summarize_file" and is_current:
                        sent = await send_tts_stream_to_esp32(
                            target_id,
                            result_text,
                            source="pc_result",
                            turn_id=int(result_turn_id)
                        )
                        if sent:
                            print(f"[ESP32] 已播报 PC Agent 结果 -> {target_id}")
                    else:
                        await send_json_to_esp32(target_id, {
                            "type": "status",
                            "msg": result_text,
                            "source": "pc_result",
                            "turn_id": result_turn_id
                        })
                        print(f"[ESP32] PC Agent 结果仅状态提示，不语音插队 -> {target_id}")
                elif not target_id:
                    print("[ESP32] 没有在线客户端，无法播报 PC Agent 结果")

            elif data.get("type") == "ping":
                await websocket.send_text(json.dumps({"type": "pong"}, ensure_ascii=False))

    except WebSocketDisconnect:
        pc_agents.pop(agent_id, None)
        print(f"[PC Agent] 已断开 ({agent_id})")


@app.get("/health")
async def health():
    """健康检查接口，用于确认服务是否在线"""
    return {
        "status": "ok",
        "esp32_connected": bool(esp32_clients),
        "esp32_clients": len(esp32_clients),
        "pc_agents": len(pc_agents)
    }


if __name__ == "__main__":
    import uvicorn
    # 启动 WebSocket 服务，默认监听 0.0.0.0:8000
    print(f"[ESPAgent] version={APP_VERSION}")
    uvicorn.run(
        app,
        host=SERVER_HOST,
        port=int(SERVER_PORT),
        ws_ping_interval=30,
        ws_ping_timeout=120
    )
