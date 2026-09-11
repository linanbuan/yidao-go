"""把画出来的应用图标写成 Windows 要的那个 .ico。

    python desktop/scripts/gen_icons.py             # 生成 desktop/resources/app.ico
    python desktop/scripts/gen_icons.py --out X.ico # 生成到别处（测试与手工比对用）

为什么要有这个脚本：运行时窗口图标是 `ui/app_icon.icon()` 现画的，不需要任何文件；
但 PyInstaller 的 `--icon` 只吃 .ico，而且 exe 在资源管理器里显示的那枚也只能是它。
也就是说这是**唯一**一处必须把「画出来的东西」落成二进制资源的地方，
所以落盘这件事本身要可重跑、可验证，而不是手 P 一张图丢进仓库。

.ico 的容器是自己拼的（`struct` 二三十行），没有用 Qt 的 ico 编码器：
实测 `QPixmap.save(x, "ICO")` 一次只写得下**一个**尺寸，而 Windows 的图标
是要按场合挑尺寸的（托盘 16、任务栏 24/32、alt-tab 48、快捷方式 256），
只带一档的后果是系统在别处拿拉伸过的那张糊出来。多张图必须自己写目录项。

每条图元用 PNG 负载（Vista 起的 shell 都认），不是位图：
256 那档按规范**必须**是 PNG，而小尺寸用 PNG 也省掉 alpha 掩码那一段。

自检（`verify`）跑在写盘之前，坏了就非 0 退出：
  ① 用自己写的解析器把容器读回来 —— 条目数、每档宽高、偏移与长度都对得上；
  ② 再用 Qt 的独立实现（QImageReader 的 ico 插件）把每一页解出来 —— 这证明
     容器不是「只有我自己的解析器认」；
  ③ 每页都要真有墨：最小尺寸下透明底上没画上东西，就是一枚看不见的图标。

诚实边界：以上证明的是**格式合法**，不是「Windows 已把它画在桌面上」——
后者要等真封装那轮（见 PACKAGING.md）。本机另外用 GDI+（System.Drawing.Icon）
读过一次这个文件，结论记在那份文档里。
"""
from __future__ import annotations

import argparse
import os
import struct
import sys
from pathlib import Path

DESKTOP = Path(__file__).resolve().parent.parent
if str(DESKTOP) not in sys.path:
    sys.path.insert(0, str(DESKTOP))

#: 默认输出位。PACKAGING.md 里 `--icon=` 指的就是它，所以不许随手改动。
DEFAULT_OUT = DESKTOP / "resources" / "app.ico"
#: 每档一个字节宽：边长上限 256，而 256 在规范里写成 0（不是 0 也不是 256）。
MAX_EDGE = 256


# ---------------------------------------------------------------- PNG 负载

def png_bytes(pixmap) -> bytes:
    """把 QPixmap 编成 PNG 字节（带 alpha）。"""
    from PySide6.QtCore import QBuffer, QIODevice

    buf = QBuffer()
    buf.open(QIODevice.WriteOnly)
    if not pixmap.save(buf, "PNG"):
        raise RuntimeError("PNG 编码失败（QPixmap.save 返回 False）")
    return bytes(buf.data())


# ---------------------------------------------------------------- 容器拼装

def build_ico(pixmaps: list) -> bytes:
    """`pixmaps` 是已按尺寸排好的 QPixmap 列表，返回整个 .ico 的字节。"""
    images = [png_bytes(pm) for pm in pixmaps]
    header = struct.pack("<HHH", 0, 1, len(images))          # 保留, 类型=图标, 张数
    step = struct.calcsize("<BBBBHHII")                      # 一条 ICONDIRENTRY = 16
    base = len(header) + step * len(images)
    entries = bytearray()
    offset = base
    for pm, blob in zip(pixmaps, images):
        w, h = pm.width(), pm.height()
        if w > MAX_EDGE or h > MAX_EDGE:
            raise RuntimeError(f"单张图标边长不能超过 {MAX_EDGE}（规范里只有 1..{MAX_EDGE}）")
        entries += struct.pack(
            "<BBBBHHII",
            0 if w == 256 else w,        # 256 记作 0，这是规范里唯一的坑
            0 if h == 256 else h,
            0,                           # 调色板颜色数：0 = 不进调色板
            0,                           # 保留
            1,                           # 色平面数（ico 里恒为 1）
            32,                          # 位深：32 才有 alpha
            len(blob),
            offset,
        )
        offset += len(blob)
    return header + bytes(entries) + b"".join(images)


# ---------------------------------------------------------------- 读回来

def read_ico(data: bytes) -> list[dict]:
    """解析自己拼的容器。故意不复用 build 里的任何变量，好让两边互相校验。"""
    reserved, kind, count = struct.unpack_from("<HHH", data, 0)
    if (reserved, kind) != (0, 1):
        raise RuntimeError(f"图标目录头不对：reserved={reserved} type={kind}（应为 0,1）")
    step = struct.calcsize("<BBBBHHII")
    out: list[dict] = []
    for i in range(count):
        w, h, colors, rsvd, planes, depth, size, off = struct.unpack_from(
            "<BBBBHHII", data, 6 + step * i)
        body = data[off:off + size]
        if len(body) != size:
            raise RuntimeError(f"第 {i} 张越界：offset={off} size={size} 文件长 {len(data)}")
        if body[:8] != b"\x89PNG\r\n\x1a\n":
            raise RuntimeError(f"第 {i} 张不是 PNG 负载")
        out.append({"w": w or 256, "h": h or 256, "colors": colors, "rsvd": rsvd,
                    "planes": planes, "depth": depth, "size": size, "off": off,
                    "png": body})
    return out


def ink_ratio(data: bytes) -> float:
    """一张 PNG 里不透明像素的占比。0.0 就是「画了个看不见的东西」。"""
    from PySide6.QtGui import QImage

    # `QImage.fromData` 而不是往 QBuffer 上 load：后者要求先 setData 再 open，
    # 顺序写错只会得到一句轻描淡写的“解不开”（本函数试过，症状就是它）。
    img = QImage.fromData(data, "PNG")
    if img.isNull():
        raise RuntimeError("PNG 负载解不开")
    img = img.convertToFormat(img.Format.Format_ARGB32)
    solid = sum(1 for y in range(img.height()) for x in range(img.width())
                if (img.pixel(x, y) >> 24) & 0xFF > 16)
    return solid / max(1, img.width() * img.height())


def verify(data: bytes, sizes: tuple[int, ...]) -> list[str]:
    """返回人话写成的检查清单。任何一条不合格都会抛，不会只打印。"""
    from PySide6.QtCore import QBuffer, QIODevice
    from PySide6.QtGui import QImageReader

    lines: list[str] = []
    entries = read_ico(data)
    got = tuple(e["w"] for e in entries)
    if got != tuple(sorted(sizes)):
        raise RuntimeError(f"容器里的档位 {got} 与要生成的 {sorted(sizes)} 不一致")
    lines.append(f"容器：{len(entries)} 张，档位 {list(got)}，共 {len(data)} 字节")

    for e in entries:
        if (e["colors"], e["rsvd"], e["planes"], e["depth"]) != (0, 0, 1, 32):
            raise RuntimeError(f"{e['w']}px 那张的目录项不对：{e}")
        ratio = ink_ratio(e["png"])
        if ratio < 0.15:
            raise RuntimeError(f"{e['w']}px 那张只有 {ratio:.1%} 的墨，等于没有图标")
    lines.append("目录项与墨量：每张 32bpp / 无调色板，最小那档不透明占比 "
                 f"{ink_ratio(entries[0]['png']):.1%}")

    # 独立实现：Qt 的 ico 插件。它要能把同一份字节读成同样多的页。
    # 注意 `setData` 必须在 `open` 之前（开了再换数据会被 Qt 拒掉而不报错）。
    reader = QImageReader()
    buf = QBuffer()
    buf.setData(data)
    buf.open(QIODevice.ReadOnly)
    reader.setDevice(buf)
    if reader.format() != b"ico":
        raise RuntimeError(f"Qt 不认这个容器：format={reader.format()!r}")
    pages, dims = 0, []
    while reader.jumpToImage(pages):
        img = reader.read()
        if img.isNull():
            raise RuntimeError(f"Qt 读不出第 {pages} 页")
        dims.append((img.width(), img.height()))
        pages += 1
    if dims != [(s, s) for s in sorted(sizes)]:
        raise RuntimeError(f"Qt 读出的页与写入不一致：{dims}")
    lines.append(f"Qt 的 ico 插件复核：{pages} 页全部解出，尺寸 {dims}")
    return lines


# ---------------------------------------------------------------- 入口

def generate(out: Path, sizes: tuple[int, ...]) -> list[str]:
    from ui import app_icon                      # 唯一的画法来源，不在这里重画一遍

    ordered = sorted(sizes)
    pixmaps = [app_icon.pixmap(s) for s in ordered]
    data = build_ico(pixmaps)
    lines = verify(data, tuple(ordered))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    lines.append(f"已写入 {out}")
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成桌面客户端的 .ico")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help=f"默认 {DEFAULT_OUT}")
    parser.add_argument("--sizes", default="", help="逗号分隔，默认用 ui/app_icon.SIZES")
    args = parser.parse_args(argv)

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")   # 不出窗口，也别抢焦点
    from PySide6.QtGui import QGuiApplication

    app = QGuiApplication.instance() or QGuiApplication(sys.argv)   # noqa: F841
    from ui import app_icon

    sizes = tuple(int(s) for s in args.sizes.replace(" ", "").split(",")) \
        if args.sizes else tuple(app_icon.SIZES)
    for line in generate(Path(args.out), sizes):
        print(f"  {line}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
