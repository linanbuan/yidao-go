"""KataGo analysis engine 异步封装。

以子进程方式常驻 `katago analysis`，通过 stdin/stdout 交换 JSON 行协议。
一个进程可并发处理多条查询（按 id 分发），因此用「单读取协程 + Future 字典」实现。

若二进制或权重缺失，`start()` 返回 False，上层自动降级到内置启发式引擎，
保证平台在没有 KataGo 的机器上依然完整可用（只是棋力与分析精度下降）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import time
from pathlib import Path
from typing import Optional

from ..config import DATA_DIR, settings
from .protocol import AnalysisQuery, AnalysisResult, parse_multi_response, parse_response

logger = logging.getLogger("go.engine")

#: katago.exe 是控制台程序：宿主（pythonw 内嵌后端）自己没有控制台时，Windows 会
#: 给子进程**新开一个终端窗口，且引擎活多久开多久**（lifespan 预热即触发，玩家一
#: 启动就看到一个黑框）。CREATE_NO_WINDOW 压掉它；POSIX 上 getattr 拿不到该常量
#: 得 0，而 Popen 对 creationflags=0 不挑平台，跨平台安全。
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

DEFAULT_ANALYSIS_CFG = """# KataGo analysis engine 配置（本项目自动生成，可按硬件调整）
#
# 注意：numAnalysisThreads 与 numSearchThreadsPerAnalysisThread 是 analysis 模式的
# **必需项**，缺任何一个都会启动即退：
#     Uncaught exception: Could not find key 'numAnalysisThreads' in config file
# 旧参数名 numSearchThreads 在 v1.16+ 已被 numSearchThreadsPerAnalysisThread 取代。
logSearchInfo = false
reportAnalysisWinratesAs = SIDETOMOVE

# 并发与搜索规模：教学平台同时进行的对局不多，2 个分析线程够用；
# 每线程 8 条搜索线程，在 8GB 显存的笔记本 GPU 上仍留有余量
numAnalysisThreads = 2
numSearchThreadsPerAnalysisThread = 8
maxVisits = 512
maxPlayouts = 512
maxTime = 30

# 神经网络缓存：2^22 约 400 万条，b18 网络在 16GB 内存 / 8GB 显存上安全
nnMaxBatchSize = 32
nnCacheSizePowerOfTwo = 22
nnMutexPoolSizePowerOfTwo = 16
nnRandomize = true
"""

# analysis 模式启动即退的常见原因：配置缺必需键。发现旧配置不完整就重写，
# 否则升级 KataGo 后用户会卡在“引擎装好了但永远降级到启发式”的状态
REQUIRED_CFG_KEYS = ("numAnalysisThreads", "numSearchThreadsPerAnalysisThread",
                     "nnCacheSizePowerOfTwo", "nnMutexPoolSizePowerOfTwo")

# 预热查询的超时。OpenCL / CUDA 后端首次运行要编译并调优 GPU 内核（几分钟，
# 结果写进缓存后下次就快了），再加上加载 90MB+ 网络，比稳态查询慢一个数量级；
# 用 settings.katago_timeout（60s）会让首次启动必然失败并永久降级。
WARMUP_TIMEOUT = 600.0

# 一条响应算不算「真结果」。KataGo 对不认识的查询字段会先用同一个 id
# 回一条 {"warning": "Unexpected or unused field"}，它不是结果，不能拿去唤醒 Future
RESULT_KEYS = ("moveInfos", "rootInfo", "moveAnalysis", "error")

# 主网络是 human SL 网络（如 b18c384nbt-humanv0）时，配置里**必须**有这一项，
# 否则第一条查询就报
#     FATAL ERROR: SGFMetadata is required for ... but was not initialized
# 并把整个引擎进程杀掉（实测：不写则 100% 起不来）。
#
# 取 preaz_9d（最强的人类档位）而不是关掉人类先验，是权衡后的确定选择：
#   * 试过查询级的 ignoreHumanSLProfile（KataGo 较新版本支持），v1.17.1 不认，
#     会回一条字段告警且结果与基线一致 —— 没有零成本的「纯 AI 口径」开关；
#   * 标准 b18 只在 media.katagotraining.org 上，本机 DNS 解析失败，拿不到；
#     GitHub release 里的 b18 只有 humanv0 这一个。
# 对 18级~九段这个全是人类段位的教学场景，人类高段先验反而更贴近学员；
# 弱档位再用 overrideSettings.humanSLProfile 单独降下去（已实测生效：
# preaz_18k 与 preaz_9d 在同一局面的目差能差 1.4 目）。
# 以后拿到标准网络并改为主模型后，这一行会自动不再写入（见 ensure_config）。
HUMAN_SL_DEFAULT_PROFILE = "preaz_9d"


def _is_human_net(path: str) -> bool:
    """文件名带 human 的就是 human SL 网络（官方命名惯例，如 -humanv0）。"""
    return "human" in Path(path).name.lower()


class KataGoEngine:
    def __init__(self, bin_path: str = "", model_path: str = "", config_path: str = "",
                 human_model_path: str = "", timeout: float = 60.0):
        self.bin_path = bin_path or settings.katago_bin
        self.model_path = self._resolve_model(model_path or settings.katago_model)
        self.config_path = config_path or settings.katago_config
        self.human_model_path = self._resolve_human_model(
            human_model_path or settings.katago_human_model, self.model_path)
        self.timeout = timeout or settings.katago_timeout
        self.proc: Optional[asyncio.subprocess.Process] = None
        self._pending: dict[str, asyncio.Future] = {}
        self._expect: dict[str, int] = {}    # analyzeTurns：每个 id 应收几行
        self._partial: dict[str, list] = {}  # 已收到但还没攒齐的行
        self._reader_task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()          # 保证写 stdin 原子性
        self._stderr_file = None
        self._stderr_path = DATA_DIR / "logs" / "katago.stderr.log"
        self.available = False
        self.warming = False                 # 已 spawn、正在跑预热查询
        self.version = ""
        self.error = ""
        # 断链可观测性：光靠 available 分不出「没装」与「装了但半路死了」。
        # _stopping 用来把主动关停和意外死亡分开——两者在读循环里都只是一个 EOF，
        # 不分的话每次重启服务都会虚增一条「引擎崩了」的记录。
        self._stopping = False
        self.death_count = 0
        self.last_death_at: Optional[float] = None
        self.last_death_reason = ""        # 恢复后 error 会被清空，死因单独留着
        self.dead_event = asyncio.Event()    # 引擎池的看门狗等这个信号

    # ------------------------------------------------------------------
    @property
    def stderr_log_path(self) -> Path:
        """引擎自己的错误日志路径（崩溃原因只走这里），供看门狗与状态接口引用。"""
        return self._stderr_path

    @staticmethod
    def _resolve_model(configured: str) -> str:
        """配置的网络文件不在时，自动在 models/ 里按优先级挑一个。

        下载脚本拿到的文件名会随 KataGo 官方 release 变（比如现在用的是
        b18c384nbt-humanv0.bin.gz），而配置里写的是 kata_b18c384nbt.bin.gz；
        硬卡文件名会造成“权重明明下好了却识别不到”，白白降级到启发式引擎。
        """
        path = Path(configured)
        if path.exists():
            return configured
        folder = path.parent
        if not folder.is_dir():
            return configured
        nets = [p for p in folder.glob("*.bin.gz") if p.stat().st_size > 1024]
        if not nets:
            return configured
        prefer = ("b18c384nbt", "b18", "b15", "b14", "b11", "b10", "b6")

        def rank(p: Path) -> tuple:
            name = p.name.lower()
            for i, key in enumerate(prefer):
                if key in name:
                    return (i, -p.stat().st_size)
            return (len(prefer), -p.stat().st_size)

        nets.sort(key=rank)
        logger.info("网络权重 %s 不存在，改用同目录下发现的 %s", path.name, nets[0].name)
        return str(nets[0])

    @staticmethod
    def _resolve_human_model(configured: str, main_model: str) -> str:
        """human SL 网络（级位～低段的拟人棋风）：找不到就试主网络能不能兼任。

        b18c384nbt-humanv0 这类网络同时带标准分析头与 human 头，
        一个文件可以同时做 -model 与 -human-model；真的没有就返回空，
        启动时不传 -human-model，只是级位棋风拟人度下降，不影响其他功能。
        """
        if configured and Path(configured).exists():
            return configured
        folder = Path(main_model).parent
        if folder.is_dir():
            humans = sorted(p for p in folder.glob("*human*.bin.gz")
                            if p.stat().st_size > 1024)
            if humans:
                return str(humans[0])
        if "human" in Path(main_model).name.lower():
            return main_model
        return ""

    # ------------------------------------------------------------------
    def ensure_config(self) -> None:
        cfg = Path(self.config_path)
        # human SL 网络当主模型时多一个必需键，不满足就重写配置
        required = list(REQUIRED_CFG_KEYS)
        if _is_human_net(self.model_path):
            required.append("humanSLProfile")
        if cfg.exists():
            try:
                lines = cfg.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                lines = []
            keys = {ln.split("=", 1)[0].strip() for ln in lines
                    if "=" in ln and not ln.strip().startswith("#")}
            missing = [k for k in required if k not in keys]
            if not missing:
                return
            backup = cfg.with_name(cfg.name + ".bak")
            try:
                cfg.replace(backup)
                logger.warning("KataGo 配置缺必需项 %s，已重写（旧文件备份为 %s）",
                               missing, backup.name)
            except OSError:
                logger.warning("KataGo 配置缺必需项 %s，直接覆盖重写", missing)
        text = DEFAULT_ANALYSIS_CFG
        if _is_human_net(self.model_path):
            text += (f"\n# 主网络 {Path(self.model_path).name} 是 human SL 网络：缺这一行时"
                     f"\n# 第一条查询就会 FATAL ERROR: SGFMetadata is required，引擎直接退出。"
                     f"\n# 换成标准网络做 -model 后可以删掉（弱档位棋风仍走 overrideSettings）。"
                     f"\nhumanSLProfile = {HUMAN_SL_DEFAULT_PROFILE}\n")
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text(text, encoding="utf-8")
        logger.info("已生成 KataGo 配置: %s", cfg)

    def rebind_loop(self) -> None:
        """换 event loop 时重建 asyncio 原语（由引擎池在 `startup()` 里调）。

        为何需要：`pool` 是模块级单例，而 `asyncio.Lock/Event` 一旦被某个循环用过就
        永久绑死它（Py3.10+ 的 `_LoopBoundMixin` 在首次 await 时记下 loop，之后从
        别的循环再 await 就是 `RuntimeError: ... is bound to a different event loop`）。
        桌面端把后端跑在同一进程里，重启服务（看门狗自愈、换端口）会新建一个
        事件循环并复用这同一个对象 —— 于是第二次启动的看门狗必然炸。
        **没**开 KataGo 时看不出来：preflight 失败就不创建看门狗任务，根本不会有第二次 await。
        """
        self._lock = asyncio.Lock()
        self.dead_event = asyncio.Event()
        if self._pending:
            # 在飞的查询属于旧循环，新循环里永远不会被唤醒。这里只拆引用而不给异常：
            # 旧循环已经关掉，对它的 Future 调 set_exception 会在 `call_soon` 上再抛一个
            # `Event loop is closed`，把真正的现场（下面这条告警）淹掉。
            logger.warning("换事件循环时仍有 %d 个在飞查询，直接作废", len(self._pending))
            self._pending.clear()
            self._expect.clear()
            self._partial.clear()

    def preflight(self) -> bool:
        """检查二进制与权重是否就位。"""
        if not settings.katago_enabled:
            self.error = "配置中已禁用 KataGo（GO_KATAGO_ENABLED=false）"
            return False
        if not Path(self.bin_path).exists():
            self.error = f"未找到 KataGo 可执行文件: {self.bin_path}"
            return False
        if not Path(self.model_path).exists():
            self.error = f"未找到网络权重: {self.model_path}（运行 python katago/download.py）"
            return False
        self.ensure_config()
        return True

    async def start(self) -> bool:
        # 断链重启时上一轮的子进程与 stderr 句柄还挂着，先回收再 spawn，
        # 否则每自愈一次就泄漏一个文件句柄、甚至多占一份几百 MB 显存
        await self._reap_leftovers()
        self._stopping = False
        if not self.preflight():
            logger.warning("KataGo 不可用：%s —— 自动降级到内置启发式引擎", self.error)
            return False
        cmd = [self.bin_path, "analysis", "-config", self.config_path, "-model", self.model_path]
        if self.human_model_path and Path(self.human_model_path).exists():
            cmd += ["-human-model", self.human_model_path]
            logger.info("已加载 human SL 模型（用于级位～低段的拟人棋风）")

        # stderr 必须落盘：analysis 模式的失败原因（配置缺必需键、枚举不到 GPU、
        # 网络权重头不匹配）只走 stderr，丢给 DEVNULL 就等于把唯一的诊断信息
        # 扔了，最后只能手动敲命令行去复现。
        self._stderr_path.parent.mkdir(parents=True, exist_ok=True)
        if self.death_count:
            # 断链重启：先把上一次的引擎 stderr 存档。下面用 "wb" 打开会直接
            # 抹掉死因，而重启之后恰好是最需要知道上次为什么死的时候。
            try:
                if self._stderr_path.exists():
                    self._stderr_path.replace(
                        self._stderr_path.with_name(self._stderr_path.name + ".prev"))
            except OSError:
                pass
        stderr_target: object = asyncio.subprocess.DEVNULL
        try:
            self._stderr_file = open(self._stderr_path, "wb", buffering=0)
            stderr_target = self._stderr_file
        except OSError as exc:
            logger.warning("无法写入 KataGo 错误日志 %s：%s", self._stderr_path, exc)

        self.warming = True
        try:
            self.proc = await asyncio.create_subprocess_exec(
                *cmd, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=stderr_target, creationflags=CREATE_NO_WINDOW,
            )
        except Exception as exc:   # noqa: BLE001
            self.error = f"启动 KataGo 失败: {exc}"
            logger.warning(self.error)
            self.warming = False
            self._close_stderr()
            return False

        self._reader_task = asyncio.create_task(self._read_loop())
        # 预热查询：确认引擎真的能应答（光看进程活着不够，GPU 初始化失败也是活的）
        logger.info("KataGo 启动中（首次运行需做 GPU 内核调优，可能要几分钟）…")
        try:
            await self.query(AnalysisQuery(size=19, moves=[], max_visits=1,
                                           include_ownership=False, include_policy=False),
                             timeout=WARMUP_TIMEOUT)
        except Exception as exc:   # noqa: BLE001
            detail = self._stderr_tail()
            self.error = f"KataGo 预热查询失败: {exc}"
            if detail:
                self.error += f"\n引擎输出: {detail}"
            logger.warning("%s\n完整日志: %s", self.error, self._stderr_path)
            await self.stop()
            return False
        except BaseException as exc:   # noqa: BLE001
            # 包括 CancelledError：服务关停或超时打断预热时也必须把已 spawn 的
            # 子进程带走，否则留在系统里就是一个占几百 MB 显存的孤儿 katago.exe
            logger.warning("KataGo 预热被打断: %r", exc)
            await self.stop()
            raise

        self.warming = False
        self.available = True
        self.error = ""                  # 死因已转入 lastDeathReason，留着会误导
        self.version = "KataGo analysis engine"
        logger.info("KataGo 就绪：%s", self.model_path)
        return True

    # ------------------------------------------------------------------
    def _close_stderr(self) -> None:
        if self._stderr_file is not None:
            try:
                self._stderr_file.close()
            except OSError:
                pass
            self._stderr_file = None

    def _stderr_tail(self, lines: int = 4, limit: int = 500) -> str:
        """取引擎自己吐的最后几行错误，拼进 self.error 让上层直接看见真因。"""
        try:
            text = self._stderr_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
        kept = [ln.strip() for ln in text.splitlines() if ln.strip()]
        return " | ".join(kept[-lines:])[:limit]

    def _mark_dead(self, reason: str) -> None:
        """引擎不再应答：置降级位并通知看门狗。

        读循环里的 EOF 既可能是引擎崩了，也可能是服务正常关停，靠 _stopping
        分开；归错类的代价是日志里一堆“引擎崩了”的假记录，以及真断链看不出来。
        """
        self.available = False
        if self._stopping:
            logger.info("KataGo 输出流结束（服务主动关停，不计断链）")
            return
        self.death_count += 1
        self.last_death_at = time.time()
        self.last_death_reason = reason
        self.error = reason
        self.dead_event.set()
        logger.warning("KataGo 断链（第 %d 次）：%s", self.death_count, reason)

    async def _reap_leftovers(self) -> None:
        """回收上一轮残留的子进程与 stderr 句柄（重启前调用）。

        断链时读循环只是退出，进程与文件句柄都还在；不回收的话每自愈一次就
        漏一个句柄，而那个进程可能还占着几百 MB 显存（新进程就起不来了）。
        """
        task = self._reader_task
        self._reader_task = None
        if task is not None and not task.done() and task is not asyncio.current_task():
            task.cancel()
            # 必须 await：不 await 的话读循环还在跑，它可能在下一次 start() 之后
            # 才处理完最后一批数据、把缓存的 future 唤醒成「上一局的迟到响应」
            # （审计 1.9 的窗口就是这里放大的）。
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:   # noqa: BLE001
                pass
        proc, self.proc = self.proc, None
        if proc is None:
            self._close_stderr()
            return
        if proc.returncode is None:
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                except Exception:   # noqa: BLE001
                    pass
            except Exception:   # noqa: BLE001
                pass
        # 退出码是「自己崩了」与「被人杀了」的唯一现场证据（EOF 本身分不出这两件事）
        logger.info("上一轮 KataGo 进程退出码：%s", proc.returncode)
        self._close_stderr()

    def _fail_pending(self, reason: str) -> None:
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(RuntimeError(reason))
        self._pending.clear()
        self._expect.clear()
        self._partial.clear()

    async def stop(self) -> None:
        self._stopping = True      # 主动关停：读循环接下来看到的 EOF 不算断链
        self.available = False
        self.warming = False
        task = self._reader_task
        self._reader_task = None
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            try:
                await task         # 等它真退出，不让它在本函数返回后又改状态
            except asyncio.CancelledError:
                pass
            except Exception:   # noqa: BLE001
                pass
        if self.proc and self.proc.returncode is None:
            try:
                self.proc.terminate()
                await asyncio.wait_for(self.proc.wait(), timeout=5)
            except Exception:   # noqa: BLE001
                try:
                    self.proc.kill()
                except Exception:   # noqa: BLE001
                    pass
        self.proc = None
        self._fail_pending("引擎已停止")
        self._close_stderr()

    # ------------------------------------------------------------------
    async def _read_loop(self) -> None:
        assert self.proc is not None and self.proc.stdout is not None
        # 手动分块读 + 自己切行。原因：KataGo 的 analyzeTurns 每手回一行 JSON，
        # 19 路高 visit 时单行可达几百 KB（候选数 × 变化图），而 asyncio 的
        # readline/readuntil 有 64KB 硬上限，超限抛 LimitOverrunError 且已读数据
        # 被直接丢弃——读循环死亡后所有在飞查询只能干等超时（表现就是日志里
        # 那条空错误信息的「批量分析失败」）。
        buffer = b""
        while True:
            try:
                chunk = await self.proc.stdout.read(65536)
            except Exception as exc:   # noqa: BLE001
                reason = f"KataGo 输出读取失败: {exc!r}"
                self._mark_dead(reason)
                self._fail_pending(reason)
                break
            if not chunk:      # EOF
                if buffer.strip():
                    self._dispatch_line(buffer)
                # 这一支**不拼 stderr 尾部**（原来拼了，后果见下）。实测：真杀一次
                # katago.exe，设置页那行变成「运行中断链 1 次：Loaded config … |
                # Loaded model … | Started, ready to begin handling requests」——
                # 学员读到的是「引擎好好的」而它刚被杀。致命错误走的是 stdout
                # （已由 `_dispatch_line` 存进 self.error），运行期的 stderr 里通常只剩
                # 开机那几行。退出码在 `_reap_leftovers` 里进日志，日志路径在
                # `status()` 里交给界面（stalled 那一态会直接把路径写给用户）。
                code = self.proc.returncode if self.proc is not None else None
                reason = self.error or "KataGo 输出流结束（进程被结束或自行退出）"
                if code is not None:
                    # 退出码是「自己崩了」与「被人杀了」的唯一现场证据（1.24）
                    reason = f"{reason}（退出码 {code}）"
                self._mark_dead(reason)
                # 不等超时：让挂着 Future 的调用方立刻得到错误并降级
                self._fail_pending(reason)
                break
            buffer += chunk
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                if line.strip():
                    self._dispatch_line(line)

    def _dispatch_line(self, line: bytes) -> None:
        """处理引擎的一行输出（JSON 结果 / 告警 / 致命错误文本）。"""
        text = line.decode("utf-8", errors="replace").strip()
        if not text:
            return
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            # 致命错误是**纯文本走 stdout** 的（"FATAL ERROR:" 后面跟原因），
            # 不进 stderr；不存下来就只能看到“引擎莫名退出”
            if text.lower().startswith(("fatal", "uncaught", "error", "exception")):
                self.error = text[:500]
                logger.error("KataGo 致命错误: %s", self.error)
            else:
                logger.debug("忽略非 JSON 输出: %s", text[:200])
            return
        if isinstance(data, list):
            # 部分版本的 analyzeTurns 会整包回一个数组（元素共用同一个 id）：
            # 那就直接整包交出，不能逐元素唤醒 Future
            rid = next((str(it["id"]) for it in data
                        if isinstance(it, dict) and it.get("id")), "")
            fut = self._pending.get(rid)
            if fut and not fut.done():
                fut.set_result(data)
            self._expect.pop(rid, None)
            self._partial.pop(rid, None)
            return
        if not isinstance(data, dict):
            return
        if "warning" in data and not any(k in data for k in RESULT_KEYS):
            logger.warning("KataGo 查询字段告警[%s]: %s",
                           data.get("id", "?"), str(data["warning"])[:200])
            return
        rid = str(data.get("id", ""))
        fut = self._pending.get(rid)
        if fut is None or fut.done():
            return
        # 错误响应只有一行，等不到剩下的手，必须立刻交出否则只能等超时
        if "error" in data and not any(k in data for k in ("moveInfos", "moveAnalysis")):
            fut.set_result(data)
            self._expect.pop(rid, None)
            self._partial.pop(rid, None)
            return
        want = self._expect.get(rid, 1)
        if want <= 1:
            fut.set_result(data)
            return
        # analyzeTurns 实测是**每手一行**、id 完全相同（请求 4 手就回 4 行）；
        # 在第一行就 set_result 会让复盘只拿到第 0 手，其余全部 missing
        bucket = self._partial.setdefault(rid, [])
        bucket.append(data)
        if len(bucket) >= want:
            # 按 turnNumber 排序：side_to_move_seq 是按下标对齐的，乱序会错配行棋方
            bucket.sort(key=lambda d: d.get("turnNumber", 0))
            fut.set_result(bucket)
            self._expect.pop(rid, None)
            self._partial.pop(rid, None)

    async def query(self, q: AnalysisQuery, timeout: Optional[float] = None) -> dict:
        """发送一次查询，返回原始 JSON dict。

        守卫只看进程而不能看 `self.available`：后者要等预热查询成功才置位，
        而预热本身就是一次 query —— 拿它做守卫会让启动永远失败（已踩过）。
        """
        if self.proc is None or self.proc.stdin is None:
            raise RuntimeError(self.error or "KataGo 不可用")
        if self.proc.returncode is not None:
            raise RuntimeError(self.error or f"KataGo 已退出（返回码 {self.proc.returncode}）")
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending[q.request_id] = fut
        if q.analyze_turns:
            # 告知 _read_loop 这个 id 要攒够几行才能算完成
            self._expect[q.request_id] = len(q.analyze_turns)
        payload = json.dumps(q.to_json(), ensure_ascii=False) + "\n"
        try:
            async with self._lock:
                self.proc.stdin.write(payload.encode("utf-8"))
                await self.proc.stdin.drain()
            return await asyncio.wait_for(fut, timeout or self.timeout)
        finally:
            self._pending.pop(q.request_id, None)
            self._expect.pop(q.request_id, None)
            self._partial.pop(q.request_id, None)

    # ------------------------------------------------------------------
    async def analyze(self, q: AnalysisQuery, side_to_move: int,
                      turn: int = 0) -> AnalysisResult:
        data = await self.query(q)
        if "error" in data and not data.get("moveInfos") and not data.get("moveAnalysis"):
            # 常见于不被支持的可选字段（如 humanSLProfile 未启用 human 模型时）：去掉后重试一次
            retry = AnalysisQuery(**{**{k: v for k, v in q.__dict__.items()},
                                     "human_sl_profile": "", "include_policy": False,
                                     "request_id": q.request_id + "r"})
            logger.info("KataGo 查询被拒（%s），降级参数重试", data["error"])
            data = await self.query(retry)
        return parse_response(data, q.size, side_to_move, turn=turn, engine_name="katago")

    async def analyze_turns(self, q: AnalysisQuery, turns: list[int],
                            side_to_move_seq: list[int],
                            timeout: Optional[float] = None) -> list[AnalysisResult]:
        """一次查询分析多手（复盘用），turns 为 0-based 的 analyzeTurns。

        timeout 由调用方按手数给（复盘一块 20 手，共用单查询的 60 秒常常不够）；
        为 None 时退回实例默认超时。
        """
        q.analyze_turns = turns
        data = await self.query(q, timeout)
        if isinstance(data, dict) and "error" in data:
            retry = AnalysisQuery(**{**{k: v for k, v in q.__dict__.items()},
                                     "human_sl_profile": "",
                                     "request_id": q.request_id + "r"})
            logger.info("批量分析被拒（%s），降级参数重试", data["error"])
            data = await self.query(retry, timeout)
        return parse_multi_response(data, q.size, side_to_move_seq, engine_name="katago")

    def status(self) -> dict:
        return {
            "name": "katago",
            "available": self.available,
            "warming": self.warming,
            "binary": self.bin_path,
            "model": self.model_path,
            "humanModel": self.human_model_path if Path(self.human_model_path).exists() else "",
            "error": "" if (self.available or self.warming) else self.error,
            "stderrLog": str(self._stderr_path),
            # 断链历史：前端靠它把「没装 KataGo」与「运行中断链」分开提示，
            # 否则会对一个只需要等几秒自愈的引擎说“请重装”
            "deathCount": self.death_count,
            "lastDeathAt": (time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.last_death_at))
                            if self.last_death_at else ""),
            "lastDeathReason": self.last_death_reason,
        }
