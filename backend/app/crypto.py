"""静态加密：把用户填的 LLM API Key 从「明文进 SQLite」改成「密文进库」。

审计 1.22：`users.llm_config` 是 JSON 列，API Key 原样落盘；而 `go_teach.db` 的
ACL 允许 `Authenticated Users: Modify`——共享机器上等于明文泄露，且可被篡改。
当前 0 个用户配置过，属潜在风险，趁没有存量数据时改掉最省事。

两套后端，按平台自动选：

1. **Windows DPAPI**（首选）：`CryptProtectData` 用当前登录用户的凭据做加密，
   密文只有同一台机器的同一用户能解。密钥由系统托管，我们这边不落任何东西。
2. **数据目录密钥文件 + HMAC-SHA256 流加密**（回退，非 Windows 或 DPAPI 不可用）：
   `sha256(key || nonce || counter)` 生成 keystream 做 CTR 式异或，附一个
   HMAC-SHA256 认证标签防篡改。这不是 AES，但**密钥不随库文件走**（库可以被
   `git clean` 删、可被拷走，密钥文件在另一处），足以把「库文件裸奔」降级成
   「需要同时拿到两个文件」。

落盘格式：`enc:v1:<scheme>:<base64>`。**不带 `enc:` 前缀的值一律原样返回**，
所以（a）库里的存量明文不会解不开，（b）把密文送去解也不会二次加密。
"""
from __future__ import annotations

import base64
import ctypes
import hashlib
import hmac
import os
import secrets
from pathlib import Path
from typing import Optional

_PREFIX = "enc:v1:"
KEY_FILE_NAME = "llm_key.bin"


def _data_dir() -> Path:
    # 延迟 import：crypto 可能被 config 之外的东西在极早期 import，
    # 而 config 自己会创建数据目录，这里不想抢先制造副作用。
    from .config import DATA_DIR
    return DATA_DIR


# ---------------------------------------------------------------------------
# Windows DPAPI
# ---------------------------------------------------------------------------

class _Blob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_ulong), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _blob(data: bytes) -> "_Blob":
    buf = ctypes.create_string_buffer(data, len(data))
    return _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))


def _dpapi(data: bytes, *, protect: bool) -> Optional[bytes]:
    """走一次 DPAPI；任何环节不可用都返回 None 让上层回退，不抛。"""
    if os.name != "nt":
        return None
    try:
        crypt32 = ctypes.windll.crypt32
        kernel32 = ctypes.windll.kernel32
        blob_in = _blob(data)
        blob_out = _Blob()
        if protect:
            ok = crypt32.CryptProtectData(
                ctypes.byref(blob_in), None, None, None, None, 0,
                ctypes.byref(blob_out))
        else:
            ok = crypt32.CryptUnprotectData(
                ctypes.byref(blob_in), None, None, None, None, 0,
                ctypes.byref(blob_out))
        if not ok:
            return None
        try:
            return ctypes.string_at(blob_out.pbData, blob_out.cbData)
        finally:
            kernel32.LocalFree(blob_out.pbData)
    except (OSError, AttributeError):
        return None


# ---------------------------------------------------------------------------
# 回退：数据目录密钥 + HMAC-SHA256 流加密
# ---------------------------------------------------------------------------

def _load_key() -> bytes:
    path = _data_dir() / KEY_FILE_NAME
    try:
        if path.exists():
            raw = path.read_bytes()
            if len(raw) == 32:
                return raw
    except OSError:
        pass
    key = secrets.token_bytes(32)
    try:
        path.write_bytes(key)
        try:
            os.chmod(path, 0o600)       # POSIX 下收紧；Windows 靠 ACL
        except OSError:
            pass
    except OSError:
        pass                            # 只读目录：退化为「本次运行有效」
    return key


def _keystream(key: bytes, nonce: bytes, n: int) -> bytes:
    out = bytearray()
    counter = 0
    while len(out) < n:
        out += hashlib.sha256(key + nonce + counter.to_bytes(8, "big")).digest()
        counter += 1
    return bytes(out[:n])


def _file_seal(plain: bytes) -> bytes:
    key = _load_key()
    nonce = secrets.token_bytes(16)
    ct = bytes(a ^ b for a, b in zip(plain, _keystream(key, nonce, len(plain))))
    tag = hmac.new(key, nonce + ct, hashlib.sha256).digest()
    return nonce + tag + ct


def _file_unseal(blob: bytes) -> Optional[bytes]:
    if len(blob) < 48:
        return None
    key = _load_key()
    nonce, tag, ct = blob[:16], blob[16:48], blob[48:]
    if not hmac.compare_digest(tag, hmac.new(key, nonce + ct, hashlib.sha256).digest()):
        return None                     # 密钥换了 / 数据被改过：当作解不开
    return bytes(a ^ b for a, b in zip(ct, _keystream(key, nonce, len(ct))))


# ---------------------------------------------------------------------------
# 对外接口
# ---------------------------------------------------------------------------

def is_sealed(value: str) -> bool:
    return bool(value) and value.startswith(_PREFIX)


def seal(plaintext: str) -> str:
    """明文 → `enc:v1:...`。空串原样返回（语义是「清除」）。"""
    if not plaintext:
        return ""
    if is_sealed(plaintext):
        return plaintext               # 已经是密文，别二次加密
    raw = plaintext.encode("utf-8")
    blob = _dpapi(raw, protect=True)
    if blob is not None:
        scheme = "dpapi"
    else:
        blob, scheme = _file_seal(raw), "file"
    return _PREFIX + scheme + ":" + base64.b64encode(blob).decode("ascii")


def unseal(value: str) -> str:
    """`enc:v1:...` → 明文；解不开或本来就不是密文时原样返回。

    解不开就返回原文（而不是抛）是刻意的：换机器/换用户后 DPAPI 解不出来，
    宁可让上层把它当成一个无效 Key（调用报鉴权失败）也不要 500。
    """
    if not is_sealed(value):
        return value                   # 存量明文 / 空串 / 全局配置
    body = value[len(_PREFIX):]
    try:
        scheme, _, b64 = body.partition(":")
        blob = base64.b64decode(b64, validate=True)
    except (ValueError, TypeError):
        return value
    if scheme == "dpapi":
        out = _dpapi(blob, protect=False)
    elif scheme == "file":
        out = _file_unseal(blob)
    else:
        return value
    if out is None:
        return value
    try:
        return out.decode("utf-8")
    except UnicodeDecodeError:
        return value


def scheme_of(value: str) -> str:
    """返回实际生效的后端，供自检/接口展示用。"""
    if not is_sealed(value):
        return "plaintext"
    return value[len(_PREFIX):].partition(":")[0]


def active_scheme() -> str:
    """不写任何数据地报告「本机会用哪套后端」。"""
    probe = _dpapi(b"probe", protect=True)
    return "dpapi" if probe is not None else "file"
