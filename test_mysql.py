# -*- coding: utf-8 -*-
"""MySQL 版本模型与方言测试（v0.11.0）。

均为**离线**测试：只验证版本解析、能力矩阵与 SQL 文本，不建立真实连接
（服务器集成测试需外部实例，见 wiki/29-MySQL支持）。
"""
import unittest

from tinkpyorm import Db, UnsupportedOperation
from tinkpyorm.config import Config
from tinkpyorm.drivers.mysql import MYSQL_FEATURES
from tinkpyorm.exceptions import InvalidArgumentException
from tinkpyorm.versioning import Version, parse_version


def _mysql(version=None, **kwargs):
    """切换到 MySQL 配置；返回 Connection（不建立网络连接）。"""
    Db.close()
    config = {"type": "mysql", "database": "app", "user": "root"}
    if version is not None:
        config["version"] = version
    config.update(kwargs)
    return Db.config(config)


class TestVersionParsing(unittest.TestCase):
    """版本号解析规则与默认值。"""

    def test_default_is_5_7(self):
        self.assertEqual(parse_version(None), Version(5, 7, 0))
        self.assertEqual(parse_version(""), Version(5, 7, 0))

    def test_string_forms(self):
        self.assertEqual(parse_version("5.7"), Version(5, 7, 0))
        self.assertEqual(parse_version("5.7.44"), Version(5, 7, 44))
        self.assertEqual(parse_version("8.0.36"), Version(8, 0, 36))
        self.assertEqual(parse_version("9"), Version(9, 0, 0))

    def test_int_float_tuple_forms(self):
        self.assertEqual(parse_version(8), Version(8, 0, 0))
        self.assertEqual(parse_version(5.7), Version(5, 7, 0))
        self.assertEqual(parse_version(8.0), Version(8, 0, 0))
        self.assertEqual(parse_version((8, 0, 36)), Version(8, 0, 36))
        self.assertEqual(parse_version([9, 1]), Version(9, 1, 0))

    def test_distro_suffix_is_ignored(self):
        self.assertEqual(parse_version("8.0.36-0ubuntu0.22.04.1"),
                         Version(8, 0, 36))
        self.assertEqual(parse_version("5.7.44-log"), Version(5, 7, 44))

    def test_supported_ranges(self):
        for value in ("5.7.0", "5.7.44", "8.0.0", "8.4.2", "9.0.0", "9.9.9"):
            self.assertEqual(str(parse_version(value)), str(parse_version(value)))

    def test_unsupported_versions_rejected(self):
        for value in ("5.6.51", "5.5.62", "6.0", "7.0", "8.5", "10.0", "abc",
                      "8.0.36.1", "1"):
            with self.assertRaises(InvalidArgumentException, msg=value):
                parse_version(value)

    def test_bool_rejected(self):
        with self.assertRaises(InvalidArgumentException):
            parse_version(True)

    def test_version_comparison(self):
        self.assertTrue(Version(8, 0, 1) > Version(5, 7, 44))
        self.assertTrue(Version(8, 0, 19).at_least("8.0.19"))
        self.assertFalse(Version(8, 0, 18).at_least("8.0.19"))
        self.assertEqual(str(Version(9, 1)), "9.1.0")


class TestConfigEntry(unittest.TestCase):
    """Db.config 的两种传参方式。"""

    def tearDown(self):
        Db.close()

    def test_dict_form(self):
        conn = Db.config({"type": "mysql", "version": "8.0.36",
                          "database": "app", "host": "127.0.0.1"})
        self.assertEqual(str(conn.driver.version), "8.0.36")

    def test_kwargs_form(self):
        conn = Db.config(type="mysql", version="9.1", database="app")
        self.assertEqual(str(conn.driver.version), "9.1.0")

    def test_dict_and_kwargs_equivalent(self):
        a = Db.config({"type": "mysql", "version": "8.0", "database": "app"})
        b = Db.config(type="mysql", version="8.0", database="app")
        self.assertEqual(str(a.driver.version), str(b.driver.version))

    def test_missing_version_defaults_to_5_7(self):
        conn = Db.config(type="mysql", database="app")
        self.assertEqual(str(conn.driver.version), "5.7.0")

    def test_mixing_positional_and_kwargs_rejected(self):
        with self.assertRaises(InvalidArgumentException):
            Db.set_config({"type": "mysql"}, version="8.0")

    def test_version_reaches_config_object(self):
        cfg = Config.from_dict({"type": "mysql", "version": "8.4.2"})
        self.assertEqual(cfg.version, "8.4.2")
        self.assertEqual(str(parse_version(cfg.version)), "8.4.2")


class TestCapabilityMatrix(unittest.TestCase):
    """能力矩阵与 require_feature 的报错文案。"""

    def tearDown(self):
        Db.close()

    def _driver(self, version):
        return _mysql(version).driver

    def test_5_7_capabilities(self):
        d = self._driver("5.7.44")
        self.assertTrue(d.supports_feature("json"))
        self.assertTrue(d.supports_feature("json_merge_patch"))
        self.assertFalse(d.supports_feature("cte"))
        self.assertFalse(d.supports_feature("window_functions"))
        self.assertFalse(d.supports_feature("json_table"))
        self.assertFalse(d.supports_feature("values_alias"))

    def test_8_0_early_capabilities(self):
        d = self._driver("8.0.5")
        self.assertTrue(d.supports_feature("cte"))
        self.assertTrue(d.supports_feature("json_table"))
        self.assertFalse(d.supports_feature("functional_index"))
        self.assertFalse(d.supports_feature("check_constraint"))
        self.assertFalse(d.supports_feature("values_alias"))
        self.assertFalse(d.supports_feature("except_intersect"))

    def test_8_0_late_capabilities(self):
        d = self._driver("8.0.36")
        self.assertTrue(d.supports_feature("values_alias"))
        self.assertTrue(d.supports_feature("check_constraint"))
        self.assertTrue(d.supports_feature("except_intersect"))
        self.assertTrue(d.supports_feature("native_password_auth"))

    def test_9_x_removals(self):
        d = self._driver("9.1")
        self.assertFalse(d.supports_feature("native_password_auth"))
        self.assertTrue(d.supports_feature("values_alias"))
        self.assertTrue(d.supports_feature("except_intersect"))

    def test_unknown_feature_rejected(self):
        d = self._driver("8.0.36")
        with self.assertRaises(InvalidArgumentException):
            d.supports_feature("quantum_index")

    def test_require_feature_message_has_version(self):
        d = self._driver("5.7.44")
        with self.assertRaises(UnsupportedOperation) as ctx:
            d.require_feature("cte")
        msg = str(ctx.exception)
        self.assertIn("8.0.1", msg)
        self.assertIn("5.7.44", msg)

    def test_features_snapshot(self):
        snapshot = self._driver("8.0.36").features()
        self.assertEqual(snapshot["cte"]["min"], "8.0.1")
        self.assertTrue(snapshot["cte"]["supported"])
        self.assertFalse(snapshot["cte"]["removed_in"])
        self.assertTrue(snapshot["native_password_auth"]["supported"])

    def test_matrix_covers_documented_features(self):
        for name in ("cte", "window_functions", "json_table", "functional_index",
                     "check_constraint", "values_alias", "except_intersect",
                     "nowait", "skip_locked", "json_merge_patch"):
            self.assertIn(name, MYSQL_FEATURES)


class TestDialectSQL(unittest.TestCase):
    """MySQL 方言生成的 SQL 文本。"""

    def setUp(self):
        self.conn = _mysql("8.0.36")

    def tearDown(self):
        Db.close()

    def _sql(self, query):
        """返回**未渲染**的 SQL（保留 %s 占位符）。"""
        query.fetch_sql().select()
        return query._last_sql

    def _write_sql(self, query, method, *args, **kwargs):
        query.fetch_sql()
        getattr(query, method)(*args, **kwargs)
        return query._last_sql

    def test_select_uses_percent_placeholders(self):
        sql = self._sql(Db.table("user").where("id", 1).where("name", "x"))
        self.assertIn("`id` = %s", sql)
        self.assertIn("`name` = %s", sql)
        self.assertNotIn("?", sql)

    def test_update_uses_placeholders(self):
        sql = self._write_sql(Db.table("user").where("id", 1),
                              "update", {"name": "x"})
        self.assertIn("`name` = %s", sql)
        self.assertIn("`id` = %s", sql)

    def test_in_between_like(self):
        sql = self._sql(Db.table("u").where_in("id", [1, 2]))
        self.assertIn("IN (%s, %s)", sql)
        sql = self._sql(Db.table("u").where_between("age", [18, 30]))
        self.assertIn("BETWEEN %s AND %s", sql)
        sql = self._sql(Db.table("u").where_like("name", "a%"))
        self.assertIn("LIKE %s", sql)

    def test_identifier_quoting_backtick(self):
        sql = self._sql(Db.table("user").field("id", "name"))
        self.assertIn("`user`", sql)
        self.assertIn("`id`", sql)

    def test_limit_offset(self):
        sql = self._sql(Db.table("u").limit(10, 20))
        self.assertIn("LIMIT 10 OFFSET 20", sql)

    def test_fetch_sql_renders_values(self):
        rendered = Db.table("u").where("id", 7).fetch_sql().select()
        self.assertIn("`id` = 7", rendered)
        self.assertNotIn("%s", rendered)

    def test_json_extract_and_contains(self):
        sql = self._sql(Db.table("u").where_json("extra", "$.age", ">", 18))
        self.assertIn("JSON_EXTRACT(`extra`, '$.age')", sql)
        sql = self._sql(Db.table("u").where_json_contains("extra", "$.tags", "vip"))
        self.assertIn("JSON_CONTAINS(`extra`, CAST(%s AS JSON), '$.tags')", sql)

    def test_json_type_normalized_to_lowercase(self):
        sql = self._sql(Db.table("u").where_json_type("extra", "$.tags", "array"))
        self.assertIn("JSON_TYPE", sql)
        self.assertIn("'string' THEN 'text'", sql)
        self.assertIn("'boolean' THEN IF(", sql)

    def test_json_set_uses_cast(self):
        sql = self._write_sql(Db.table("u").where("id", 1),
                              "update_json", "extra", "$.age", 19)
        self.assertIn("JSON_SET(`extra`, '$.age', %s)", sql)

    def test_json_patch_uses_merge_patch(self):
        sql = self._write_sql(Db.table("u").where("id", 1),
                              "update_json_patch", "extra", {"a": 1})
        self.assertIn("JSON_MERGE_PATCH(`extra`, CAST(%s AS JSON))", sql)

    def test_sqlite_still_uses_qmark(self):
        Db.close()
        Db.set_config({"database": ":memory:"})
        conn = Db.get_connection()
        sql = Db.table("t").where("id", 1).fetch_sql().select()
        self.assertIn("`id` = 1", sql)   # rendered；未渲染形态为 ?
        self.assertEqual(conn.driver.placeholder(2), "?, ?")


class TestVersionGating(unittest.TestCase):
    """版本门禁：该拦的拦住，该降级的降级。"""

    def tearDown(self):
        Db.close()

    def test_cte_gated_on_5_7(self):
        _mysql("5.7.44")
        with self.assertRaises(UnsupportedOperation) as ctx:
            Db.table("u").cte("paid", Db.table("o").field("uid"))
        self.assertIn("8.0.1", str(ctx.exception))

    def test_cte_sql_on_8_0(self):
        _mysql("8.0.36")
        sql = (Db.table("u")
               .cte("paid", Db.table("o").field("uid").where("status", 1))
               .where("id", 1).fetch_sql().select())
        self.assertTrue(sql.startswith("WITH `paid` AS ("))
        self.assertIn("`status` = 1", sql)

    def test_cte_columns_and_recursive(self):
        _mysql("8.0.36")
        sql = (Db.table("u").cte("seq", "SELECT 1 AS n", columns=["n"],
                                 recursive=True)
               .fetch_sql().select())
        self.assertIn("WITH RECURSIVE `seq` (`n`) AS", sql)

    def test_skip_locked_gated_on_5_7(self):
        _mysql("5.7.44")
        with self.assertRaises(UnsupportedOperation):
            Db.table("u").lock("skip locked")

    def test_skip_locked_sql_on_8_0(self):
        _mysql("8.0.36")
        sql = Db.table("u").where("id", 1).lock("skip locked").fetch_sql().select()
        self.assertTrue(sql.endswith("FOR UPDATE SKIP LOCKED"))
        sql = Db.table("u").lock("nowait").fetch_sql().select()
        self.assertTrue(sql.endswith("FOR UPDATE NOWAIT"))

    def test_json_patch_gated_below_5_7_22(self):
        _mysql("5.7.21")
        with self.assertRaises(UnsupportedOperation) as ctx:
            Db.table("u").where("id", 1).fetch_sql().update_json_patch(
                "extra", {"a": 1})
        self.assertIn("5.7.22", str(ctx.exception))

    def test_upsert_degrades_by_version(self):
        _mysql("5.7.44")
        sql = Db.table("u").fetch_sql().upsert({"id": 1, "name": "x"},
                                               update=["name"])
        self.assertIn("VALUES(`name`)", sql)
        self.assertNotIn(" AS new", sql)

        _mysql("8.0.36")
        sql = Db.table("u").fetch_sql().upsert({"id": 1, "name": "x"},
                                               update=["name"])
        self.assertIn(" AS new ON DUPLICATE KEY UPDATE", sql)
        self.assertIn("= new.`name`", sql)

    def test_upsert_requires_update_targets(self):
        _mysql("8.0.36")
        with self.assertRaises(Exception):
            Db.table("u").fetch_sql().upsert({"id": 1}, update=[])

    def test_upsert_rejected_on_sqlite(self):
        Db.close()
        Db.set_config({"database": ":memory:"})
        with self.assertRaises(UnsupportedOperation):
            Db.table("t").fetch_sql().upsert({"id": 1, "name": "x"}, ["name"])


class TestCapabilityBoundaries(unittest.TestCase):
    """MySQL 不适用的能力在构造期即拒绝。"""

    def setUp(self):
        _mysql("8.0.36")

    def tearDown(self):
        Db.close()

    def test_document_collection_rejected(self):
        with self.assertRaises(UnsupportedOperation):
            Db.collection("docs")

    def test_fts_rejected(self):
        with self.assertRaises(UnsupportedOperation):
            Db.fts("docs")

    def test_driver_flags(self):
        driver = Db.get_connection().driver
        self.assertFalse(driver.supports_document)
        self.assertFalse(driver.supports_fts)
        self.assertTrue(driver.supports_upsert)
        self.assertTrue(driver.supports_json)

    def test_dialect_info(self):
        info = Db.get_connection().driver.dialect_info()
        self.assertEqual(info["driver"], "mysql")
        self.assertEqual(info["version"], "8.0.36")
        self.assertEqual(info["charset"], "utf8mb4")
        self.assertIn("features", info)

    def test_mysql_registered(self):
        from tinkpyorm.drivers import available_drivers
        self.assertIn("mysql", available_drivers())


if __name__ == "__main__":
    unittest.main(verbosity=2)
