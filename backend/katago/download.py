"""KataGo 引擎与网络权重下载脚本（Windows / Linux 通用）。

用法（在 backend 目录下）：
    python katago/download.py                     # 自动选后端：有 N 卡用 cuda，否则用 eigen(CPU)
    python katago/download.py --backend eigen     # 强制纯 CPU 版（最稳，不依赖显卡驱动）
    python katago/download.py --backend opencl    # 显卡通用版（N 卡/AMD/Intel 都能跑）
    python katago/download.py --no-human          # 不下载 human SL 网络（省 ~100MB）
    python katago/download.py --model-only        # 已有引擎，只补权重
    python katago/download.py --mirror https://gh-proxy.com/https://github.com

说明：
  * --mirror 的域名以本脚本 --help 里的实测结论为准（ghproxy.com 已停用，填了反而下不动）；
  * 引擎来自 KataGo 官方 GitHub Release；Windows 取 *windows*.zip，Linux 取 *<backend>-linux*.tar.gz；
  * **解压时保留包内全部文件**：cuda/opencl 版依赖随包的 cudnn、cudart、OpenCL 等动态库，
    只拷 katago.exe 会直接起不来；
  * 装完会跑一次 `katago version` 自检：cuda 版对驱动/cudnn 版本敏感，
    下载成功不等于能跑，自检失败时会提示换哪个后端；
  * 主网络默认 b18c384nbt（强，适合段位档位），--small 可额外下载 b6 小网络（CPU 更快）；
  * human SL 网络 human5k 用于级位～低段的"拟人棋风"（会犯该级别典型错误）；
  * Docker 部署时 Dockerfile 会调用本脚本（--backend eigen，纯 CPU 可跑）。

没有 KataGo 时平台会自动降级到内置启发式引擎，全流程仍可用。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import subprocess
import sys
import tarfile
import time
import urllib.request
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent          # backend/katago
MODELS_DIR = HERE / "models"
USER_AGENT = "yidao-setup/1.0"
API_ROOT = "https://api.github.com/repos/lightvector/KataGo"

MIN_SPEED = 0.03          # MB/s：低于此速度就放弃当前通道（官方 CDN 常被限到几 KB/s）
MIN_SPEED_GRACE = 10.0    # 秒：给连接足够的预热时间再开始卡速度，避免误杀慢启动

# 本脚本通常被启动器（pythonw，无控制台）经 QProcess 拉起；`katago version`
# 自检是控制台子进程，Windows 会为它新开一个终端窗口。压掉（POSIX 上取 0，
# Popen 对 creationflags=0 不挑平台）。
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# 不是终端时（重定向到日志、被启动器管道接管）\r 不会回到行首，
# 每次刷新都会留成一行，几百行进度会把真正的错误信息洗掉
IS_TTY = sys.stdout.isatty()

# 引擎包的候选 release，从新到旧依次试。
# v1.18 起官方只发布 cuda 版（opencl/eigen 的预编译包停在 v1.17.1），
# 所以想要非 cuda 后端必须往回退，只查 latest 会直接找不到包。
ENGINE_TAGS = ["latest", "v1.17.1", "v1.16.2", "v1.15.3"]

# 网络权重（同样从官方 release 资产拿，因此能走 api.github.com 下载端点）
#
# 主网络为何是 human SL 版：**没得选**，实测结论：
#   * GitHub release 里的 .bin.gz 只有 v1.15.0 的 b18c384nbt-humanv0（human SL）
#     和 v1.17.1 的三个中小标准网络（b10c384 / b10c512 / b11c768）；
#   * 标准 b18 只在 media.katagotraining.org 上，本机 DNS 解析失败，拿不到。
# 注意 humanv0 当 -model 时，analysis.cfg 里**必须**写 humanSLProfile，
# 否则第一条查询就 FATAL ERROR: SGFMetadata is required 并杀掉引擎；
# 这件事由 app/engine/katago.py 的 ensure_config 自动处理，不要在这里重复实现。
MAIN_NETWORK = ("v1.15.0", "b18c384nbt-humanv0.bin.gz", "kata_b18c384nbt-humanv0.bin.gz")
# v1.17.1 自带的标准（非 human）网络，想要纯 AI 口径的分析可以拿它当 -model
SMALL_NETWORK = ("v1.17.1", "b10c384h6nbttflrs.bin.gz", "kata_b10c384h6nbt.bin.gz")

# 后端名 → 压缩包名里应包含的关键字
BACKEND_KEYS = {
    "cuda": ("cuda",),
    "opencl": ("opencl",),
    "eigen": ("eigen", "eigenavx2"),
    "trt": ("trt", "tensorrt"),
}

# 自检失败时的降级顺序：GPU 版起不来就一路退到纯 CPU，
# 因为 eigen 不依赖任何显卡运行库，“能跑”这件事几乎总能成立
BACKEND_FALLBACK = {
    "trt": ["cuda", "opencl", "eigen"],
    "cuda": ["opencl", "eigen"],
    "opencl": ["eigen"],
    "eigen": [],
}


def first_line(text: str) -> str:
    lines = [ln for ln in (text or "").splitlines() if ln.strip()]
    return lines[0] if lines else "(无输出)"


def log(msg: str) -> None:
    print(f"[katago-setup] {msg}", flush=True)


def gh_api(path: str):
    """读 GitHub API（release 信息）。api.github.com 在国内通常比 github.com 主站可达。"""
    url = f"{API_ROOT}/{path.lstrip('/')}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                                   "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=45) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as exc:   # noqa: BLE001
        log(f"GitHub API 请求失败 {path}：{exc}")
        return None


_RELEASE_CACHE: dict[str, object] = {}


def release_data(tag: str):
    """取 release 元数据，带缓存：同一个 tag 会被多个后端/两轮匹配反复查。"""
    if tag not in _RELEASE_CACHE:
        path = "releases/latest" if tag == "latest" else f"releases/tags/{tag}"
        _RELEASE_CACHE[tag] = gh_api(path)
    return _RELEASE_CACHE[tag]


def find_asset(tag: str, match) -> dict | None:
    """在指定 release 里找第一个名字满足 match 的资产（release 信息带缓存）。"""
    data = release_data(tag)
    if not isinstance(data, dict):
        return None
    for asset in data.get("assets", []):
        if match(asset.get("name", "")):
            found = dict(asset)
            found["_tag"] = data.get("tag_name", tag)
            return found
    return None


def resolve_engine_asset(backend: str) -> dict | None:
    """按 ENGINE_TAGS 从新到旧找指定后端的引擎包。

    必须支持版本回退：v1.18 起官方只发 cuda 版，opencl/eigen 的预编译包
    停在 v1.17.1，只查 latest 的话非 cuda 后端永远找不到包。
    同一版本里优先取标准包而不是 +bs50/+bs29（大 batch 变体，体积更大）。
    """
    is_windows = os.name == "nt"
    suffix = ".zip" if is_windows else ".tar.gz"
    platform = "windows" if is_windows else "linux"
    keys = BACKEND_KEYS.get(backend.lower(), (backend.lower(),))

    def make_match(reject_bs: bool):
        def match(name: str) -> bool:
            low = name.lower()
            if not low.endswith(suffix) or platform not in low:
                return False
            if not any(k in low for k in keys):
                return False
            # TensorRT 包名里也带 cuda，选 cuda 时必须排掉
            if backend.lower() == "cuda" and ("trt" in low or "tensorrt" in low):
                return False
            return not (reject_bs and "+bs" in low)
        return match

    for reject_bs in (True, False):
        for tag in ENGINE_TAGS:
            asset = find_asset(tag, make_match(reject_bs))
            if asset:
                log(f"选定引擎包：{asset['name']}（Release {asset['_tag']}）")
                return asset
    log(f"在 {ENGINE_TAGS} 里都没找到 {backend}/{platform} 的引擎包")
    return None


def _stream_download(url: str, dest: Path, headers: dict, label: str) -> bool:
    """分块下载到 .part 临时文件，成功后才改名。

    四个细节：
      1. 大文件必须有进度输出，否则用户会以为卡死了；
      2. 先写 .part 再改名：下载中断不会留下半成品被当成“已下好”；
      3. urlopen 的 timeout 会变成 socket 超时，作用于每一次 read，
         所以它是「多久没收到新数据」而不是总时长，慢速下载不会被误杀；
      4. 但“一直在动却极慢”得管：官方 CDN 在国内常被限到几 KB/s，
         94MB 的权重要跑五六个小时，所以低于 MIN_SPEED 就主动放弃，
         把机会让给下一条通道。
    """
    part = dest.with_name(dest.name + ".part")
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **headers})
        with urllib.request.urlopen(req, timeout=60) as resp, open(part, "wb") as fh:
            total = int(resp.headers.get("Content-Length") or 0)
            log(f"  通道 [{label}] 已连通，共 {total / 1048576:.1f} MB")
            done = 0
            last_print = 0.0
            last_pct = -10.0
            started = time.time()
            while True:
                try:
                    chunk = resp.read(1 << 16)
                except (TimeoutError, OSError) as exc:
                    raise RuntimeError(f"传输中断（已收 {done / 1048576:.1f} MB）：{exc}") from exc
                if not chunk:
                    break
                fh.write(chunk)
                done += len(chunk)
                now = time.time()
                # 速度卡点放在打印节流之前：否则非终端下每 10% 才查一次，
                # 一条几 KB/s 的死通道要白耗好几分钟才会被放弃
                speed = done / max(0.001, now - started) / 1048576
                if now - started > MIN_SPEED_GRACE and speed < MIN_SPEED:
                    raise RuntimeError(f"速度过低（{speed * 1024:.0f} KB/s），换下一条通道")
                pct = done * 100.0 / total if total else 0.0
                finished = bool(total) and done >= total
                if IS_TTY:
                    if now - last_print < 0.4 and not finished:
                        continue
                    last_print = now
                else:
                    if pct - last_pct < 10 and not finished:
                        continue
                    last_pct = pct
                if total:
                    line = (f"[katago-setup]   {pct:5.1f}%  "
                            f"{done / 1048576:6.1f}/{total / 1048576:.1f} MB  "
                            f"{speed:4.1f} MB/s  {'#' * int(pct // 5):<20}")
                else:
                    line = f"[katago-setup]   {done / 1048576:6.1f} MB  {speed:4.1f} MB/s"
                print(("\r" + line) if IS_TTY else line,
                      end="" if IS_TTY else "\n", flush=True)
            if IS_TTY:
                print(flush=True)
            if total and done != total:
                raise RuntimeError(f"下载不完整：{done}/{total} 字节")
        part.replace(dest)
        log(f"完成：{dest.name}（{dest.stat().st_size / 1048576:.1f} MB）")
        return True
    except Exception as exc:   # noqa: BLE001
        print(flush=True)
        log(f"  通道 [{label}] 失败：{exc}")
        part.unlink(missing_ok=True)
        return False


def mirror_url(browser: str, mirror: str) -> str:
    """把 GitHub 直链套上镜像前缀，兼容两种常见写法。

    只给域名（https://gh-proxy.com）和给完整前缀
    （https://gh-proxy.com/https://github.com，早期 ghproxy 的用法）都要能拼对，
    否则会得到一个丢掉了 https://github.com 的错误地址，直接 404。
    """
    prefix = mirror.rstrip("/")
    if prefix.endswith("github.com"):
        return browser.replace("https://github.com", prefix)
    return f"{prefix}/{browser}"


def download_asset(asset: dict, dest: Path, mirror: str = "") -> bool:
    """下载一个 release 资产，依次尝试多条通道。

    为什么要多通道：国内网络下 GitHub 各域名的可达性与吞吐差异极大。实测：
      · api.github.com                    通，但只有 ~5 KB/s（94MB 要 5 小时）
      · github.com / objects.githubusercontent.com  可能 TLS 握手超时
      · 第三方镜像（gh-proxy.com 等）       ~150 KB/s，快两个数量级
    用户显式给了 --mirror 就把它放第一位：否则先试官方通道会白等十几分钟。
    没给镜像时仍然官方优先（不经第三方中转，供应链更干净）。
    """
    if dest.exists() and dest.stat().st_size > 1024:
        log(f"已存在，跳过：{dest.name}")
        return True
    name = asset.get("name", dest.name)
    log(f"下载 {name}（Release {asset.get('_tag', '?')}）")

    channels: list[tuple[str, str, dict]] = []
    browser = asset.get("browser_download_url", "")
    if mirror and browser:
        channels.append((f"镜像 {mirror.split('//')[-1].rstrip('/')}",
                         mirror_url(browser, mirror), {}))
    if asset.get("url"):
        channels.append(("api.github.com", asset["url"],
                         {"Accept": "application/octet-stream"}))
    if browser:
        channels.append(("GitHub 直链", browser, {}))
    if not channels:
        log("该资产没有可用的下载地址")
        return False

    for label, url, headers in channels:
        if _stream_download(url, dest, headers, label):
            return True
    log(f"{name} 所有通道都失败。可换镜像重试（实测较快的几个）："
        f"--mirror https://gh-proxy.com  /  --mirror https://ghproxy.net")
    return False


def _lib_dirs(extra: list[Path]) -> list[Path]:
    dirs = [Path(d) for d in os.environ.get("PATH", "").split(os.pathsep) if d]
    dirs += extra
    if os.name == "nt":
        dirs.append(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32")
    return dirs


def _has_lib(patterns: list[str], extra: list[Path]) -> bool:
    for folder in _lib_dirs(extra):
        try:
            if not folder.is_dir():
                continue
            for pat in patterns:
                if next(folder.glob(pat), None) is not None:
                    return True
        except OSError:
            continue
    return False


def _opencl_runtime_present() -> bool:
    """OpenCL 运行时是否可用（显卡驱动一般会带，不需要单独装 SDK）。"""
    if os.name == "nt":
        system32 = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"
        return (system32 / "OpenCL.dll").exists()
    return _has_lib(["libOpenCL.so*"], [Path("/usr/lib/x86_64-linux-gnu")])


def detect_backend() -> str:
    """没显式指定时自动挑引擎后端：opencl > eigen。

    **不自动选 cuda**，这是踩过坑的结论：
      1. 官方 cuda 包不自带 cudnn（只有 ~10MB），要求本机已装版本严格匹配的
         CUDA + cuDNN，否则直接报缺 DLL；
      2. 想靠扫描 cudart64*.dll 判断“装没装 CUDA”会误判——比如 NVIDIA PhysX
         目录下就常年躺着一个 cudart64_65.dll（CUDA 6.5），跟现代版完全无关。
    opencl 靠显卡驱动自带的运行时，N 卡/A 卡/Intel 核显都能跑，
    RTX 系列上速度接近 cuda；连 OpenCL 都没才退到纯 CPU 的 eigen。
    确实装了完整 CUDA Toolkit 的机器可以显式 --backend cuda，
    而且自检失败时会自动降级，不会把平台卡在不能用的引擎上。
    """
    return "opencl" if _opencl_runtime_present() else "eigen"


def _extract_all(archive: Path, url: str, dest_dir: Path, exe_name: str) -> bool:
    """把整个引擎包解压到 dest_dir（拍平压缩包内的顶层目录）。

    不能只取 katago.exe：cuda/opencl 版依赖随包的 cudnn、cudart、OpenCL 等动态库，
    缺一个就起不来；eigen 版也可能带 libgcc/libstdc++ 运行库。
    只取文件名（不拼接包内路径）顺带避开了 zip slip 路径穿越。
    """
    names: list[str] = []
    try:
        if url.endswith(".zip"):
            with zipfile.ZipFile(archive) as zf:
                for info in zf.infolist():
                    if info.is_dir():
                        continue
                    name = Path(info.filename.replace("\\", "/")).name
                    if not name:
                        continue
                    with zf.open(info) as src, open(dest_dir / name, "wb") as dst:
                        shutil.copyfileobj(src, dst)
                    names.append(name)
        else:
            with tarfile.open(archive) as tf:
                for member in tf.getmembers():
                    if not member.isfile():
                        continue
                    name = Path(member.name).name
                    if not name:
                        continue
                    src = tf.extractfile(member)
                    if src is None:
                        continue
                    with open(dest_dir / name, "wb") as dst:
                        shutil.copyfileobj(src, dst)
                    names.append(name)
    except Exception as exc:   # noqa: BLE001
        log(f"解压失败：{exc}")
        return False
    if exe_name not in names:
        log(f"压缩包里没找到 {exe_name}（内容示例：{names[:8]}）")
        return False
    exe = dest_dir / exe_name
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    dlls = [n for n in names if n.lower().endswith((".dll", ".so", ".dylib"))]
    log(f"引擎已解压：{len(names)} 个文件（含 {len(dlls)} 个依赖库）→ {dest_dir}")
    return True


def verify_engine(exe: Path) -> tuple[bool, str]:
    """跑一次 `katago version`，确认这台机器真的能启动它。

    下载解压成功不等于能用：cuda 版可能因驱动/cudnn 不匹配而报缺 DLL，
    opencl 版可能找不到可用的 GPU 平台。把原始报错抛给用户比默默降级好。
    """
    try:
        proc = subprocess.run([str(exe), "version"], capture_output=True, text=True,
                              timeout=120, cwd=str(exe.parent),
                              encoding="utf-8", errors="replace",
                              creationflags=CREATE_NO_WINDOW)
    except Exception as exc:   # noqa: BLE001
        return False, f"无法启动引擎：{exc}"
    out = ((proc.stdout or "") + (proc.stderr or "")).strip()
    return proc.returncode == 0, out[:800]


def install_engine(mirror: str = "", backend: str = "opencl") -> bool:
    """下载并安装引擎，自检失败时自动沿 BACKEND_FALLBACK 降级重试。

    宁可多试几个后端，也不要让用户拿到一个“装上了但跑不起来”的引擎：
    后者会让平台静默降级到启发式引擎，用户还以为是 KataGo 在工作。
    """
    exe_name = "katago.exe" if os.name == "nt" else "katago"
    exe = HERE / exe_name
    if exe.exists():
        good, output = verify_engine(exe)
        if good:
            log(f"引擎已存在且可运行：{first_line(output)}")
            return True
        log(f"现有引擎跑不起来，重新安装：{first_line(output)}")

    order = [backend] + [b for b in BACKEND_FALLBACK.get(backend, []) if b != backend]
    last_output = ""
    for cand in order:
        log(f"—— 尝试 {cand} 版引擎")
        asset = resolve_engine_asset(cand)
        if asset is None:
            continue
        archive_name = asset.get("name", "")
        tmp = HERE / ("_katago.zip" if archive_name.endswith(".zip") else "_katago.tar.gz")
        if not download_asset(asset, tmp, mirror):
            continue
        try:
            if not _extract_all(tmp, archive_name, HERE, exe_name):
                continue
        finally:
            tmp.unlink(missing_ok=True)

        good, output = verify_engine(exe)
        if good:
            log(f"引擎自检通过（{cand} 版）：{first_line(output)}")
            if cand != backend:
                log(f"注意：{backend} 版在本机跑不起来，已自动改用 {cand} 版")
            return True
        last_output = output
        log(f"{cand} 版自检失败：{first_line(output)}")

    log("所有后端都装不上，最后一次的完整报错：")
    for line in (last_output or "(无输出)").splitlines()[:10]:
        log(f"    {line}")
    log("可手动下载 KataGo 并把包内全部文件解压到 backend/katago/；")
    log("在此之前平台会自动使用内置启发式引擎，不影响先把其他部分跑起来。")
    return False


def install_networks(mirror: str = "", small: bool = False) -> bool:
    """下载网络权重。

    主网络 b18c384nbt-humanv0 同时带标准分析头与 human SL 头，既能做主分析
    又能做级位档拟人采样（-human-model 指向同一个文件，省一半流量）。
    代价是全局分析会带上 humanSLProfile 的人类先验；想要纯 AI 口径就把
    --small 拿到的标准网络手动改为主模型（后端会自动识别 models/ 里的文件）。
    """
    tag, asset_name, save_name = MAIN_NETWORK
    dest = MODELS_DIR / save_name
    if dest.exists() and dest.stat().st_size > 1024:
        log(f"主网络已存在，跳过：{dest.name}")
        ok = True
    else:
        log("主网络：b18c384nbt-humanv0（主分析 + human SL 拟人棋风，同一个文件）")
        asset = find_asset(tag, lambda n: n == asset_name)
        if asset is None:
            log(f"在 Release {tag} 里没找到 {asset_name}")
            log(f"可手动下载任意 KataGo 网络（*.bin.gz）放进 {MODELS_DIR}，后端会自动识别")
            ok = False
        else:
            ok = download_asset(asset, dest, mirror)

    if small:
        stag, sname, ssave = SMALL_NETWORK
        sasset = find_asset(stag, lambda n: n == sname)
        if sasset is None:
            log(f"在 Release {stag} 里没找到轻量网络 {sname}")
            ok = False
        else:
            # 结果必须计入返回值：用户显式要求的下载失败却报「全部完成」，
            # 会让人以为装好了（实测镜像中途掉速就会走到这里）
            ok = download_asset(sasset, MODELS_DIR / ssave, mirror) and ok
    return ok


def write_default_config() -> None:
    """生成/修复 analysis.cfg。

    **必须在网络权重就位之后调用**：主网络是不是 human SL 网络（文件名带 human）
    决定了要不要写 humanSLProfile —— 漏了它，用 humanv0 当 -model 时第一条查询
    就 FATAL ERROR: SGFMetadata is required，引擎直接退出。
    这里不自己拼配置，直接走后端启动时的同一份逻辑，避免两处对不上。
    """
    try:
        from app.engine.katago import KataGoEngine
    except Exception as exc:                                  # noqa: BLE001
        log(f"导入后端模块失败，跳过配置生成（{exc}）；后端启动时会自动补")
        return
    cfg = HERE / "analysis.cfg"
    existed = cfg.exists()
    KataGoEngine().ensure_config()
    log(f"配置已{'校正' if existed else '生成'}：{cfg}"
        f"（缺必需项时会自动重写并备份 .bak）")


def main() -> int:
    ap = argparse.ArgumentParser(description="下载 KataGo 引擎与网络权重")
    ap.add_argument("--mirror", default="", help="GitHub 镜像前缀（官方通道在国内可能只有几 KB/s），"
                                                 "实测较快：https://gh-proxy.com")
    ap.add_argument("--backend", default="auto",
                    help="引擎后端：auto(默认，opencl 优先、无则 eigen) / eigen(CPU) / opencl / cuda / trt；"
                         "选定后端自检失败时会自动降级重试")
    # 主网络 b18c384nbt-humanv0 本身就带 human SL 头，不需要单独的 human 网络；
    # 保留这个开关只为兼容旧命令行（静默忽略）
    ap.add_argument("--no-human", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--model-only", action="store_true", help="只下载权重，不动引擎")
    ap.add_argument("--small", action="store_true", help="额外下载轻量网络（b10c384，CPU 更快）")
    args = ap.parse_args()

    sys.path.insert(0, str(HERE.parent))     # 让 app.* 可导入
    MODELS_DIR.mkdir(parents=True, exist_ok=True)

    backend = (args.backend or "auto").lower()
    if backend == "auto":
        backend = detect_backend()
        reason = {
            "opencl": "OpenCL 运行时可用（显卡驱动自带）",
            "eigen": "没检测到 OpenCL 运行时，用纯 CPU 版",
        }.get(backend, "")
        log(f"自动选择引擎后端：{backend}（{reason}）")

    ok = True
    if not args.model_only:
        ok = install_engine(args.mirror, backend) and ok

    ok = install_networks(args.mirror, small=args.small) and ok

    # 配置放在权重之后生成：要不要写 humanSLProfile 取决于主网络的文件名
    write_default_config()

    log("全部完成" if ok else "部分失败：可稍后重试，或手动放置文件；"
                            "在此之前平台会使用内置启发式引擎")
    if ok:
        log("重启后端即可生效（重启客户端或启动器，内嵌后端会自动带上新引擎）")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
