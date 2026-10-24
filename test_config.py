# -*- coding: utf-8 -*-
"""config.py 独立单元测试（审查报告 K2：此前仅经 mysql/mongo 驱动间接覆盖）。"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from tinkpyorm.config import Config
from tinkpyorm.exceptions import InvalidArgumentException


class TestConfigDefaults(unittest.TestCase):
    def test_sqlite_defaults(self):
        c = Config()
        self.assertEqual(c.type, "sqlite")
        self.assertEqual(c.database, ":memory:")
        self.assertEqual(c.prefix, "")
        self.assertFalse(c.json)
        self.assertEqual(c.cache_backend, "memory")
        self.assertIsNone(c.cache_dir)


class TestConfigNormalize(unittest.TestCase):
    def test_port_coerced_to_int(self):
        self.assertEqual(Config(port="3306").port, 3306)

    def test_bool_coercion(self):
        self.assertTrue(Config(json=1).json)
        self.assertFalse(Config(json=0).json)

    def test_invalid_cache_backend_rejected(self):
        with self.assertRaises(InvalidArgumentException):
            Config(cache_backend="redis")

    def test_file_backend_requires_dir(self):
        with self.assertRaises(InvalidArgumentException):
            Config(cache_backend="file")

    def test_file_backend_ok(self):
        c = Config(cache_backend="file", cache_dir="/tmp/tkcache")
        self.assertEqual(c.cache_backend, "file")
        self.assertEqual(c.cache_dir, "/tmp/tkcache")

    def test_memory_backend_clears_dir(self):
        c = Config(cache_backend="memory", cache_dir="/tmp/x")
        self.assertIsNone(c.cache_dir)

    def test_invalid_type_rejected(self):
        with self.assertRaises(Exception):
            Config(type="oracle9")


class TestFromDict(unittest.TestCase):
    def test_username_alias(self):
        c = Config.from_dict({"username": "root", "password": "p"})
        self.assertEqual(c.user, "root")
        self.assertEqual(c.password, "p")

    def test_driver_alias(self):
        self.assertEqual(Config.from_dict({"driver": "mysql"}).type, "mysql")

    def test_dsn_bare_path(self):
        self.assertEqual(Config.from_dict({"dsn": "app.db"}).database, "app.db")

    def test_unknown_keys_go_to_options(self):
        c = Config.from_dict({"database": ":memory:", "timeout": 5})
        self.assertEqual(c.options.get("timeout"), 5)

    def test_non_mapping_rejected(self):
        with self.assertRaises(InvalidArgumentException):
            Config.from_dict("not-a-dict")


class TestToDictSecretMasking(unittest.TestCase):
    """审查报告 K1：password 默认掩码，防止日志/调试 dump 泄漏。"""

    def test_password_masked_by_default(self):
        c = Config(type="mysql", host="h", port=3306,
                   user="u", password="s3cret")
        self.assertEqual(c.to_dict()["password"], "***")

    def test_mask_disabled_roundtrip(self):
        c = Config(type="mysql", host="h", user="u", password="s3cret")
        plain = c.to_dict(mask_secrets=False)
        self.assertEqual(plain["password"], "s3cret")
        rebuilt = Config.from_dict(plain)
        self.assertEqual(rebuilt.password, "s3cret")
        self.assertEqual(rebuilt.user, "u")

    def test_no_password_key_when_none(self):
        self.assertNotIn("password", Config(host="h").to_dict())

    def test_other_fields_unaffected(self):
        c = Config(type="mysql", host="h", user="u", password="x")
        d = c.to_dict()
        self.assertEqual(d["user"], "u")
        self.assertEqual(d["host"], "h")
        self.assertEqual(d["type"], "mysql")

    def test_repr_has_no_password(self):
        c = Config(type="mysql", database="db", password="s3cret")
        self.assertNotIn("s3cret", repr(c))


if __name__ == "__main__":
    unittest.main()
