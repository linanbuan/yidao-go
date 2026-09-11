"""服务端文案进界面之前的一道清洗。

后端的讲解文本是按 Markdown 写的（`tsumego/puzzles.py` 的 note、`api/tsumego.py` 的
comment、复盘的大模型输出），而两个客户端都是**纯文本渲染**：React 那边 `{text}` 直出，
原生这边 QLabel 也不解析 —— 于是「紧气要从**对方的外气**紧起」会带着四颗星号出现在
学员眼前（死活页的截图实测到）。

`app/review/commentary.py:221` 给自家模板立了「强调只用「」」的规矩，但题库的 note 与
大模型的输出都不归它管 —— 只能在显示这一侧兜住。改数据也行，可那要动后端契约、
还要重 build 冻结的网页版，不如客户端一处到位。
"""
from __future__ import annotations

import re

#: 成对的 `**粗**`。允许跨行：讲解文本里有一整段被包住的。
_BOLD = re.compile(r"\*\*(.+?)\*\*", flags=re.S)
#: 成对的 `*斜*`。两头都不能挨着**ASCII** 字母数字或另一个星号，否则「2*3」会被啃掉一半。
#: 这里不能用 `\w`：Python 的 `\w` 认汉字，而正文几乎句句是汉字挨着星号
#: （「不是*共同边界*」），拿 `\w` 当护栏就等于对中文放弃了这条规则。
_EM = re.compile(r"(?<![A-Za-z0-9*])\*([^*\n]+)\*(?![A-Za-z0-9*])")
#: 行内代码 `x`：原生标签没有等宽混排，留下反引号只会像打错字。
_CODE = re.compile(r"`([^`\n]+)`")


def plain(text: str) -> str:
    """去掉成对的 Markdown 记号，文字一个不动。

    只做「去记号」，不做「转富文本」：这些字段都是句子，不是文章。
    标题井号、列表星号这类**行首**记号不在本函数范围内 —— 只有复盘页会收到整段
    输出，那里由 `ui/pages/review.py` 的 `prose()` 接手（成对记号依旧走这里）。
    """
    if not text:
        return text
    out = _BOLD.sub(r"\1", text)
    out = _EM.sub(r"\1", out)
    return _CODE.sub(r"\1", out)
