# -*- coding: utf-8 -*-
"""驱动抽象层测试（v0.3.0 Phase 2：driver 抽象与 dialect 下沉）。

v0.3.1 撤销集中式配置后，原 test_config_manager.py 中属 Phase 2 的
驱动层用例迁移至此（配置层用例已随机制移除）。
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from tinkpyorm import Db, DriverNotAvailable
from tinkpyorm.config import Config
from tinkpyorm.drivers import (
    Driver, NoSQLDriver, SQLDriver, UnsupportedOperation,
    available_drivers, get_driver, register_driver,
)

TMP_DIR = os.path.join(tempfile.gettempdir(), "tinkpyorm_drv_test")


def _mem_config() -> Config:
    """内存库 sqlite 配置（驱动单测用）。"""
    return Config(database=":memory:")


class TestDriverAbstraction(unittest.TestCase):
    """驱动抽象层测试：注册表、方言钩子、Builder 下沉。"""

    def setUp(self):
        os.makedirs(TMP_DIR, exist_ok=True)

    def test_default_drivers_listed(self):
        self.assertIn("sqlite", available_drivers())

    def test_get_driver_returns_class(self):
        cls = get_driver("sqlite")
        self.assertTrue(issubclass(cls, Driver))

    def test_unknown_driver_raises(self):
        with self.assertRaises(DriverNotAvailable):
            get_driver("oracle")

    def test_sqlite_driver_quote_identifier_smart(self):
        """Base.quote_identifier 必须区分纯标识符与表达式。"""
        d = get_driver("sqlite")(_mem_config())
        self.assertEqual(d.quote_identifier("id"), "`id`")
        self.assertEqual(d.quote_identifier("user.id"), "`user`.`id`")
        self.assertEqual(d.quote_identifier("*"), "*")
        self.assertEqual(d.quote_identifier("COUNT(*)"), "COUNT(*)")
        self.assertEqual(d.quote_identifier("name AS n"), "name AS n")

    def test_placeholder_generation(self):
        d = get_driver("sqlite")(_mem_config())
        self.assertEqual(d.placeholder(1), "?")
        self.assertEqual(d.placeholder(3), "?, ?, ?")
        self.assertEqual(d.placeholder(0), "")

    def test_limit_sql(self):
        d = get_driver("sqlite")(_mem_config())
        self.assertEqual(d.limit_sql(10), "LIMIT 10")
        self.assertEqual(d.limit_sql(10, 20), "LIMIT 10 OFFSET 20")

    def test_register_custom_driver(self):
        """@register_driver 装饰器可用，新增驱动无需改 Connection/Builder。"""

        @register_driver("mock")
        class MockDriver(SQLDriver):
            name = "mock"
            def connect(self): return None
            def close(self): pass
            def select(self, sql, params=()): return []
            def execute(self, sql, params=()): return 0
            def insert(self, sql, params=()): return 0
            def _wrap_identifier(self, name): return f'"{name}"'   # PG 风格
            def placeholder(self, count=1): return ",".join(["%s"] * count)
            def limit_sql(self, limit, offset=None):
                sql = f"LIMIT {int(limit)}"
                if offset:
                    sql += f" OFFSET {int(offset)}"
                return sql

        cls = get_driver("mock")
        self.assertIs(cls, MockDriver)
        # 构造一个 mock-typed config（避开 KNOWN_TYPES 校验：直接 __new__）
        cfg = object.__new__(Config)
        cfg.type = "mock"
        cfg.database = ":memory:"
        cfg.options = {}
        cfg.host = cfg.port = cfg.user = cfg.password = None
        cfg.prefix = ""
        cfg.connect_timeout = None
        cfg.sql_log_enabled = True
        cfg.sql_log_max = 1000
        cfg.path_expand = False
        d = cls(cfg)
        # 上层 Builder 的方言切换在 mock 驱动下立即生效
        self.assertEqual(d._wrap_identifier("x"), '"x"')
        self.assertEqual(d.placeholder(2), "%s,%s")

    def test_builder_uses_driver_placeholder(self):
        """Builder 必须通过 driver.placeholder 而非硬编码 '?'。"""
        from tinkpyorm.query import Query

        path = os.path.join(TMP_DIR, "drv.db")
        Db.set_config({"database": path})
        # 抓取一次 Builder 生成的 SQL，看占位符就是 driver.placeholder 的产物
        d = Db.get_connection().driver
        original = d.placeholder
        d.placeholder = lambda n: ", ".join([f"PG{i}" for i in range(n)])
        try:
            q = Query(Db.get_connection()).table("drv_test")
            sql, params = q.where("a", "in", [1, 2, 3])._execute_select()
            self.assertIn("PG0, PG1, PG2", sql, "Builder 必须走 driver.placeholder")
        finally:
            d.placeholder = original

    def test_nosql_driver_blocks_sql(self):
        @register_driver("test_nosql")
        class FakeNoSQL(NoSQLDriver):
            name = "test_nosql"
            def connect(self): return object()
            def close(self): pass

        cfg = object.__new__(Config)
        cfg.type = "test_nosql"
        cfg.database = ":memory:"
        cfg.options = {}
        cfg.host = cfg.port = cfg.user = cfg.password = None
        cfg.prefix = ""
        cfg.connect_timeout = None
        cfg.sql_log_enabled = True
        cfg.sql_log_max = 1000
        cfg.path_expand = False
        d = FakeNoSQL(cfg)
        with self.assertRaises(UnsupportedOperation):
            d.select("SELECT 1")
        with self.assertRaises(UnsupportedOperation):
            d.begin()


if __name__ == "__main__":
    unittest.main(verbosity=2)
