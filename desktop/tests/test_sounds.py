"""音效资源与播放层的验收。

计划口径：「`scripts/gen_sounds.py` 生成 13 个 wav（名称严格对齐 `sound.ts` 的
`SoundName` 联合类型）」+「`QSoundEffect` 播放；不做实时合成」。
（`sound.ts` 随旧网页端于 2026-09-08 下线，名单冻结在下面的 `SOUND_TS_FROZEN`。）

音频没法"看截图"，所以这一层的等价物是**从 wav 字节里量出来的结构**：
时长、峰值、是否静音、13 个是否互不相同、包络有几声、音高随时间升还是降。
最后两条是照着"如果 delay 掉了会怎样"设计的 —— 写生成脚本时真犯过这个错
（所有事件叠在 t=0，音阶变和弦、三声警告变一声），而那时长只从 0.51s 变 0.15s、
**一点异常都不抛**。只查"有没有声音"的测试对这种 bug 完全没有检出力。
"""
from __future__ import annotations

import struct
import subprocess
import sys
import wave
from pathlib import Path

import pytest

from core import paths
from core import sound as S
from core.settings import Prefs

DESKTOP = Path(__file__).resolve().parent.parent
SCRIPT = DESKTOP / "scripts" / "gen_sounds.py"
SR = 44100

# 旧网页端 `frontend/src/lib/sound.ts` 已于 2026-09-08 下线删除（L26 关闭）。这份名单是它
# 下线那一刻 `SoundName` 联合类型的**冻结快照**，逐字保序抄自该文件（含 13 条，无增删）。
#
# 为什么冻结而不是删掉这条契约：改音效名 / 加音效时桌面端仍要红，只是基准从「读网页端源码」
# 换成「读这份快照」。要取回原文件核对：`git show <网页端下线前的提交>:frontend/src/lib/sound.ts`
# （首个提交 ebd8fab 里它还在）。
SOUND_TS_FROZEN: tuple[str, ...] = (
    "stone", "stoneAi", "capture", "win", "lose", "resign", "timeout",
    "promote", "demote", "correct", "wrong", "click", "review",
)


# ---------------------------------------------------------------- 读 wav

def _decode(name: str) -> list[float]:
    """一个音效 → [-1,1] 的浮点样本序列。"""
    with wave.open(str(paths.SOUNDS_DIR / f"{name}.wav"), "rb") as w:
        assert w.getnchannels() == 1, f"{name} 不是单声道：QSoundEffect 对立体声更挑格式"
        assert w.getsampwidth() == 2, f"{name} 不是 16-bit：{w.getsampwidth()} 字节"
        assert w.getframerate() == SR, f"{name} 采样率是 {w.getframerate()}"
        raw = w.readframes(w.getnframes())
    vals = struct.unpack(f"<{len(raw) // 2}h", raw)
    return [v / 32768.0 for v in vals]


def _rms_windows(xs, size=256):
    out = []
    for i in range(0, len(xs) - size, size):
        chunk = xs[i:i + size]
        out.append((sum(v * v for v in chunk) / size) ** 0.5)
    return out


def _segments(xs, ratio=0.10, size=256):
    """包络里有几"声"：连续高于阈值的窗口算一段。"""
    windows = _rms_windows(xs, size)
    peak = max(windows) if windows else 0.0
    thr = peak * ratio
    count, inside = 0, False
    for w in windows:
        if w >= thr and not inside:
            count += 1
            inside = True
        elif w < thr:
            inside = False
    return count, len(windows), peak


def _pitch_windows(xs, size=512):
    """用**过零率**当音高代理：每窗口过零次数 ≈ 2 * freq * size / SR。

    不用 FFT：这里只要判"后面比前面高/低"，过零率够硬且没有窗函数带来的歧义。
    """
    out = []
    for i in range(0, len(xs) - size, size):
        chunk = xs[i:i + size]
        crossings = sum(1 for a, b in zip(chunk, chunk[1:]) if (a < 0) != (b < 0))
        out.append((crossings, (sum(v * v for v in chunk) / size) ** 0.5))
    return out


def _voiced_pitch_trend(xs):
    """取"有声音"的第一批与最后一批窗口的过零率均值，返回 (前, 后)。"""
    ps = [(c, r) for c, r in _pitch_windows(xs) if r > 0.02]
    assert len(ps) >= 6, f"有效窗口太少，比不出趋势：{len(ps)}"
    k = max(1, len(ps) // 4)
    head = sum(c for c, _ in ps[:k]) / k
    tail = sum(c for c, _ in ps[-k:]) / k
    return head, tail


# ---------------------------------------------------------------- 名单契约

def _load_gen_sounds():
    """scripts/ 不是包，手工加一下 sys.path 再 import。"""
    sys.path.insert(0, str(SCRIPT.parent))
    try:
        import gen_sounds
        return gen_sounds
    finally:
        sys.path.pop(0)


def test_names_match_frozen_sound_ts_exactly_including_order():
    """与冻结的网页端名单逐字同序（原测试：直接解析 sound.ts）。

    抄一份 13 个名字的列表只能证明「两处手抄一致」；冻结快照把它变成
    「相对历史基准改没改」—— 改名的红仍是有效的，只是基准不能再被上游自动跟走。
    """
    frozen = list(SOUND_TS_FROZEN)
    assert len(frozen) == 13, frozen
    assert list(S.SOUND_NAMES) == frozen
    # 生成脚本里也真实现了这 13 个，不是只登记了名字
    assert list(_load_gen_sounds().BUILDERS) == frozen


def test_generator_writes_where_the_player_looks():
    """脚本默认输出位必须就是 `paths.SOUNDS_DIR`，否则是“生成了但地方不对”。

    这条是打包缝的一部分：改 paths.py 时两边要一起动，靠这条红来提醒。
    """
    gen = _load_gen_sounds()
    assert gen.DEFAULT_OUT == paths.SOUNDS_DIR, f"{gen.DEFAULT_OUT} != {paths.SOUNDS_DIR}"
    assert paths.SOUNDS_DIR == DESKTOP / "resources" / "sounds"


# ---------------------------------------------------------------- 资源本身

@pytest.mark.parametrize("name", S.SOUND_NAMES)
def test_wav_is_real_audible_audio(name):
    xs = _decode(name)
    secs = len(xs) / SR
    assert 0.05 <= secs <= 2.0, f"{name} 时长 {secs:.2f}s 不合理"
    peak = max(abs(v) for v in xs)
    assert peak > 0.05, f"{name} 峰值只有 {peak:.4f}，等于没声音"
    assert peak <= 1.0, f"{name} 峰值 {peak:.3f} 超界，会削波"
    energy = sum(v * v for v in xs) / len(xs)
    assert energy > 1e-5, f"{name} 几乎是静音：RMS {energy ** 0.5:.5f}"


def test_all_thirteen_are_different_sounds():
    """13 个文件互不相同。防的是"复制粘贴同一份 wav 改名交差"这种交付。"""
    digests = {}
    for name in S.SOUND_NAMES:
        data = (paths.SOUNDS_DIR / f"{name}.wav").read_bytes()
        digests.setdefault(data, []).append(name)
    assert len(digests) == 13, [v for v in digests.values() if len(v) > 1]


def test_regeneration_is_byte_identical(tmp_path):
    """重跑一次生成脚本必须逐字节一样：幂等 + 提交的资源没和脚本脱节。

    噪声用的是固定种子，就是为了这条能成立。否则"重新生成一遍"这种
    看起来无害的操作会在 diff 里塞进 13 个二进制改动，也没人说得清哪版是对的。
    """
    r = subprocess.run([sys.executable, str(SCRIPT), "--out", str(tmp_path)],
                       capture_output=True, timeout=300)
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")
    for name in S.SOUND_NAMES:
        a = (paths.SOUNDS_DIR / f"{name}.wav").read_bytes()
        b = (tmp_path / f"{name}.wav").read_bytes()
        assert a == b, f"{name} 与脚本产出不一致 —— 要么忘了重跑生成，要么改过资源"


def test_sounds_have_their_expected_number_of_events():
    """脉冲/音阶类音效必须真的是一声一声，不是糊成一片长音。

    sound.ts 把方波 LFO 硬开关改成脉冲，靠的就是"断续"；delay 掉了这些声会叠成一声。
    下面每个数字都是从生成结果量出来的（不是推测）。变异验证过：把 `tone()`
    的 delay 抹成 0（所有事件叠在起点），timeout 3→1、wrong 2→1、win 4→1，全红。
    """
    for name, expected in (("timeout", 3), ("wrong", 2), ("win", 4), ("lose", 3)):
        n, windows, peak = _segments(_decode(name))
        assert n == expected, (
            f"{name} 应有 {expected} 声，实为 {n}（共 {windows} 窗口 / 峰值 {peak:.3f}）"
            "—— 十有八九是事件没排在不同时间上，全都叠在 t=0")


def test_melodies_go_the_right_direction():
    """win 上行、lose 下行：音阶的音符必须真的排在不同时间上。"""
    head, tail = _voiced_pitch_trend(_decode("win"))
    assert tail > head * 1.3, f"win 没有上行：前 {head:.1f} → 后 {tail:.1f} 过零/窗口"
    head, tail = _voiced_pitch_trend(_decode("lose"))
    assert tail < head * 0.9, f"lose 没有下行：前 {head:.1f} → 后 {tail:.1f} 过零/窗口"


def test_ai_stone_is_lower_pitched_than_player_stone():
    """落子/AI 落子的可听区别（sound.ts:「音色略低，便于分辨是谁下的」）。"""
    def centroid(name):
        xs = _decode(name)
        w = max(1, len(xs) // 400)
        vals = _pitch_windows(xs, w)
        voiced = [c for c, r in vals if r > 0.02]
        return sum(voiced) / max(1, len(voiced))

    assert centroid("stoneAi") < centroid("stone") * 0.95


# ---------------------------------------------------------------- 播放层

class _FakeBackend:
    """记录调用而不是真出声：这样"有没有静音/音量对不对"能被断言，
    而且不依赖这台机器有没有声卡。"""

    def __init__(self, ready=True):
        self.plays: list[tuple[str, float]] = []
        self.created: list[str] = []
        self.stopped = 0
        self.ready = ready

    def create(self, path):
        self.created.append(Path(path).stem)
        return _FakeEffect(self, Path(path).stem)

    def play(self, effect, volume):
        if not self.ready:
            return False
        self.plays.append((effect.key, volume))
        return True

    def stop(self, effect):
        self.stopped += 1


class _FakeEffect:
    def __init__(self, backend, key):
        self._backend = backend
        self.key = key


@pytest.fixture
def prefs(tmp_path):
    p = Prefs(str(tmp_path / "client.ini"))
    p.sound_enabled = True
    p.volume = 0.6
    return p


def test_play_reaches_the_backend_with_prefs_volume(prefs):
    be = _FakeBackend()
    player = S.SoundPlayer(prefs, be)
    assert player.play("stone") is True
    assert be.plays == [("stone", 0.6)]
    assert player.loaded("stone")


def test_volume_is_read_on_every_play_not_snapshotted(prefs):
    """P5 设置页要能边听边调：音量必须每次现读。"""
    be = _FakeBackend()
    player = S.SoundPlayer(prefs, be)
    player.play("click")
    prefs.volume = 0.2
    player.play("click")
    assert [v for _, v in be.plays] == [0.6, 0.2]


def test_muting_stops_before_touching_the_backend(prefs):
    be = _FakeBackend()
    player = S.SoundPlayer(prefs, be)
    prefs.sound_enabled = False
    assert player.play("win") is False
    assert be.plays == [] and be.created == [], "静音了还在建 effect / 还在放"


def test_unknown_name_raises_instead_of_going_silent(prefs):
    """拼错名字是代码 bug，必须响；环境坏了才是静默（见模块 docstring）。"""
    player = S.SoundPlayer(prefs, _FakeBackend())
    with pytest.raises(KeyError):
        player.play("stnse")


def test_missing_file_is_silent_and_remembered(prefs, tmp_path):
    be = _FakeBackend()
    player = S.SoundPlayer(prefs, be, sounds_dir=tmp_path / "empty")
    assert player.play("stone") is False
    assert player.play("stone") is False
    assert be.created == [], "文件不在还反复建对象"
    assert player.missing == frozenset({"stone"})


def test_no_audio_device_degrades_to_false_not_crash(prefs):
    """声卡不在（QSoundEffect 停在 Error/Loading）时，落子流程不能被拖住。"""
    be = _FakeBackend(ready=False)
    player = S.SoundPlayer(prefs, be)
    assert player.play("capture") is False
    assert be.created == ["capture"]        # 装过，只是放不出来


def test_each_sound_is_created_once(prefs):
    be = _FakeBackend()
    player = S.SoundPlayer(prefs, be)
    for _ in range(5):
        player.play("stone")
    assert be.created == ["stone"]
    assert len(be.plays) == 5


def test_preload_loads_the_frequent_ones_without_playing(prefs):
    be = _FakeBackend()
    player = S.SoundPlayer(prefs, be)
    player.preload()
    assert be.created == ["stone", "stoneAi", "capture", "click"]
    assert be.plays == [], "preload 不该出声"


def test_all_thirteen_can_be_loaded_from_the_real_directory(prefs):
    be = _FakeBackend()
    player = S.SoundPlayer(prefs, be)
    assert [n for n in S.SOUND_NAMES if player.load(n)] == list(S.SOUND_NAMES)
    assert player.missing == frozenset()


# ---------------------------------------------------------------- 真 Qt 冒烟

def test_qsoundeffect_can_really_load_our_wav(qapp, prefs):
    """走真 _QtBackend：证明 16-bit/44.1k/单声道的 wav 在 Qt 这条路上真能起来。

    没有输出设备的环境（CI）跳过 —— 本机实测是 Ready，这条不是"应该能响"。
    """
    from PySide6.QtCore import QUrl
    from PySide6.QtMultimedia import QMediaDevices, QSoundEffect

    if not QMediaDevices().audioOutputs():
        pytest.skip("这台机器没有音频输出设备，真出声这条测不了")
    fx = QSoundEffect()
    fx.setSource(QUrl.fromLocalFile(str(paths.SOUNDS_DIR / "stone.wav")))
    import time
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not fx.isLoaded():
        qapp.processEvents()
        time.sleep(0.01)
    assert fx.status() == QSoundEffect.Status.Ready, "wav 装了但 Qt 不认这个格式"
    fx.play()
    qapp.processEvents()
    assert fx.isPlaying(), "status 是 Ready 却放不出：这条资源在真 Qt 上不能用"
    fx.stop()
    qapp.processEvents()
    player = S.SoundPlayer(prefs, sounds_dir=paths.SOUNDS_DIR)   # 默认后端
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not player.play("click"):
        qapp.processEvents()
        time.sleep(0.01)      # setSource 是异步的，第一次 play 会被丢掉 —— 所以才要 preload
    assert player.play("click") is True
