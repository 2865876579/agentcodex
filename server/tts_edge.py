"""
Edge TTS 文字转语音模块 —— Opus 编码版

输出格式：Opus 压缩帧（60ms/帧，16kHz 单声道）
每个 Opus 帧约 80-120 字节，通过 WebSocket binary frame 直接发送。

对齐小智（xiaozhi-esp32）架构：binary frame + Opus 压缩。
"""
from collections.abc import AsyncIterator

import edge_tts
import miniaudio
import opuslib
from config import TTS_VOICE, TTS_RATE, TTS_VOLUME

# Opus 参数
SAMPLE_RATE = 16000
CHANNELS = 1
FRAME_DURATION_MS = 60
FRAME_SAMPLES = SAMPLE_RATE * FRAME_DURATION_MS // 1000  # 960 samples


async def synthesize(text: str) -> AsyncIterator[bytes]:
    """
    文字转语音流（Opus 编码）

    产出：Opus 编码音频帧（每帧 60ms，约 80-120 字节）
    """
    import time as _time
    _t0 = _time.monotonic()
    try:
        # 1. Edge TTS → MP3（纯文本 + 配置语速/音量，不用 SSML 避免解析异常）
        print(f"[TTS] Step1: Edge TTS 开始, voice={TTS_VOICE}, rate={TTS_RATE}, volume={TTS_VOLUME}")
        communicate = edge_tts.Communicate(text, TTS_VOICE, rate=TTS_RATE, volume=TTS_VOLUME)
        mp3_chunks: list[bytes] = []
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                mp3_chunks.append(chunk["data"])
        _t1 = _time.monotonic()
        print(f"[TTS] Step1: Edge TTS 完成, chunks={len(mp3_chunks)},耗时={_t1-_t0:.1f}s")

        if not mp3_chunks:
            print("[TTS] Step1: Edge TTS 返回空音频!")
            return

        # 2. MP3 → PCM（16kHz 16bit mono）
        print(f"[TTS] Step2: MP3→PCM 解码开始, mp3_size={sum(len(c) for c in mp3_chunks)}")
        mp3_data = b"".join(mp3_chunks)
        decoded = miniaudio.decode(
            mp3_data,
            output_format=miniaudio.SampleFormat.SIGNED16,
            nchannels=CHANNELS,
            sample_rate=SAMPLE_RATE,
        )
        pcm_bytes = bytes(decoded.samples)
        _t2 = _time.monotonic()
        print(f"[TTS] Step2: MP3→PCM 完成, pcm_samples={len(pcm_bytes)//2},耗时={_t2-_t1:.1f}s")

        # 3. PCM → Opus（60ms 帧）
        print(f"[TTS] Step3: Opus 编码开始")
        encoder = opuslib.Encoder(SAMPLE_RATE, CHANNELS, opuslib.APPLICATION_VOIP)

        total_samples = len(pcm_bytes) // 2  # int16 samples
        frame_count = 0
        offset = 0
        while offset < total_samples:
            remaining = total_samples - offset
            if remaining >= FRAME_SAMPLES:
                # 完整 60ms 帧
                frame_samples = FRAME_SAMPLES
                frame_bytes = FRAME_SAMPLES * 2
            else:
                # 最后一帧不足 60ms，补零
                frame_samples = remaining
                frame_bytes = remaining * 2
                pcm_bytes += b"\x00" * ((FRAME_SAMPLES * 2) - frame_bytes)

            raw_frame = pcm_bytes[offset * 2 : offset * 2 + FRAME_SAMPLES * 2]
            # 如果帧短于标准长度（最后帧），padding 到 960 samples
            if len(raw_frame) < FRAME_SAMPLES * 2:
                raw_frame = raw_frame + b"\x00" * (FRAME_SAMPLES * 2 - len(raw_frame))

            opus_frame = encoder.encode(raw_frame, FRAME_SAMPLES)
            frame_count += 1
            yield opus_frame

            offset += frame_samples

        _t3 = _time.monotonic()
        print(f"[TTS] Step3: Opus 编码完成, frames={frame_count},耗时={_t3-_t2:.1f}s,总耗时={_t3-_t0:.1f}s")

    except Exception as e:
        import traceback
        print(f"[TTS] 失败: {e}")
        traceback.print_exc()
        return
