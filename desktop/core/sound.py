"""音效播放：资源是预生成的 wav，运行期只负责「放」和「闭嘴」。

口径来自 `frontend/src/lib/sound.ts` 的那句注释 —— **音效绝不能打断对局流程**：
没有声卡、文件被手删、格式不支持，一律安静返回，不弹窗、不抛异常。

但「静默」只针对环境问题，不针对代码写错：
  · `play("stnse")` 这种拼错的名字**会 raise** —— 那是 bug，静默掉就等于让
    某一声效永久消失而没人知道（本项目已经吃过太多次静默失败的亏）；
  · 文件在、声卡没了 → 返回 False。

音量/开关每播放**都重新读** `Prefs`，不在构造时快照：P5 的设置页要能边打音效
边调滑杆，快照的话就得再加一套通知机制，不值当。
"""
from __future__ import annotations

from collections import deque
from pathlib import Path

from core import paths
from core.settings import Prefs

#: 13 个名字，必须与 sound.ts 的 `SoundName` 联合类型逐项一致（含顺序）。
#: 这条由 tests/test_sounds.py 直接解析 sound.ts 来把关，不靠这里的手抄清单。
SOUND_NAMES: tuple[str, ...] = (
    "stone", "stoneAi", "capture", "win", "lose", "resign", "timeout",
    "promote", "demote", "correct", "wrong", "click", "review",
)


class _QtBackend:
    """真播放层。拆出来只为了测试能塞一个假的进来（见 SoundPlayer 的 backend 参数）。

    PySide6 6.11.2 实测的 API 面：有 `isLoaded()/status()/setSource()/setVolume()/
    play()/stop()`，**没有** `duration()`，也没有 6.5 之前那个 `error()`。
    按这个面写，别顺手调不存在的方法（那样只会拿到 AttributeError 崩在落子上）。
    """

    def create(self, path: Path):
        from PySide6.QtCore import QUrl
        from PySide6.QtMultimedia import QSoundEffect

        fx = QSoundEffect()
        # setSource 是**异步**装载：紧接着 play() 会被丢掉，所以常用音效要 preload
        fx.setSource(QUrl.fromLocalFile(str(path)))
        return fx

    def play(self, effect, volume: float) -> bool:
        from PySide6.QtMultimedia import QSoundEffect

        if effect.status() != QSoundEffect.Status.Ready:
            return False                  # 没声卡（Error）或还在装载（Loading）
        effect.setVolume(max(0.0, min(1.0, volume)))
        effect.play()                     # 已在播时 Qt 会从头重来，与 WebAudio 的重触发一致
        return True

    def stop(self, effect) -> None:
        effect.stop()


class SoundPlayer:
    """按键名放音。effect 按名字缓存，一个名字只建一次。"""

    def __init__(self, prefs: Prefs | None = None, backend=None,
                 sounds_dir: str | Path | None = None):
        self._prefs = prefs or Prefs()
        self._backend = backend if backend is not None else _QtBackend()
        self._dir = Path(sounds_dir) if sounds_dir else paths.SOUNDS_DIR
        self._effects: dict[str, object] = {}
        self._broken: set[str] = set()    # 文件缺失的名字：只提示一次，不再反复建对象
        # 该响的音效按顺序记在这里。存在的理由是对局/死活页的验收要断言
        # “这一步真的响了”（计划 P3），而“响了”在无人听着的环境里没法靠耳朵验。
        # 语义是“按开关与资源该出声”，不是“扬声器真的出了声”：
        # 没声卡时 QSoundEffect 是 Loading/Error，那种失败不该反过来改断言口径。
        self._played: deque[str] = deque(maxlen=64)

    # ---------------------------------------------------------------- 属性

    @property
    def sounds_dir(self) -> Path:
        return self._dir

    @property
    def enabled(self) -> bool:
        return self._prefs.sound_enabled

    @property
    def volume(self) -> float:
        return self._prefs.volume

    # ---------------------------------------------------------------- 播放

    def play(self, name: str) -> bool:
        """放一个音效，返回是否真的出声。任何环境故障都是 False，不是异常。"""
        if name not in SOUND_NAMES:
            raise KeyError(
                f"没有这个音效：{name}。可用：{'、'.join(SOUND_NAMES)}")
        if not self.enabled or name in self._broken:
            return False
        self._played.append(name)
        effect = self._effects.get(name)
        if effect is None:
            path = self._dir / f"{name}.wav"
            if not path.exists():
                self._broken.add(name)
                return False
            effect = self._backend.create(path)
            self._effects[name] = effect
        return bool(self._backend.play(effect, self.volume))

    def preload(self, *names: str) -> None:
        """提前把 wav 交出去装载。对局里第一次落子就想有声音，就得先 preload。

        不传参数时预载最频繁的那几个（落子/提子/按钮），这也是真实调用点的用法。
        """
        for name in (names or ("stone", "stoneAi", "capture", "click")):
            if name in SOUND_NAMES:
                self.load(name)

    def load(self, name: str) -> bool:
        """只装载不出声。返回文件是否存在（环境有没有声卡不在这里的判断范围）。"""
        if name not in SOUND_NAMES:
            raise KeyError(f"没有这个音效：{name}")
        if name in self._effects or name in self._broken:
            return name not in self._broken
        path = self._dir / f"{name}.wav"
        if not path.exists():
            self._broken.add(name)
            return False
        self._effects[name] = self._backend.create(path)
        return True

    def stop_all(self) -> None:
        for effect in self._effects.values():
            self._backend.stop(effect)

    # ---------------------------------------------------------------- 诊断

    @property
    def played(self) -> tuple[str, ...]:
        """最近最多 64 次“该出声”的音效名（最早 → 最新）。"""
        return tuple(self._played)

    def count_of(self, name: str) -> int:
        return self._played.count(name)

    def forget_played(self) -> None:
        """清账。测试数“这一手响了几次”之前先清一下，不然会把上一步的算进来。"""
        self._played.clear()

    @property
    def missing(self) -> frozenset[str]:
        """装载失败（文件不在）的名字。设置页可以拿它提示「资源不完整」。"""
        return frozenset(self._broken)

    def loaded(self, name: str) -> bool:
        return name in self._effects
