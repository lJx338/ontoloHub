"""secrets 工具单元测试（HIA-71 — connector secret_fields 加解密）。"""
from __future__ import annotations

import os

import pytest

from src.core.secrets import (
    decrypt_secret_fields,
    decrypt_value,
    encrypt_secret_fields,
    encrypt_value,
    mask_secret_fields,
)


class TestEncryptDecryptValue:
    def test_roundtrip(self):
        plain = "hello world"
        cipher = encrypt_value(plain)
        assert cipher.startswith("enc:v1:")
        assert plain not in cipher  # 不应直接出现明文
        assert decrypt_value(cipher) == plain

    def test_idempotent_encrypt(self):
        # 已加密值不应被再次加密
        plain = "secret"
        cipher = encrypt_value(plain)
        cipher2 = encrypt_value(cipher)
        assert cipher == cipher2

    def test_plaintext_passthrough(self):
        # 未带前缀视为明文
        assert decrypt_value("plain") == "plain"

    def test_none_passthrough(self):
        assert encrypt_value(None) is None
        assert decrypt_value(None) is None


class TestEncryptSecretFields:
    def test_only_listed_fields_are_encrypted(self):
        cfg = {"host": "pg.example.com", "password": "s3cret", "user": "bob"}
        out = encrypt_secret_fields(cfg, ["password"])
        assert out["host"] == "pg.example.com"
        assert out["user"] == "bob"
        assert out["password"].startswith("enc:v1:")

    def test_missing_field_is_ignored(self):
        cfg = {"host": "x"}
        out = encrypt_secret_fields(cfg, ["password"])
        assert out == {"host": "x"}

    def test_empty_fields(self):
        cfg = {"a": 1, "b": 2}
        assert encrypt_secret_fields(cfg, []) == cfg

    def test_roundtrip(self):
        cfg = {"password": "x", "token": "y"}
        enc = encrypt_secret_fields(cfg, ["password", "token"])
        dec = decrypt_secret_fields(enc, ["password", "token"])
        assert dec == cfg


class TestMaskSecretFields:
    def test_masks_only_secrets(self):
        cfg = {"host": "pg", "password": "enc:v1:abc", "user": "bob"}
        out = mask_secret_fields(cfg, ["password"])
        assert out == {"host": "pg", "password": "***", "user": "bob"}

    def test_empty(self):
        assert mask_secret_fields({}, ["x"]) == {}
        assert mask_secret_fields({"a": 1}, []) == {"a": 1}
