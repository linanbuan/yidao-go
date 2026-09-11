"""SGF 导出/导入（标准棋谱兼容）。"""
from __future__ import annotations

import re
from typing import Optional

from .rules import BLACK, WHITE, Game, from_sgf, to_sgf


def export_sgf(game: Game, black_name: str = "玩家", white_name: str = "KataGo",
               comment: str = "", extra: Optional[dict] = None) -> str:
    props = [
        "FF[4]", "GM[1]", "CA[UTF-8]", "AP[Yidao]",
        f"SZ[{game.size}]", f"KM[{game.komi:g}]",
        f"RU[{'Chinese' if game.score_method == 'area' else 'Japanese'}]",
        f"PB[{_esc(black_name)}]", f"PW[{_esc(white_name)}]",
    ]
    if game.handicap >= 2:
        props.append(f"HA[{game.handicap}]")
        props.append("AB" + "".join(f"[{to_sgf(p, game.size)}]" for p in game.handicap_stones))
        props.append("PL[W]")
    if game.result:
        res = game.result.get("result") or ""
        code = _result_code(game.result)
        if code:
            props.append(f"RE[{code}]")
        elif res:
            props.append(f"RE[{_esc(res)}]")
    if comment:
        props.append(f"C[{_esc(comment)}]")
    for k, v in (extra or {}).items():
        props.append(f"{k}[{_esc(str(v))}]")

    head = ";" + "".join(props)
    body = []
    for m in game.moves:
        tag = "B" if m.color == BLACK else "W"
        coord = "" if m.point is None else to_sgf(m.point, game.size)
        body.append(f";{tag}[{coord}]")
    return "(" + head + "".join(body) + ")"


def _result_code(result: dict) -> str:
    winner = result.get("winner")
    diff = result.get("diff")
    text = result.get("result") or ""
    if "认输" in text or "投子" in text or "Resign" in text:
        return "B+R" if winner == BLACK else ("W+R" if winner == WHITE else "")
    if "和棋" in text:
        return "0"
    if winner == BLACK and diff is not None:
        return f"B+{diff:g}"
    if winner == WHITE and diff is not None:
        return f"W+{-diff:g}"
    return ""


def _esc(text: str) -> str:
    return text.replace("\\", "\\\\").replace("]", "\\]").replace("[", "\\[")


_PROP_RE = re.compile(r"([A-Z]{1,2})((?:\s*\[(?:[^\]\\]|\\.)*\])+)", re.S)
_VAL_RE = re.compile(r"\[(?:[^\]\\]|\\.)*\]")


def parse_sgf(sgf: str, default_size: int = 19) -> dict:
    """解析 SGF 主序列，返回 {size, komi, handicap, moves:[(color, point|None)], result}。

    只解析主分支（教学复盘场景足够），忽略变着分支。
    """
    sgf = sgf.strip()
    size = default_size
    komi = 7.5
    handicap = 0
    result = ""
    moves: list[tuple[int, Optional[tuple[int, int]]]] = []
    handicap_stones: list[tuple[int, int]] = []

    # 逐节点切分：以 ';' 为界，忽略括号层级
    nodes: list[str] = []
    buf = ""
    for ch in sgf:
        if ch in "()":
            if ch == ")" and buf.strip():
                nodes.append(buf)
                buf = ""
            continue
        if ch == ";":
            if buf.strip():
                nodes.append(buf)
            buf = ""
            continue
        buf += ch
    if buf.strip():
        nodes.append(buf)

    for idx, node in enumerate(nodes):
        props = {}
        for m in _PROP_RE.finditer(node):
            key = m.group(1)
            vals = [v[1:-1].replace("\\]", "]").replace("\\[", "[").replace("\\\\", "\\")
                    for v in _VAL_RE.findall(m.group(2))]
            props[key] = vals
        if idx == 0:
            if "SZ" in props:
                size = int(props["SZ"][0].split(":")[0])
            if "KM" in props:
                try:
                    komi = float(props["KM"][0])
                except ValueError:
                    pass
            if "HA" in props:
                handicap = int(props["HA"][0])
            if "RE" in props:
                result = props["RE"][0]
            if "AB" in props:
                handicap_stones = [from_sgf(v, size) for v in props["AB"] if len(v) == 2]
            continue
        color = None
        val = ""
        if "B" in props:
            color = BLACK
            val = props["B"][0]
        elif "W" in props:
            color = WHITE
            val = props["W"][0]
        if color is None:
            continue
        val = val.strip()
        # 空值或 "tt"（19 路以下旧式约定）视为虚手
        point = from_sgf(val, size) if len(val) == 2 and val != "tt" else None
        moves.append((color, point))

    return {
        "size": size, "komi": komi, "handicap": handicap,
        "handicapStones": handicap_stones, "moves": moves, "result": result,
    }


def sgf_to_game(sgf: str) -> Game:
    """把 SGF 直接还原成 Game（用于导入棋谱复盘）。"""
    data = parse_sgf(sgf)
    game = Game(size=data["size"], komi=data["komi"], handicap=data["handicap"])
    if data["handicapStones"]:
        game.handicap_stones = data["handicapStones"]
        game.board.place(BLACK, data["handicapStones"])
        game.next_color = WHITE
    for color, point in data["moves"]:
        if color != game.next_color:
            break   # 乱序棋谱：保守停在第一处不一致
        game.play(color, point)
    game.finished = False
    return game
