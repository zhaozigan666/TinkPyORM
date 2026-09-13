"""json 自动建表/建列模式（v0.9.0）单元测试。

覆盖：开关默认关闭 / 自动建表（insert 与读路径）/ 缺失字段自动加列 /
类型推断与 JSON 编码回读 / insert_all 异构行并集补 None / 显式 id /
表前缀 / 非法字段名拒绝 / fetch_sql 不产生 DDL / join 不参与 / 缓存失效。
"""
import sqlite3
import unittest

from tinkpyorm import Db
from tinkpyorm.exceptions import InvalidArgumentException


class _Base(unittest.TestCase):

    def tearDown(self):
        Db.close()


def _on(**extra):
    Db.close()
    Db.set_config({"database": ":memory:", "json": True, **extra})


def _off(**extra):
    Db.close()
    Db.set_config({"database": ":memory:", **extra})


class TestSwitchDefault(_Base):

    def test_default_off_missing_table_raises(self):
        _off()
        with self.assertRaises(sqlite3.OperationalError):
            Db.table("t1").insert({"a": 1})

    def test_default_off_config_value(self):
        _off()
        self.assertFalse(Db.get_connection().config.json)

    def test_on_config_value(self):
        _on()
        self.assertTrue(Db.get_connection().config.json)


class TestAutoCreate(_Base):

    def setUp(self):
        _on()

    def test_insert_creates_table_and_row(self):
        uid = Db.table("events").insert({"type": "click", "n": 3})
        self.assertEqual(uid, 1)
        row = Db.table("events").where("id", uid).find()
        self.assertEqual(row["type"], "click")
        self.assertEqual(row["n"], 3)

    def test_name_respects_prefix(self):
        Db.set_config({"database": ":memory:", "json": True,
                       "prefix": "app_"})
        Db.name("logs").insert({"msg": "hi"})
        tables = {r["name"] for r in Db.get_connection().query(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn("app_logs", tables)
        self.assertNotIn("logs", tables)

    def test_missing_fields_added_on_later_insert(self):
        Db.table("t2").insert({"a": 1})
        Db.table("t2").insert({"a": 2, "b": "x", "c": 1.5})
        rows = Db.table("t2").order("id").select()
        self.assertEqual(len(rows), 2)
        self.assertIsNone(rows[0]["b"])          # 旧行新列为 NULL
        self.assertEqual(rows[1]["b"], "x")
        self.assertEqual(rows[1]["c"], 1.5)

    def test_read_path_creates_missing_table(self):
        self.assertEqual(Db.table("empty_t").select(), [])
        self.assertEqual(Db.table("empty_t").count(), 0)
        self.assertIsNone(Db.table("empty_t").where("id", 1).find())
        tables = {r["name"] for r in Db.get_connection().query(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn("empty_t", tables)

    def test_explicit_id_respected(self):
        Db.table("t3").insert({"id": 42, "a": 1})
        self.assertEqual(Db.table("t3").where("id", 42).count(), 1)
        self.assertIsNone(Db.table("t3").find(1))

    def test_types_inferred_and_json_roundtrip(self):
        Db.table("t4").insert({"i": 7, "f": 2.5, "s": "文本",
                               "b": True, "d": {"x": 1}, "l": [1, 2]})
        row = Db.table("t4").find()
        self.assertEqual(row["i"], 7)
        self.assertEqual(row["f"], 2.5)
        self.assertEqual(row["s"], "文本")
        self.assertEqual(row["b"], 1)            # bool 落 INTEGER
        # dict/list 以 JSON 文本存储；json() 指定字段解码回读
        raw = Db.table("t4").find()
        self.assertEqual(raw["d"], '{"x": 1}')
        decoded = Db.table("t4").json(["d", "l"]).find()
        self.assertEqual(decoded["d"], {"x": 1})
        self.assertEqual(decoded["l"], [1, 2])

    def test_insert_all_heterogeneous_rows(self):
        n = Db.table("t5").insert_all(
            [{"a": 1}, {"b": "x"}, {"a": 2, "b": "y", "c": 3}])
        self.assertEqual(n, 3)
        rows = Db.table("t5").order("id").select()
        self.assertEqual(rows[0]["a"], 1)
        self.assertIsNone(rows[0]["b"])
        self.assertEqual(rows[1]["b"], "x")
        self.assertIsNone(rows[1]["a"])
        self.assertEqual(rows[2]["c"], 3)

    def test_existing_table_with_columns_untouched(self):
        Db.close()
        Db.set_config({"database": ":memory:"})
        Db.execute("CREATE TABLE t6 (id INTEGER PRIMARY KEY, a TEXT)")
        Db.close()
        _on()
        Db.table("t6").insert({"a": "keep", "extra": 1})
        cols = [r["name"] for r in
                Db.get_connection().query("PRAGMA table_info(t6)")]
        self.assertEqual(cols, ["id", "a", "extra"])  # 原列未动，仅追加

    def test_cache_invalidated_after_auto_insert(self):
        Db.table("t7").insert({"a": 1})
        self.assertEqual(Db.table("t7").cache(60).count(), 1)
        Db.table("t7").insert({"a": 2})
        self.assertEqual(Db.table("t7").cache(60).count(), 2)


class TestBoundaries(_Base):

    def setUp(self):
        _on()

    def test_invalid_field_name_rejected(self):
        with self.assertRaises(InvalidArgumentException):
            Db.table("t8").insert({"a b": 1})
        with self.assertRaises(InvalidArgumentException):
            Db.table("t8").insert({"a;b": 1})

    def test_join_main_created_side_table_not(self):
        # join 语义：主表参与自动建表，关联表不参与
        with self.assertRaises(sqlite3.OperationalError):
            Db.table("jm").join(
                "nope",
                lambda c: c.where_column("jm.id", "=", "nope.id")).select()
        tables = {r["name"] for r in Db.get_connection().query(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn("jm", tables)       # 主表被自动创建
        self.assertNotIn("nope", tables)  # 关联表不会自动创建

    def test_fetch_sql_produces_no_ddl(self):
        sql = Db.table("t9").fetch_sql().insert({"a": 1})
        self.assertIn("INSERT INTO", str(sql))
        tables = {r["name"] for r in Db.get_connection().query(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertNotIn("t9", tables)

    def test_update_does_not_add_columns(self):
        Db.table("t10").insert({"a": 1})
        with self.assertRaises(sqlite3.OperationalError):
            Db.table("t10").where("id", 1).update({"new_col": 5})

    def test_toggle_mid_session(self):
        _off()
        with self.assertRaises(sqlite3.OperationalError):
            Db.table("t11").insert({"a": 1})
        Db.set_config({"database": ":memory:", "json": True})
        Db.table("t11").insert({"a": 1})   # 开启后同一入口直接可用
        self.assertEqual(Db.table("t11").count(), 1)


if __name__ == "__main__":
    unittest.main()
