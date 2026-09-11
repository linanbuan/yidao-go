"""登录页：对应 frontend/src/pages/LoginPage.tsx。

只做"拿 token + 拿 user"这一件事，成功后由 shell 决定去哪。
令牌交给 `core/settings.Prefs`（等价于网页版的 localStorage）。

文案逐字抄网页版（它是这套文案的活文档）。**一处刻意偏离**：网页版用
「登录 / 注册」两个 tab 切模式 + 一个主按钮，原生端直接给两个按钮 ——
tab 把「我要注册」藏在了一个要点开才看得见的状态里，而两个并排的按钮不需要解释。
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFormLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout, QWidget,
)

from .. import app_icon
from .. import chrome as chrome_mod
from .. import theme


class LoginPage(QWidget):
    """账号密码登录 / 注册。`loggedIn(user)` 只在后端确认之后才发。"""

    loggedIn = Signal(object)

    def __init__(self, api, prefs, parent=None):
        super().__init__(parent)
        self._api = api
        self._prefs = prefs
        self._busy = False

        # 页面底色给一层柔和渐变（比全局纯米色多一点“门口”的感觉）。
        # 用 ID 选择器只作用于本页：`QWidget {}` 那条会级联进子控件，
        # 把卡片里的标签也染上渐变底（全局那行 `QLabel { transparent }` 压不住它）。
        self.setObjectName("loginRoot")
        self.setStyleSheet(f"#loginRoot {{ background: qlineargradient("
                           f"x1:0, y1:0, x2:0, y2:1,"
                           f" stop:0 {theme.BG}, stop:1 #ece5d6); }}")

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addStretch(1)

        card = QWidget(self)
        card.setObjectName("loginCard")
        card.setStyleSheet(f"QWidget#loginCard {{ background: {theme.PANEL};"
                           f" border: 1px solid {theme.LINE}; border-radius: 14px; }}")
        lay = QVBoxLayout(card)
        lay.setContentsMargins(34, 26, 34, 26)
        lay.setSpacing(10)

        # 品牌行：棋盘图标 + 应用名 + 一句用途 —— 玩家在门口先看见“这是什么”。
        brand = QVBoxLayout()
        brand.setSpacing(4)
        mark = QLabel(card)
        mark.setPixmap(app_icon.pixmap(72))
        mark.setAlignment(Qt.AlignCenter)
        brand.addWidget(mark)
        title = QLabel(chrome_mod.APP_TITLE, card)
        title.setStyleSheet("font-size: 26px; font-weight: 700; background: transparent;")
        title.setAlignment(Qt.AlignCenter)
        brand.addWidget(title)
        sub = QLabel("AI 陪练 · 从 18 级一路打到九段 · 大模型逐手讲解", card)
        sub.setProperty("role", "muted")
        sub.setAlignment(Qt.AlignCenter)
        brand.addWidget(sub)
        lay.addLayout(brand)
        lay.addSpacing(6)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight)
        form.setSpacing(8)
        self.user = QLineEdit(prefs.last_username or "", card)
        self.user.setPlaceholderText("用户名")
        self.user.setMinimumWidth(240)
        self.pw = QLineEdit(card)
        self.pw.setPlaceholderText("密码（至少 6 位）")
        self.pw.setEchoMode(QLineEdit.Password)
        form.addRow("账号", self.user)
        form.addRow("密码", self.pw)
        lay.addLayout(form)

        self.error = QLabel("", card)
        self.error.setWordWrap(True)
        self.error.setStyleSheet(f"color: {theme.DANGER}; background: transparent;")
        self.error.setVisible(False)
        lay.addWidget(self.error)

        row = QHBoxLayout()
        self.btnLogin = QPushButton("登录", card)
        self.btnLogin.setProperty("role", "primary")
        self.btnRegister = QPushButton("注册新账号", card)
        row.addWidget(self.btnLogin)
        row.addWidget(self.btnRegister)
        row.addStretch(1)
        lay.addLayout(row)

        # 逐字抄 `LoginPage.tsx:73`。之前这里写的是「数据都在 backend/data」——
        # 那是一个开发者关心的路径，不是学员关心的事；而晋升规则恰好是新人
        # 点「注册新账号」之前最想知道的一句。（这一处是换上真字体重读截图才看见的：
        # 之前那张图里所有字都是方框，根本读不下去。）
        self.tip = QLabel("新账号从 18级 起步：累计 3 胜进入晋升战，晋升战 2 连胜即可升到 17级。",
                          card)
        self.tip.setProperty("role", "muted")
        self.tip.setWordWrap(True)
        lay.addWidget(self.tip)

        card.setMaximumWidth(460)
        holder = QWidget(self)
        h = QHBoxLayout(holder)
        h.setContentsMargins(24, 0, 24, 0)
        h.addStretch(1)
        h.addWidget(card)
        h.addStretch(1)
        outer.addWidget(holder)
        outer.addStretch(2)

        self.btnLogin.clicked.connect(lambda: self._submit("login"))
        self.btnRegister.clicked.connect(lambda: self._submit("register"))
        self.pw.returnPressed.connect(lambda: self._submit("login"))
        self.user.returnPressed.connect(lambda: self.pw.setFocus())

    # ---------------------------------------------------------------- 行为

    def _submit(self, mode: str) -> None:
        if self._busy:
            return
        username = self.user.text().strip()
        password = self.pw.text()
        if not username or not password:
            self._fail("请填写用户名与密码")
            return
        if mode == "register" and len(password) < 6:
            self._fail("密码至少 6 位")     # 与后端 RegisterIn 的 min_length 同口径
            return
        self._set_busy(True, "登录中…" if mode == "login" else "注册中…")
        body = {"username": username, "password": password}
        if mode == "register":
            body["displayName"] = username
        reply = self._api.post(f"/api/auth/{mode}", body)
        reply.finished.connect(self._on_done)

    def _on_done(self, data, err) -> None:
        if err is not None or not data or not data.get("token"):
            self._set_busy(False)
            self._fail(getattr(err, "args", ["服务没有返回令牌"])[0] if err
                       else "服务没有返回令牌")
            return
        self._prefs.token = data["token"]
        self._prefs.last_username = data["user"].get("username", "")
        self._prefs.sync()
        self._set_busy(False)
        self.error.setVisible(False)
        self.pw.clear()
        self.loggedIn.emit(data["user"])

    # ---------------------------------------------------------------- 小工具

    def _set_busy(self, busy: bool, label: str = "") -> None:
        self._busy = busy
        for b in (self.btnLogin, self.btnRegister):
            b.setEnabled(not busy)
        if busy:
            self.btnLogin.setText(label or "登录中…")
        else:
            # 复位（审计 L1）：从前只有 `_fail()` 才把按钮文案改回来，于是
            # **成功**路径留下「注册中…」/「登录中…」—— 令牌失效被送回登录页时
            # 最容易踩：按钮写着「注册中…」，点下去执行的却还是登录。
            self.btnLogin.setText("登录")
            self.btnRegister.setText("注册新账号")

    def _fail(self, message: str) -> None:
        self._set_busy(False)                # 文案复位统一走 `_set_busy`（审计 L1）
        self.error.setText(message)
        self.error.setVisible(True)
