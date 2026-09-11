"""用户偏好与登录态的落盘。

用 INI 而不是注册表：本项目所有状态都是"能打开来看的文件"（棋谱、日志、缓存），
偏好也不例外；出问题时可以直接删这个 ini 复位，不必开 regedit。

令牌放这里的理由与边界：JWT 由本机后端签发、只对本机那个 127.0.0.1 端口有效，
和网页版把它放 localStorage 是同一安全量级；但它**仍是凭据**，所以只写当前用户的
AppData，绝不写项目目录（项目目录会被拷来拷去，还会进版本库）。
"""
from __future__ import annotations

import shutil
from pathlib import Path

from PySide6.QtCore import QSettings


class Prefs:
    """键值偏好。全部给默认值，读到空也返回默认，不做"未设置"三态。"""

    ORG = "Yidao"
    APP = "client"
    #: 更名前的 org（2026-09-08 起叫「弈道」）。只在「新文件不存在、旧文件存在」时
    #: 做一次搬迁 —— 否则用户升级后要重新登录，窗口尺寸与音效偏好也会一起丢。
    LEGACY_ORG = "GoTeach"

    def __init__(self, file_path: str | None = None):
        """默认落在当前用户的 AppData；`file_path` 用于测试把配置指到临时目录。

        留这个口子不是为了灵活，是为了**不污染真配置**：测试里会写 token，
        走默认路径就会把开者登录顶掉。
        """
        if file_path:
            self._s = QSettings(file_path, QSettings.Format.IniFormat)
        else:
            self._s = QSettings(QSettings.Format.IniFormat, QSettings.Scope.UserScope,
                                self.ORG, self.APP)
            self._migrate_legacy(self._s.fileName())

    @classmethod
    def _migrate_legacy(cls, new_path: str, old_path: str | None = None) -> bool:
        """把旧 org 的 ini 原样搬到新 org 名下（只在新文件不存在时）。返回是否真搬了。

        搬的是**文件**而不是逐键复制：偏好文件本身就是「能打开来看的一整个状态」，
        逐键复制既漏得了将来新增的键，也保不住 QSettings 的数组/分组写法。
        """
        new = Path(new_path)
        if new.exists():
            return False
        old = Path(old_path) if old_path else Path(
            QSettings(QSettings.Format.IniFormat, QSettings.Scope.UserScope,
                      cls.LEGACY_ORG, cls.APP).fileName())
        if old == new or not old.exists():
            return False
        new.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(old, new)
        return True

    # ---------------------------------------------------------------- 路径

    @property
    def path(self) -> str:
        return self._s.fileName()

    # ---------------------------------------------------------------- 令牌

    @property
    def token(self) -> str:
        return str(self._s.value("auth/token", "") or "")

    @token.setter
    def token(self, value: str) -> None:
        if value:
            self._s.setValue("auth/token", value)
        else:
            self._s.remove("auth/token")

    @property
    def last_username(self) -> str:
        return str(self._s.value("auth/lastUsername", "") or "")

    @last_username.setter
    def last_username(self, value: str) -> None:
        self._s.setValue("auth/lastUsername", value or "")

    # ---------------------------------------------------------------- 音效

    @property
    def sound_enabled(self) -> bool:
        return _as_bool(self._s.value("sound/enabled", True))

    @sound_enabled.setter
    def sound_enabled(self, value: bool) -> None:
        self._s.setValue("sound/enabled", bool(value))

    @property
    def volume(self) -> float:
        return _as_float(self._s.value("sound/volume", 0.7), 0.0, 1.0)

    @volume.setter
    def volume(self, value: float) -> None:
        self._s.setValue("sound/volume", max(0.0, min(1.0, float(value))))

    # ---------------------------------------------------------------- 练习

    @property
    def auto_next(self) -> bool:
        """死活题答对后自动换下一题。默认**开** —— 连着做题才是练习的常态；
        想停下来看正解变化的人可以在题目面板里关掉（这一条只影响客户端）。"""
        return _as_bool(self._s.value("tsumego/autoNext", True))

    @auto_next.setter
    def auto_next(self, value: bool) -> None:
        self._s.setValue("tsumego/autoNext", bool(value))

    # ---------------------------------------------------------------- 窗口

    @property
    def minimize_to_tray(self) -> bool:
        """点最小化就是收进托盘。默认**关**：默认收起来而任务栏上没图标，
        对用户就是「窗口去哪了」，这一条得由他自己打开。"""
        return _as_bool(self._s.value("window/minimizeToTray", False))

    @minimize_to_tray.setter
    def minimize_to_tray(self, value: bool) -> None:
        self._s.setValue("window/minimizeToTray", bool(value))

    def save_window(self, geometry, state) -> None:
        self._s.setValue("window/geometry", geometry)
        self._s.setValue("window/state", state)

    def window_geometry(self):
        return self._s.value("window/geometry")

    def window_state(self):
        return self._s.value("window/state")

    # ---------------------------------------------------------------- 杂项

    def set(self, key: str, value) -> None:
        self._s.setValue(key, value)

    def get(self, key: str, default=None):
        v = self._s.value(key, default)
        return default if v is None else v

    def sync(self) -> None:
        self._s.sync()


def _as_bool(v) -> bool:
    """QSettings 把 bool 存成字符串 "true"/"false"，读回来不一定是 bool。"""
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def _as_float(v, lo: float, hi: float) -> float:
    try:
        return max(lo, min(hi, float(v)))
    except (TypeError, ValueError):
        return lo
