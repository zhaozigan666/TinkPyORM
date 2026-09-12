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

from tinkpyorm import Db, DriverNotAvailable, InvalidArgumentException
from tinkpyorm.config import Config, available_type_names, normalize_type
from tinkpyorm.drivers import (
    Driver, NoSQLDriver, SQLDriver, SQLiteDriver, UnsupportedOperation,
    available_drivers, get_driver, register_driver,
)
from tinkpyorm.drivers import _DRIVERS

TMP_DIR = os.path.join(tempfile.gettempdir(), "tinkpyorm_drv_test")


def _mem_config() -> Config:
    """内存库 sqlite 配置（驱动单测用）。"""
    return Config(database=":memory:")


class CustomSQLDriver(SQLiteDriver):
    """第三方驱动样例：复用 SQLite 执行，仅覆写标识符引用风格（PG 风格）。"""

    name = "custom_sql"

    def _wrap_identifier(self, name: str) -> str:
        return f'"{name}"'


def _register_custom_sql_driver() -> None:
    """幂等注册第三方驱动样例。"""
    _DRIVERS.setdefault("custom_sql", CustomSQLDriver)


def _drop_driver(name: str) -> None:
    """从注册表移除测试驱动，避免跨用例污染全局注册表。"""
    _DRIVERS.pop(name, None)


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

        self.addCleanup(_drop_driver, "mock")
        cls = get_driver("mock")
        self.assertIs(cls, MockDriver)
        # v0.7.1：注册名可通过 Config 校验（此前需 object.__new__ 绕过白名单）
        cfg = Config(type="mock", database=":memory:")
        self.assertEqual(cfg.type, "mock")
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

        self.addCleanup(_drop_driver, "test_nosql")
        cfg = Config(type="test_nosql", database=":memory:")
        d = FakeNoSQL(cfg)
        with self.assertRaises(UnsupportedOperation):
            d.select("SELECT 1")
        with self.assertRaises(UnsupportedOperation):
            d.begin()


class TestCustomDriverConfig(unittest.TestCase):
    """v0.7.1：``type`` 校验必须对照**活驱动注册表**，而非静态白名单。

    修复前 :func:`normalize_type` 只认 ``KNOWN_TYPES``，导致第三方驱动
    ``register_driver("oracle", ...)`` 之后 ``{"type": "oracle"}`` 仍被拒，
    "新增数据库只需实现驱动并注册" 的扩展承诺在配置层即断裂。
    """

    def setUp(self):
        os.makedirs(TMP_DIR, exist_ok=True)
        self.addCleanup(_drop_driver, "custom_sql")
        # 复位全局连接，避免自定义驱动泄漏到后续测试模块。
        # 用 set_config 显式重建默认连接（Db.close() 亦可用：驱动把底层连接
        # 置空后由 connect() 惰性重连，故关闭后的默认连接不会失效）。
        self.addCleanup(Db.set_config, {"database": ":memory:"})

    # ---------------------------------------------------------------- #
    # 接受
    # ---------------------------------------------------------------- #
    def test_registered_driver_accepted_by_config(self):
        """Config 直接构造 / from_dict 均接受已注册的驱动名。"""
        _register_custom_sql_driver()

        self.assertEqual(Config(type="custom_sql", database=":memory:").type,
                         "custom_sql")
        self.assertEqual(
            Config.from_dict({"type": "custom_sql", "database": ":memory:"}).type,
            "custom_sql")

    def test_available_type_names_includes_registered(self):
        _register_custom_sql_driver()
        self.assertIn("custom_sql", available_type_names())

    def test_set_config_end_to_end(self):
        """Db.set_config 指定自定义驱动后，链式查询真实可用（上层零改动）。"""
        _register_custom_sql_driver()
        path = os.path.join(TMP_DIR, "custom.db")
        conn = Db.set_config({"type": "custom_sql", "database": path})
        self.assertEqual(type(conn.driver).__name__, "CustomSQLDriver")

        Db.execute("DROP TABLE IF EXISTS custom_probe")
        Db.execute("CREATE TABLE custom_probe (id INTEGER PRIMARY KEY, v TEXT)")
        Db.table("custom_probe").insert({"id": 1, "v": "ok"})
        self.assertEqual(Db.table("custom_probe").where("id", 1).value("v"), "ok")
        self.assertEqual(conn.driver._wrap_identifier("x"), '"x"')

    def test_alias_of_registered_driver_still_resolves(self):
        """别名解析不受影响（内置别名表优先命中）。"""
        self.assertEqual(normalize_type("mariadb"), "mysql")
        self.assertEqual(normalize_type("POSTGRES"), "postgresql")

    # ---------------------------------------------------------------- #
    # 拒绝 / 边界
    # ---------------------------------------------------------------- #
    def test_unregistered_type_still_rejected(self):
        """未注册的类型仍拒绝，且错误信息给出已注册清单与注册指引。"""
        with self.assertRaises(InvalidArgumentException) as ctx:
            normalize_type("nosuchdb")
        msg = str(ctx.exception)
        self.assertIn("nosuchdb", msg)
        self.assertIn("register_driver", msg)
        self.assertIn("sqlite", msg)          # 列出活注册表而非静态白名单

    def test_reserved_type_passes_config_but_fails_at_connect(self):
        """预留名（mysql 等）配置阶段放行，连接阶段才抛 DriverNotAvailable。"""
        cfg = Config(type="mysql", database="app")
        self.assertEqual(cfg.type, "mysql")
        with self.assertRaises(DriverNotAvailable):
            get_driver("mysql")

    def test_registry_lookup_is_live_not_cached(self):
        """注册表查询不缓存：注册动作之后校验立即通过。"""
        self.assertNotIn("custom_sql", available_type_names())
        with self.assertRaises(InvalidArgumentException):
            normalize_type("custom_sql")
        _register_custom_sql_driver()
        self.assertEqual(normalize_type("custom_sql"), "custom_sql")


if __name__ == "__main__":
    unittest.main(verbosity=2)
