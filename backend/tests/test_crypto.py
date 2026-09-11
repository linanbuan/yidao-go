"""LLM API Key 静态加密（app/crypto.py）。"""
from __future__ import annotations

from app.crypto import (_file_seal, _file_unseal, active_scheme, is_sealed,
                        scheme_of, seal, unseal)


def test_roundtrip_preserves_the_exact_key():
    key = "sk-abcdef0123456789-中文也要活"
    sealed = seal(key)
    assert is_sealed(sealed)
    assert unseal(sealed) == key


def test_ciphertext_does_not_leak_the_plaintext():
    sealed = seal("sk-super-secret-value")
    assert "sk-super-secret-value" not in sealed
    assert "secret" not in sealed


def test_empty_stays_empty():
    """空串语义是「清除 Key」，不能被加密成一个看起来配过的值。"""
    assert seal("") == ""
    assert unseal("") == ""


def test_legacy_plaintext_passes_through():
    """库里存量的明文（以及全局 settings 里的 Key）必须原样可用，
    否则升级后所有老用户的配置会突然『解不开』。"""
    assert unseal("sk-legacy-plaintext") == "sk-legacy-plaintext"
    assert scheme_of("sk-legacy-plaintext") == "plaintext"


def test_sealing_twice_does_not_double_wrap():
    once = seal("sk-123")
    assert seal(once) == once
    assert unseal(seal(once)) == "sk-123"


def test_unseal_is_total():
    """畸形密文只该原样返回，绝不该抛 —— 抛出去就是 500。"""
    for bad in ("enc:v1:", "enc:v1:dpapi:", "enc:v1:file:!!!not-base64!!!",
                "enc:v1:nosuch:AAAA", "enc:v1:file:QUJD"):
        assert isinstance(unseal(bad), str)


def test_file_fallback_rejects_tampering():
    """回退方案带 HMAC 认证标签：改一个字节就解不开（返回原文而非垃圾）。"""
    blob = _file_seal(b"sk-123")
    assert _file_unseal(blob) == b"sk-123"
    broken = bytearray(blob)
    broken[-1] ^= 0x01
    assert _file_unseal(bytes(broken)) is None
    assert _file_unseal(blob[:20]) is None


def test_active_scheme_is_one_of_the_two():
    assert active_scheme() in ("dpapi", "file")


def test_sealed_scheme_is_reported():
    assert scheme_of(seal("sk-123")) in ("dpapi", "file")


def test_llm_client_resolves_the_unsealed_key():
    """`LLMClient.resolve` 是唯一读用户 Key 的地方，必须解封。"""
    from app.llm.client import LLMClient
    cfg = {"apiKey": seal("sk-user-level"), "baseUrl": "https://x/v1", "model": "m"}
    assert LLMClient.resolve(cfg).api_key == "sk-user-level"
    # 没有用户配置时回落到全局设置
    assert LLMClient.resolve({}).api_key == ""
