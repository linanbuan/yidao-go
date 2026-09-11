"""`.ico` 这份二进制资源的验收（打包缝的一半：图标准备好了没有）。

计划口径：图标「画出来，不带 png 资源」（`ui/app_icon.py`），但 PyInstaller 的
`--icon` 只吃 `.ico`，所以 `scripts/gen_icons.py` 是**唯一**把画法落成二进制的一步。
资源一旦落盘就有了两类新故障，都不是代码能自己发现的：

  ① 文件与脚本脱节（改了画法没重跑，或有人手 P 了一张图提交）；
  ② 容器写坏了 —— 症状是「资源管理器里没图标」而不是任何报错。

所以这里三条线各自钉住：逐字节可重跑（①）、格式被**另一个实现**读通（②）、
以及小尺寸下真有墨且黑白子都还在（②的极端情形：容器合法但画出来是空图）。

不承诺的事：这几条证明的是文件本身，不是「Windows 已经把图标画在桌面上」。
后者要等真封装那轮；本机额外用 GDI+（`System.Drawing.Icon`）打开过这个文件，
它挑中了 32 那档、只含 256 的样本也报 256x256，说明 PNG 负载这条路系统认。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from core import paths

DESKTOP = Path(__file__).resolve().parent.parent
SCRIPT = DESKTOP / "scripts" / "gen_icons.py"
ICO = DESKTOP / "resources" / "app.ico"


def _load_gen_icons():
    """scripts/ 不是包，手工加一下 sys.path 再 import（与 test_sounds 同一做法）。"""
    sys.path.insert(0, str(SCRIPT.parent))
    try:
        import gen_icons
        return gen_icons
    finally:
        sys.path.pop(0)


def _entries() -> list[dict]:
    gen = _load_gen_icons()
    if not ICO.exists():
        pytest.fail(f"{ICO} 不在 —— 先跑 python desktop/scripts/gen_icons.py")
    return gen.read_ico(ICO.read_bytes())


def _gray(p: int) -> float:
    return ((p >> 16) & 0xFF) * .299 + ((p >> 8) & 0xFF) * .587 + (p & 0xFF) * .114


# ---------------------------------------------------------------- 落点

def test_default_output_is_the_file_packaging_will_point_at():
    """脚本默认输出位必须就是 PACKAGING.md 里 `--icon=` 指的那个路径。

    这条和 `test_sounds` 里同名的那条是一个用途：改 paths.py 时两边要一起动，
    靠这条红来提醒，而不是等到封出来没图标才发现。
    """
    gen = _load_gen_icons()
    assert gen.DEFAULT_OUT == ICO == paths.RESOURCES_DIR / "app.ico", gen.DEFAULT_OUT


def test_the_cli_default_brings_every_runtime_size(tmp_path):
    """不带 `--sizes` 时，“该带哪几档”必须只来自 `app_icon.SIZES` 这一份清单。

    两处各写一份的话，将来加一档就会漏一处，而漏的那处只在特定场合露馅
    （比如只有任务栏上那一枚糊）。所以跑一次**真的 CLI**，不把 sizes 递进去。
    """
    from ui import app_icon

    gen = _load_gen_icons()
    assert max(app_icon.SIZES) <= gen.MAX_EDGE, "超过 ico 的边长上限，构建时会直接抛"
    out = tmp_path / "from_cli.ico"
    r = subprocess.run([sys.executable, str(SCRIPT), "--out", str(out)],
                       capture_output=True, timeout=300)
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")
    assert [e["w"] for e in gen.read_ico(out.read_bytes())] == sorted(app_icon.SIZES)


# ---------------------------------------------------------------- 容器

def test_the_container_carries_every_size_and_no_more():
    from ui import app_icon

    entries = _entries()
    assert [e["w"] for e in entries] == sorted(app_icon.SIZES), \
        "档位不对：Windows 按场合挑尺寸，缺哪档那一场合就被拉伸糊掉"
    for e in entries:
        assert e["h"] == e["w"], "图标必须是方的，否则任务栏会把它压扁"
        # 这四样是 PNG 负载图标的标准写法：不带调色板、32 位才有 alpha
        assert (e["colors"], e["rsvd"], e["planes"], e["depth"]) == (0, 0, 1, 32), e
        assert e["png"][:8] == b"\x89PNG\r\n\x1a\n"


def test_another_implementation_reads_it_back(qapp):
    """用 Qt 的 ico 插件读自己拼的容器：只被自家解析器认可的算不上合法文件。"""
    from PySide6.QtCore import QBuffer, QIODevice
    from PySide6.QtGui import QImageReader
    from ui import app_icon

    data = ICO.read_bytes()
    buf = QBuffer()
    buf.setData(data)                      # setData 必须在 open 之前，见脚本里的注
    buf.open(QIODevice.ReadOnly)
    reader = QImageReader()
    reader.setDevice(buf)
    assert reader.format() == b"ico", reader.format()
    sizes, page = [], 0
    while reader.jumpToImage(page):
        img = reader.read()
        assert not img.isNull(), f"第 {page} 页解不出来"
        sizes.append((img.width(), img.height()))
        page += 1
    # 拿原始清单当基准，不拿 `read_ico` 的结果：不然两边错在同一处就看不出来了
    assert sizes == [(s, s) for s in sorted(app_icon.SIZES)], sizes


def test_every_page_has_ink_and_both_stone_colors():
    """最狠的一种坏法：容器完全合法、页也解得开，但画出来是空图。

    阈值全是从当前产物量出来的（实测 16px：不透明 100%、暗于 70 的 5 px、
    亮过 200 的 66 px）。留了余量但没留到 0 —— 只要有一色消失就说明棋盘上
    少了一枚子，而那正是这个图标唯一的辨识度来源。
    """
    from PySide6.QtGui import QImage

    entries = _entries()
    assert len(entries) >= 4, f"只量到 {len(entries)} 页，这整条测试是 vacuous"
    for e in entries:
        img = QImage.fromData(e["png"], "PNG").convertToFormat(
            QImage.Format.Format_ARGB32)
        px = [img.pixel(x, y) for y in range(img.height()) for x in range(img.width())]
        n = len(px)
        solid = sum(1 for p in px if (p >> 24) & 0xFF > 16) / n
        dark = sum(1 for p in px if _gray(p) < 70)
        light = sum(1 for p in px if _gray(p) > 200)
        assert solid > 0.90, f"{e['w']}px 那页只有 {solid:.1%} 不透明"
        assert dark >= 3, f"{e['w']}px 那页没有黑子（暗像素 {dark}）"
        assert light >= 20, f"{e['w']}px 那页没有白子（亮像素 {light}）"


# ---------------------------------------------------------------- 幂等 / 防假绿

def test_regeneration_is_byte_identical(tmp_path):
    """重跑一次生成脚本必须逐字节一样：提交的 `.ico` 与脚本没脱节。"""
    out = tmp_path / "app.ico"
    r = subprocess.run([sys.executable, str(SCRIPT), "--out", str(out)],
                       capture_output=True, timeout=300)
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")
    assert out.read_bytes() == ICO.read_bytes(), \
        "改过画法就重跑一次 gen_icons.py，否则界面里的图标与 exe 上的不是同一个"


def test_the_verifier_actually_rejects_broken_containers(qapp):
    """上面几条的守卫：解析器/校验器不是永真。

    四种坏法各造一次 —— 类型字段错、条目长度越界、负载不是 PNG、整张透明。
    少了这条，`read_ico` 一旦写坏了返回值就会让所有像素断言读到空清单白白通过。
    """
    gen = _load_gen_icons()
    good = ICO.read_bytes()

    wrong_type = good[:2] + bytes([9, 0]) + good[4:]
    with pytest.raises(RuntimeError, match="图标目录头"):
        gen.read_ico(wrong_type)

    with pytest.raises(RuntimeError, match="越界"):
        gen.read_ico(good[:200] + b"\x00" * 8)      # 截短：偏移还在但长度不够

    truncated = bytearray(good)
    e0 = gen.read_ico(good)[0]
    truncated[e0["off"]:e0["off"] + 8] = b"BM" + b"\x00" * 6
    with pytest.raises(RuntimeError, match="不是 PNG"):
        gen.read_ico(bytes(truncated))

    # 墨量也要能被否证：一张纯透明的图必须过不了
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QPixmap

    blank = QPixmap(32, 32)
    blank.fill(Qt.transparent)
    with pytest.raises(RuntimeError, match="的墨"):
        gen.verify(gen.build_ico([blank]), (32,))
