# -*- coding: utf-8 -*-
"""配置层与连接注册中心测试（Phase 1：集中式配置）。

覆盖目标：
* Config 四种来源（dict / DSN / env / file）与非法配置显式报错；
* DatabaseManager 懒连接、同名重复注册、多连接隔离、生命周期；
* 旧写法 ``Db.set_config`` 的回归护栏。
"""
import dataclasses
import json
import os
import shutil
import sys
import tempfile
import unittest
import warnings

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from tinkpyorm import (
    Config, ConfigError, ConnectionNotFound, Db, DriverNotAvailable,
    configure, connection, load_file, manager, parse_dsn,
)
from tinkpyorm.drivers import (
    Driver, SQLDriver, NoSQLDriver, UnsupportedOperation,
    available_drivers, get_driver, register_driver,
)
from tinkpyorm.connection import Connection

TMP_DIR = os.path.join(tempfile.gettempdir(), "tinkpyorm_cfg_test")


def _reset():
    """清空全局注册中心，保证用例互不干扰。"""
    manager.clear()
    manager.strict = False
    Db._default_name = "default"
    Db._prefix = ""


class TestConfig(unittest.TestCase):
    def setUp(self):
        _reset()

    def tearDown(self):
        _reset()

    def test_from_dict_basic(self):
        cfg = Config.from_dict({"database": "app.db", "prefix": "app_",
                                "journal_mode": "WAL", "timeout": 5}, name="main")
        self.assertEqual(cfg.name, "main")
        self.assertEqual(cfg.type, "sqlite")
        self.assertEqual(cfg.database, "app.db")
        self.assertEqual(cfg.prefix, "app_")
        # 驱动专属键归入 options，不再透传给 sqlite3.connect
        self.assertEqual(cfg.options["journal_mode"], "WAL")
        self.assertEqual(cfg.options["timeout"], 5)

    def test_unknown_type_raises(self):
        with self.assertRaises(ConfigError):
            Config.from_dict({"type": "oracle", "database": "x"})

    def test_type_alias_normalized(self):
        self.assertEqual(Config.from_dict({"type": "pg"}).type, "postgresql")
        self.assertEqual(Config.from_dict({"type": "sqlite3"}).type, "sqlite")

    def test_explicit_options_merged(self):
        cfg = Config.from_dict({"database": "a.db",
                                "options": {"charset": "utf8mb4"}})
        self.assertEqual(cfg.options["charset"], "utf8mb4")

    def test_path_expand(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["TINKPYORM_TEST_HOME"] = tmp
            cfg = Config.from_dict({"database": "${TINKPYORM_TEST_HOME}/v.db"})
            self.assertTrue(cfg.database.startswith(tmp))
            os.environ.pop("TINKPYORM_TEST_HOME")

    def test_memory_not_expanded(self):
        self.assertEqual(Config.from_dict({"database": ":memory:"}).database,
                         ":memory:")

    def test_dsn_sqlite(self):
        self.assertEqual(parse_dsn("sqlite:///:memory:")["database"], ":memory:")
        self.assertEqual(parse_dsn("sqlite:///./app.db")["database"], "./app.db")
        self.assertEqual(parse_dsn("sqlite:////abs/app.db")["database"],
                         "/abs/app.db")
        self.assertEqual(parse_dsn("sqlite://rel/app.db")["database"],
                         "rel/app.db")
        self.assertEqual(parse_dsn("app.db")["type"], "sqlite")

    def test_dsn_sqlite_has_no_host(self):
        # sqlite://rel/app.db 的 netloc 是路径首段，不应被当成主机
        parsed = parse_dsn("sqlite://rel/app.db")
        self.assertNotIn("host", parsed)

    def test_dsn_network(self):
        parsed = parse_dsn("mysql://root:pwd@127.0.0.1:3306/app?charset=utf8mb4")
        self.assertEqual(parsed["type"], "mysql")
        self.assertEqual(parsed["host"], "127.0.0.1")
        self.assertEqual(parsed["port"], 3306)
        self.assertEqual(parsed["user"], "root")
        self.assertEqual(parsed["password"], "pwd")
        self.assertEqual(parsed["database"], "app")
        self.assertEqual(parsed["charset"], "utf8mb4")

    def test_dsn_roundtrip(self):
        for dsn in ("sqlite:////abs/app.db", "sqlite:///rel/app.db",
                    "sqlite:///:memory:", "mysql://u:p@h:3306/db"):
            self.assertEqual(Config.from_dsn(dsn).dsn(hide_password=False), dsn)

    def test_from_env(self):
        os.environ["TINKPYORM_DEFAULT_DATABASE"] = "/tmp/env.db"
        os.environ["TINKPYORM_DEFAULT_OPTIONS_JOURNAL_MODE"] = "WAL"
        os.environ["TINKPYORM_LOG_DATABASE"] = "/tmp/log.db"
        try:
            cfg = Config.from_env()
            self.assertEqual(cfg.database, "/tmp/env.db")
            self.assertEqual(cfg.options["journal_mode"], "WAL")
            log_cfg = Config.from_env(name="log")
            self.assertEqual(log_cfg.database, "/tmp/log.db")
            self.assertIsNone(Config.from_env(name="missing"))
        finally:
            for key in ("TINKPYORM_DEFAULT_DATABASE",
                        "TINKPYORM_DEFAULT_OPTIONS_JOURNAL_MODE",
                        "TINKPYORM_LOG_DATABASE"):
                os.environ.pop(key, None)

    def test_load_file_json(self):
        shutil.rmtree(TMP_DIR, ignore_errors=True)
        os.makedirs(TMP_DIR, exist_ok=True)
        path = os.path.join(TMP_DIR, "database.json")
        with open(path, "w", encoding="utf-8") as fp:
            json.dump({"default": {"database": "a.db", "prefix": "a_"},
                       "log": {"dsn": "sqlite:///log.db"}}, fp)
        configs = load_file(path)
        self.assertEqual(sorted(configs), ["default", "log"])
        self.assertEqual(configs["default"].prefix, "a_")
        self.assertEqual(Config.from_file(path, name="log").database, "log.db")

    def test_load_file_ini(self):
        shutil.rmtree(TMP_DIR, ignore_errors=True)
        os.makedirs(TMP_DIR, exist_ok=True)
        path = os.path.join(TMP_DIR, "database.ini")
        with open(path, "w", encoding="utf-8") as fp:
            fp.write("[default]\ndatabase = a.db\nport = 3306\n")
        cfg = Config.from_file(path)
        self.assertEqual(cfg.database, "a.db")
        self.assertEqual(cfg.port, 3306)

    def test_load_file_unsupported(self):
        shutil.rmtree(TMP_DIR, ignore_errors=True)
        os.makedirs(TMP_DIR, exist_ok=True)
        path = os.path.join(TMP_DIR, "database.yaml")
        open(path, "w").close()
        with self.assertRaises(ConfigError):
            load_file(path)

    def test_merge_overrides_options(self):
        base = Config.from_dict({"database": "a.db",
                                 "options": {"timeout": 1, "journal_mode": "WAL"}})
        merged = Config.merge(base, {"database": "b.db", "options": {"timeout": 9}})
        self.assertEqual(merged.database, "b.db")
        self.assertEqual(merged.options["timeout"], 9)
        self.assertEqual(merged.options["journal_mode"], "WAL")


class TestManager(unittest.TestCase):
    def setUp(self):
        _reset()
        shutil.rmtree(TMP_DIR, ignore_errors=True)
        os.makedirs(TMP_DIR, exist_ok=True)

    def tearDown(self):
        _reset()
        shutil.rmtree(TMP_DIR, ignore_errors=True)

    def test_lazy_connection(self):
        """注册配置不应立即建立连接（文件不应被创建）。"""
        db_path = os.path.join(TMP_DIR, "lazy.db")
        manager.add("default", {"database": db_path})
        self.assertFalse(os.path.exists(db_path), "注册配置不应创建数据库文件")
        self.assertEqual(manager.established(), [])
        manager.connection().execute("CREATE TABLE t (id INTEGER)")
        self.assertTrue(os.path.exists(db_path))
        self.assertEqual(manager.established(), ["default"])

    def test_connection_cached(self):
        manager.add("default", {"database": ":memory:"})
        self.assertIs(manager.connection(), manager.connection())

    def test_reregister_closes_old_connection(self):
        path_a = os.path.join(TMP_DIR, "a.db")
        path_b = os.path.join(TMP_DIR, "b.db")
        manager.add("default", {"database": path_a})
        old = manager.connection()
        old.execute("CREATE TABLE t (id INTEGER)")
        manager.add("default", {"database": path_b})
        new = manager.connection()
        self.assertIsNot(old, new)
        new.execute("CREATE TABLE t (id INTEGER)")
        # 旧连接的底层句柄应已释放（不再持有文件锁）
        self.assertFalse(old.connected, "重复注册同名连接时应关闭旧连接")
        self.assertTrue(new.connected)

    def test_named_connections_isolated(self):
        main_path = os.path.join(TMP_DIR, "main.db")
        log_path = os.path.join(TMP_DIR, "log.db")
        manager.add("default", {"database": main_path})
        manager.add("log", {"database": log_path})
        manager.connection("default").execute("CREATE TABLE t (id INTEGER)")
        self.assertTrue(os.path.exists(main_path))
        self.assertFalse(os.path.exists(log_path))
        self.assertIsNot(manager.connection("default"), manager.connection("log"))

    def test_prefix_isolated_per_connection(self):
        manager.add("default", {"database": ":memory:", "prefix": "app_"})
        manager.add("log", {"database": ":memory:", "prefix": "log_"})
        self.assertEqual(Db._prefix_for("default"), "app_")
        self.assertEqual(Db._prefix_for("log"), "log_")

    def test_unregistered_named_connection_raises(self):
        with self.assertRaises(ConnectionNotFound):
            manager.connection("nope")
        # 兼容旧版捕获 KeyError 的代码
        with self.assertRaises(KeyError):
            manager.connection("nope")

    def test_strict_mode_raises_for_default(self):
        manager.strict = True
        with self.assertRaises(ConfigError):
            manager.connection()

    def test_default_fallback_warns(self):
        manager.strict = False
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            conn = manager.connection()
            self.assertEqual(conn.database, ":memory:")
            self.assertTrue(any(issubclass(w.category, DeprecationWarning)
                                for w in caught))

    def test_disconnect_keeps_config(self):
        path = os.path.join(TMP_DIR, "d.db")
        manager.add("default", {"database": path})
        manager.connection().execute("CREATE TABLE t (id INTEGER)")
        manager.disconnect()
        self.assertEqual(manager.established(), [])
        self.assertTrue(manager.has("default"))
        manager.connection().execute("CREATE TABLE t2 (id INTEGER)")

    def test_reconnect(self):
        manager.add("default", {"database": ":memory:"})
        first = manager.connection()
        second = manager.reconnect()
        self.assertIsNot(first, second)

    def test_clear(self):
        manager.add("default", {"database": ":memory:"})
        manager.connection()
        manager.clear()
        self.assertEqual(manager.names(), [])
        self.assertEqual(manager.established(), [])

    def test_driver_not_available(self):
        """非 SQLite 驱动在 v0.3.0 尚未提供，应显式报错而非静默降级。"""
        manager.add("mysql", "mysql://root:pwd@127.0.0.1:3306/app")
        with self.assertRaises(DriverNotAvailable):
            manager.connection("mysql")


class TestDbFacade(unittest.TestCase):
    def setUp(self):
        _reset()
        shutil.rmtree(TMP_DIR, ignore_errors=True)
        os.makedirs(TMP_DIR, exist_ok=True)

    def tearDown(self):
        _reset()
        shutil.rmtree(TMP_DIR, ignore_errors=True)

    def test_configure_once_shared_everywhere(self):
        """核心诉求：配置一次，任意模块共享，无需重复 set_config。"""
        path = os.path.join(TMP_DIR, "shared.db")
        configure({"default": {"database": path, "prefix": "app_"},
                   "log": {"database": os.path.join(TMP_DIR, "log.db")}})
        self.assertTrue(Db.configured())
        self.assertTrue(Db.configured("log"))
        # 任意位置取用，行为一致
        Db.get_connection().execute("CREATE TABLE app_user (id INTEGER, name TEXT)")
        Db.table("app_user").insert({"id": 1, "name": "amy"})
        row = connection().query("SELECT name FROM app_user WHERE id = 1")[0]
        self.assertEqual(row["name"], "amy")
        self.assertIs(Db.get_connection(), connection())

    def test_query_constructed_before_configure(self):
        """先构造 Query、后 configure 仍可用（消除导入顺序依赖）。"""
        path = os.path.join(TMP_DIR, "late.db")
        query = Db.table("late_user")          # 此刻尚未配置任何连接
        self.assertFalse(Db.configured())
        configure({"default": {"database": path}})
        Db.execute("CREATE TABLE late_user (id INTEGER, name TEXT)")
        query.insert({"id": 1, "name": "dave"})
        self.assertEqual(Db.table("late_user").find()["name"], "dave")

    def test_prefix_resolved_after_configure(self):
        """前缀同样延迟解析：configure 之后再取用也能生效。"""
        path = os.path.join(TMP_DIR, "prefix.db")
        configure({"default": {"database": path, "prefix": "px_"}})
        Db.execute("CREATE TABLE px_user (id INTEGER, name TEXT)")
        Db.name("user").insert({"id": 1, "name": "erin"})
        self.assertEqual(Db.name("user").count(), 1)

    def test_connect_proxy_named_connection(self):
        log_path = os.path.join(TMP_DIR, "log.db")
        configure({"default": {"database": os.path.join(TMP_DIR, "main.db")},
                   "log": {"database": log_path, "prefix": "lg_"}})
        Db.connect("log").execute("CREATE TABLE lg_event (id INTEGER, msg TEXT)")
        Db.connect("log").table("lg_event").insert({"id": 1, "msg": "boot"})
        rows = Db.connect("log").query("SELECT msg FROM lg_event")
        self.assertEqual(rows[0]["msg"], "boot")
        self.assertTrue(os.path.exists(log_path))

    def test_env_override_on_configure(self):
        path = os.path.join(TMP_DIR, "env.db")
        os.environ["TINKPYORM_DEFAULT_DATABASE"] = path
        try:
            configure({"default": {"database": os.path.join(TMP_DIR, "orig.db")}})
            self.assertEqual(manager.config("default").database, path)
        finally:
            os.environ.pop("TINKPYORM_DEFAULT_DATABASE", None)

    def test_configure_accepts_single_dsn(self):
        configure("sqlite:///:memory:")
        self.assertTrue(Db.configured())
        self.assertEqual(Db.query("SELECT 1 AS one")[0]["one"], 1)

    # ------------------------------------------------------------------ #
    # 旧写法回归护栏
    # ------------------------------------------------------------------ #
    def test_set_config_dict_legacy(self):
        path = os.path.join(TMP_DIR, "legacy.db")
        conn = Db.set_config({"database": path, "prefix": "lv_", "timeout": 3.0})
        self.assertIsInstance(conn, Connection)
        self.assertIs(conn, Db.get_connection())
        conn.execute("CREATE TABLE lv_user (id INTEGER, name TEXT)")
        Db.table("lv_user").insert({"id": 1, "name": "bob"})
        self.assertEqual(Db.table("lv_user").count(), 1)

    def test_set_config_str_legacy(self):
        path = os.path.join(TMP_DIR, "legacy2.db")
        conn = Db.set_config(path)
        self.assertEqual(conn.database, path)

    def test_set_config_connection_legacy(self):
        conn = Connection(os.path.join(TMP_DIR, "legacy3.db"))
        Db.set_config(conn)
        self.assertIs(Db.get_connection(), conn)

    def test_set_config_bad_type(self):
        with self.assertRaises(TypeError):
            Db.set_config(123)

    def test_set_config_repeated_no_leak(self):
        first = Db.set_config({"database": os.path.join(TMP_DIR, "r1.db")})
        second = Db.set_config({"database": os.path.join(TMP_DIR, "r2.db")})
        self.assertIsNot(first, second)
        self.assertIs(Db.get_connection(), second)

    def test_model_uses_default_connection_after_configure(self):
        path = os.path.join(TMP_DIR, "model.db")
        configure({"default": {"database": path}})
        Db.execute("CREATE TABLE cfg_user (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT)")

        from tinkpyorm import Model

        class CfgUser(Model):
            __table__ = "cfg_user"

        CfgUser.create({"name": "carol"})
        self.assertEqual(CfgUser.where("name", "carol").find()["name"], "carol")


class TestDriverAbstraction(unittest.TestCase):
    """驱动抽象层测试（Phase 2：driver 抽象与 dialect 下沉）。"""

    def setUp(self):
        # 每个用例隔离驱动表
        self._driver_purge = list(available_drivers())
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
        from tinkpyorm.drivers import get_driver
        d = get_driver("sqlite")(Config.from_dsn("sqlite:///:memory:"))
        self.assertEqual(d.quote_identifier("id"), "`id`")
        self.assertEqual(d.quote_identifier("user.id"), "`user`.`id`")
        self.assertEqual(d.quote_identifier("*"), "*")
        self.assertEqual(d.quote_identifier("COUNT(*)"), "COUNT(*)")
        self.assertEqual(d.quote_identifier("name AS n"), "name AS n")

    def test_placeholder_generation(self):
        d = get_driver("sqlite")(Config.from_dsn("sqlite:///:memory:"))
        self.assertEqual(d.placeholder(1), "?")
        self.assertEqual(d.placeholder(3), "?, ?, ?")
        self.assertEqual(d.placeholder(0), "")

    def test_limit_sql(self):
        d = get_driver("sqlite")(Config.from_dsn("sqlite:///:memory:"))
        self.assertEqual(d.limit_sql(10), "LIMIT 10")
        self.assertEqual(d.limit_sql(10, 20), "LIMIT 10 OFFSET 20")

    def test_register_custom_driver(self):
        """@register_driver 装饰器可用，新增驱动无需改 Connection/Builder。"""
        from tinkpyorm.drivers import register_driver, get_driver

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
                if offset: sql += f" OFFSET {int(offset)}"
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
        d = cls(cfg)
        # 上层 Builder 的方言切换在 mock 驱动下立即生效
        self.assertEqual(d._wrap_identifier("x"), '"x"')
        self.assertEqual(d.placeholder(2), "%s,%s")

    def test_builder_uses_driver_placeholder(self):
        """Builder 必须通过 driver.placeholder 而非硬编码 '?'。"""
        from tinkpyorm.builder import Builder
        from tinkpyorm.query import Query

        path = os.path.join(TMP_DIR, "drv.db")
        Db.set_config({"database": path})
        # 抓取一次 Builder 生成的 SQL，看占位符就是 driver.placeholder 的产物
        d = Db.get_connection().driver
        original = d.placeholder
        d.placeholder = lambda n: "::PG" + str(n) + "::" * 0  # 仅作为标识
        d.placeholder = lambda n: ", ".join([f"PG{i}" for i in range(n)])
        try:
            q = Query(Db.get_connection()).table("drv_test")
            sql, params = q.where("a", "in", [1, 2, 3])._execute_select()
            self.assertIn("PG0, PG1, PG2", sql, "Builder 必须走 driver.placeholder")
        finally:
            d.placeholder = original

    def test_nosql_driver_blocks_sql(self):
        from tinkpyorm.drivers import NoSQLDriver

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
        d = FakeNoSQL(cfg)
        with self.assertRaises(UnsupportedOperation):
            d.select("SELECT 1")
        with self.assertRaises(UnsupportedOperation):
            d.begin()


if __name__ == "__main__":
    unittest.main(verbosity=2)
