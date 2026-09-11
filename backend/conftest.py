"""pytest 配置：确保 backend/ 在 sys.path 上，`import app.*` 可用。"""
from __future__ import annotations

import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

# 测试期间禁用真实引擎与外部服务，避免用例依赖本机 KataGo / 网络
import os  # noqa: E402

os.environ.setdefault("GO_KATAGO_ENABLED", "false")
os.environ.setdefault("GO_LLM_ENABLED", "false")

#: 测试库**不放在 `data/` 里**（审计 1.22 低）：那是生产数据目录，
#: 历轮跑测试在那里留下过 `test.db` 与其 WAL，混着几十个测试账号，
#: 排障时和真实用户数据一起列出来极易误判。挪到独立目录，并进 .gitignore。
TEST_DB_DIR = BACKEND_DIR / ".pytest_tmp"
TEST_DB = TEST_DB_DIR / "test.db"
os.environ.setdefault("GO_DATABASE_URL", f"sqlite:///{TEST_DB.as_posix()}")

import pytest  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _clean_test_db():
    """每次跑测试前清掉测试库，避免用户名唯一约束与脏数据干扰。"""
    TEST_DB_DIR.mkdir(parents=True, exist_ok=True)
    _try_unlink(TEST_DB)
    yield
    # Windows 下 SQLite 连接未释放时文件可能仍被占用，清理失败不影响测试结果
    _try_unlink(TEST_DB)

    _try_unlink(TEST_DB.with_name(TEST_DB.name + "-wal"))
    _try_unlink(TEST_DB.with_name(TEST_DB.name + "-shm"))
    try:
        TEST_DB_DIR.rmdir()
    except OSError:
        pass


def _try_unlink(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass
