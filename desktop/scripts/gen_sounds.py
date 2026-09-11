"""把 frontend/src/lib/sound.ts 的合成参数离线复刻成 13 个 wav。

    python desktop/scripts/gen_sounds.py            # 生成到 desktop/resources/sounds/
    python desktop/scripts/gen_sounds.py --out DIR  # 只生成到别处（测试拿它比对幂等性）

为什么预生成而不是运行期合成（计划的定案）：
  · spike 实测 `QAudioSink.isFormatSupported` 在 PySide6 6.11 已不是静态方法，
    实时推流那套 API 面在漂；
  · 落子那一帧不该跑滤波器；生成期慢慢算，运行期只管放。

保真口径（说清楚哪部分是照抄、哪部分是近似）：
  · 波形 / 频率 / 滑音 / 时长 / 增益 / Attack / 截止频率 / Q 值 —— 逐项照抄
    sound.ts 的 tone() / stoneClick() / buzz() / melody()；
  · 包络用几何插值，对应 WebAudio 的 exponentialRampToValueAtTime；
  · **唯一的近似**：滤波器用 RBJ cookbook 形式的双二阶，WebAudio 规范另有自己的
    z 变换式。同为 Q=0.7 的低通削尖，听感等价，但不承诺逐样本相同。

噪声用的是固定种子的伪随机序列：同一个输入必须每次生成同一个文件，
否则"重新生成一遍 wav"这种无害操作会让 diff 里出现一堆二进制抖动。
"""
from __future__ import annotations

import argparse
import io
import math
import random
import struct
import wave
from pathlib import Path

SAMPLE_RATE = 44100
CHANNELS = 1
WIDTH = 2                      # 16-bit PCM：QSoundEffect 对这种格式最稳
# 默认输出位。**测试会断言它等于 core.paths.SOUNDS_DIR**，否则就是“生成了但地方不对”；
# 写在这里而不是埋在 argparse 里，就是为了它能被读到。
DEFAULT_OUT = Path(__file__).resolve().parent.parent / "resources" / "sounds"
ATTACK = 0.008                 # sound.ts: exponentialRampToValueAtTime(peak, t0 + 0.008)
FLOOR = 0.0001                 # 指数斜坡到不了 0，WebAudio 里也是用 0.0001 兜住
TAIL = 0.02                    # sound.ts: osc.stop(t0 + dur + 0.02)


# ---------------------------------------------------------------- 双二阶

def _biquad(xs, b0, b1, b2, a0, a1, a2):
    """直接 II 型转置写法；系数在这里归一化，调用方按 RBJ 原样给就行。"""
    b0, b1, b2 = b0 / a0, b1 / a0, b2 / a0
    a1, a2 = a1 / a0, a2 / a0
    out = []
    x1 = x2 = y1 = y2 = 0.0
    for x in xs:
        y = b0 * x + b1 * x1 + b2 * x2 - a1 * y1 - a2 * y2
        x2, y2 = x1, y1
        x1, y1 = x, y
        out.append(y)
    return out


def _w0(f0):
    # 截止频率超过奈奎斯特时 clamp：buzz() 里 cutoff = freq * 6 会撞到 44100/2
    f0 = min(max(f0, 1.0), SAMPLE_RATE / 2 - 1.0)
    return 2.0 * math.pi * f0 / SAMPLE_RATE


def lowpass(xs, f0, q=0.7):
    """sound.ts 里 lp.Q.value = 0.7 的理由：「Q 高了截止频率附近会嗡」，不加重共振。"""
    w = _w0(f0)
    alpha = math.sin(w) / (2.0 * q)
    cw = math.cos(w)
    return _biquad(xs, (1 - cw) / 2, 1 - cw, (1 - cw) / 2, 1 + alpha, -2 * cw, 1 - alpha)


def bandpass(xs, f0, q=1.1):
    """恒定峰值增益的带通：敲击瞬态过它之后只剩"那一声"的中心频段。"""
    w = _w0(f0)
    alpha = math.sin(w) / (2.0 * q)
    cw = math.cos(w)
    return _biquad(xs, alpha, 0.0, -alpha, 1 + alpha, -2 * cw, 1 - alpha)


# ---------------------------------------------------------------- 发声单元

def _geom(a, b, u):
    """几何插值，等价于 WebAudio 的指数斜坡。端点用 FLOOR 而不是 0 才不会 0**nan。"""
    return a * ((b / a) ** u)


class Mix:
    """一个音效的叠加缓冲。事件按 delay 落到同一根时间轴上。"""

    def __init__(self):
        self._buf: list[float] = []

    def _grow(self, n):
        if n > len(self._buf):
            self._buf.extend([0.0] * (n - len(self._buf)))

    def add(self, block, delay=0.0):
        if block is None:
            # 踩过一次：tone()/melody() 自己就会摆放，再套一层 add 传进去的就是 None
            raise TypeError("add() 要的是样本列表；tone() 类函数已经自己摆进 mix 了")
        off = int(round(delay * SAMPLE_RATE))
        self._grow(off + len(block))
        for i, s in enumerate(block):
            self._buf[off + i] += s

    def samples(self):
        n = len(self._buf) + int(round(TAIL * SAMPLE_RATE))
        return self._buf + [0.0] * (n - len(self._buf))


def tone(mix, freq, dur, delay=0.0, kind="sine", gain=0.5, glide_to=None, cutoff=None):
    """sound.ts 的 tone()：振荡器 → 指数包络 →（可选）低通 →增益，**然后按 delay 摆进 mix**。

    生成与摆放必须是同一个调用：第一版里 delay 传给了“只负责生成 block”的函数，
    而摆放用的是 `add(block)` 的默认 offset 0，结果所有事件全叠在 t=0 —— 音阶变成
    和弦、三声警告变成一声巨响，而且不报错、时长还短了一大截。
    """
    mix.add(_tone_block(freq, dur, kind, gain, glide_to, cutoff), delay)


def _tone_block(freq, dur, kind="sine", gain=0.5, glide_to=None, cutoff=None):
    n = int(round(dur * SAMPLE_RATE))
    block = [0.0] * n
    phase = 0.0
    for i in range(n):
        t = i / SAMPLE_RATE
        # 滑音走指数曲线，和 frequency.exponentialRampToValueAtTime 同一条路
        f = freq if not glide_to else freq * (glide_to / freq) ** (t / dur)
        phase += 2.0 * math.pi * f / SAMPLE_RATE
        # attack 不能省：增益从 0 瞬间跳到峰值会「咔」一声（sound.ts 原话）
        if t < ATTACK:
            env = _geom(FLOOR, gain, t / ATTACK)
        else:
            env = _geom(gain, FLOOR, (t - ATTACK) / max(dur - ATTACK, 1e-9))
        block[i] = osc_sample(kind, phase) * env
    if cutoff:
        block = lowpass(block, cutoff)
    return block


def osc_sample(kind, phase):
    """只实现 sound.ts 真的用到的两种波形；square/sawtooth 故意不给（它们就是旧警告音刺耳的原因）。"""
    if kind == "sine":
        return math.sin(phase)
    if kind == "triangle":
        # 归一化到 [-1,1] 的三角波。sound.ts 换掉方波就是为了那 1/n² 的谐波衰减。
        return (2.0 / math.pi) * math.asin(math.sin(phase))
    raise ValueError(f"未复刻的波形：{kind}")


def stone_transient(pitch, gain, seed):
    """棋子敲木板的第一层：指数衰减白噪声过 1750*pitch 的带通。无包络，本来就极短。"""
    dur = 0.09
    frames = int(SAMPLE_RATE * dur)
    rng = random.Random(seed)
    data = [(rng.random() * 2 - 1) * math.exp(-i / (frames * 0.12)) for i in range(frames)]
    data = bandpass(data, 1750 * pitch, 1.1)
    return [d * gain for d in data]


def stone_click(mix, pitch=1.0, gain=0.6, seed=1):
    """两层：敲击瞬态 + 190*pitch 的木头共鸣（triangle，短）。"""
    mix.add(stone_transient(pitch, gain, seed))
    tone(mix, 190 * pitch, 0.12, kind="triangle", gain=gain * 0.5)


NOTE = {                       # 音名 → 频率，与 sound.ts 的 NOTE 表同源
    "C3": 130.81, "D3": 146.83, "E3": 164.81, "F3": 174.61, "G3": 196.0, "A3": 220.0,
    "C4": 261.63, "D4": 293.66, "E4": 329.63, "F4": 349.23, "G4": 392.0, "A4": 440.0,
    "B4": 493.88, "C5": 523.25, "D5": 587.33, "E5": 659.25, "G5": 783.99, "A5": 880.0,
}


def melody(mix, notes, step, kind="triangle", gain=0.34):
    for i, name in enumerate(notes):
        tone(mix, NOTE.get(name, 440.0), step * 1.6, delay=i * step, kind=kind, gain=gain)


def buzz(mix, freq, dur, gain=0.35, pulses=2):
    """脉冲式蜂鸣：每声占步长 7 成、留 3 成空隙。靠"断续"而不是靠"粗糙"传达警告。"""
    step = dur / pulses
    for i in range(pulses):
        tone(mix, freq, step * 0.7, delay=i * step, kind="triangle",
             gain=gain, cutoff=freq * 6)


# ---------------------------------------------------------------- 13 个音效
# 每个函数与 sound.ts 的 synth() 里同名 case 一一对应；参数照抄，不做"我觉得更好听"的调整。

def build_stone(m):
    stone_click(m, 1.0, 0.62, seed=11)


def build_stone_ai(m):
    # 音色略低，便于闭眼分辨是谁下的
    stone_click(m, 0.86, 0.5, seed=12)


def build_capture(m):
    stone_click(m, 1.25, 0.4, seed=13)
    tone(m, 520, 0.16, delay=0.03, kind="triangle", gain=0.22, glide_to=260)


def build_win(m):
    melody(m, ["C4", "E4", "G4", "C5"], 0.11, "triangle", 0.34)


def build_lose(m):
    melody(m, ["E4", "D4", "C4"], 0.16, "sine", 0.3)


def build_resign(m):
    # AI 投子：低沉两下 + 木头轻响（刻意区别于"玩家输"的下行旋律）
    tone(m, 180, 0.28, kind="sine", gain=0.32, glide_to=96)
    tone(m, 140, 0.34, delay=0.2, kind="sine", gain=0.26, glide_to=70)
    stone_click(m, 0.7, 0.3, seed=17)


def build_timeout(m):
    # 超时是重要警告：三声、每声间隔清楚，但音高降到 G3、音量调低
    buzz(m, 196, 0.54, 0.26, 3)


def build_promote(m):
    melody(m, ["C4", "E4", "G4", "C5", "E5"], 0.1, "triangle", 0.36)
    tone(m, NOTE["C5"], 0.5, delay=0.5, kind="sine", gain=0.2)


def build_demote(m):
    melody(m, ["A4", "G4", "E4", "D4"], 0.15, "sine", 0.28)


def build_correct(m):
    melody(m, ["E5", "A5"], 0.09, "triangle", 0.3)


def build_wrong(m):
    # 答错会在试错时反复触发，所以比超时更短更轻
    buzz(m, 165, 0.26, 0.2, 2)


def build_click(m):
    # 按钮音触发最频繁，不能尖：pitch 1.35 → 带通约 2360Hz
    stone_click(m, 1.35, 0.2, seed=23)


def build_review(m):
    melody(m, ["G4", "C5"], 0.12, "sine", 0.24)


BUILDERS = {
    "stone": build_stone,
    "stoneAi": build_stone_ai,
    "capture": build_capture,
    "win": build_win,
    "lose": build_lose,
    "resign": build_resign,
    "timeout": build_timeout,
    "promote": build_promote,
    "demote": build_demote,
    "correct": build_correct,
    "wrong": build_wrong,
    "click": build_click,
    "review": build_review,
}


def render(name: str) -> bytes:
    """一个音效 → 16-bit 单声道 PCM 的完整 wav 字节。"""
    if name not in BUILDERS:
        raise KeyError(f"sound.ts 里没有这个音效：{name}")
    mix = Mix()
    BUILDERS[name](mix)
    xs = mix.samples()
    peak = max(abs(s) for s in xs) or 1.0
    # 多事件叠加可能超界。只在真会削平时整体等比缩，不逐样本 clamp —— clamp 是削波失真。
    scale = min(1.0, 0.99 / peak)
    buf = b"".join(
        struct.pack("<h", int(max(-1.0, min(1.0, s * scale)) * 32767)) for s in xs
    )
    bio = io.BytesIO()
    with wave.open(bio, "wb") as w:
        w.setnchannels(CHANNELS)
        w.setsampwidth(WIDTH)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(buf)
    return bio.getvalue()


def main(out_dir: Path) -> int:
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"{'名称':<10}{'时长':>8}{'峰值':>8}  字节")
    for name in BUILDERS:
        data = render(name)
        with wave.open(io.BytesIO(data), "rb") as w:    # 回读一遍，确认头没写坏
            assert w.getnchannels() == 1 and w.getsampwidth() == 2 and w.getframerate() == SAMPLE_RATE
            secs = w.getnframes() / w.getframerate()
        path = out_dir / f"{name}.wav"
        path.write_bytes(data)
        print(f"{name:<10}{secs:7.2f}s{peak_of(data):8.3f}  {len(data)}")
    return 0


def peak_of(data: bytes) -> float:
    n = (len(data) - 44) // 2
    vals = struct.unpack(f"<{n}h", data[44:44 + n * 2])
    return max(abs(v) for v in vals) / 32767.0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="生成桌面端音效资源")
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    raise SystemExit(main(Path(ap.parse_args().out)))
