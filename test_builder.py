# -*- coding: utf-8 -*-
"""builder.py SQL 生成层独立单元测试（审查报告 K2：此前仅经 query 间接覆盖）。

经 Query 公共链式 API 构造 options，再取 ``(sql, params)`` 断言：
1. 片段正确（WHERE/ORDER/LIMIT/别名/Raw）；
2. **参数化完整性**：值绝不内联进 SQL（`?` 占位数 == params 数），
   与审查报告"SQL 注入防护"结论形成回归防线。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from tinkpyorm import Db, raw
from tinkpyorm.connection import Connection

def _fresh_db():
    """每个测试类独立重建 :memory: 环境（discover 下全局连接会被其他模块重置）。"""
    Db.close()
    Db.set_config(Connection(":memory:"))
    Db.execute("""
CREATE TABLE user (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT, age INTEGER DEFAULT 0, balance REAL DEFAULT 0
)""")


class TestSelectSql(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _fresh_db()
    def test_basic_parameterized(self):
        sql, params = Db.table("user").where("age", ">", 18)._execute_select()
        self.assertIn("SELECT", sql)
        self.assertIn("FROM", sql)
        self.assertIn("WHERE", sql)
        self.assertEqual(sql.count("?"), len(params))
        self.assertNotIn("18", sql)
        self.assertEqual(params, [18])

    def test_multi_where_params_order(self):
        sql, params = (Db.table("user")
                       .where("age", ">", 18)
                       .where("name", "like", "%a%")
                       ._execute_select())
        self.assertEqual(params, [18, "%a%"])
        self.assertEqual(sql.count("?"), 2)

    def test_order_limit_fragments(self):
        sql, params = (Db.table("user").order("id", "desc")
                       .limit(5)._execute_select())
        self.assertIn("ORDER BY", sql)
        self.assertIn("DESC", sql)
        self.assertIn("LIMIT", sql)
        self.assertEqual(params, [])

    def test_field_alias(self):
        sql, params = Db.table("user").field({"uid": "id"})._execute_select()
        self.assertIn("AS", sql)
        self.assertIn("`uid`", sql)
        self.assertEqual(params, [])

    def test_raw_where_inline_by_design(self):
        sql, params = (Db.table("user")
                       .where("balance", ">", raw("`balance` - 10"))
                       ._execute_select())
        self.assertIn("`balance` - 10", sql)
        self.assertEqual(params, [])

    def test_no_where(self):
        sql, params = Db.table("user")._execute_select()
        self.assertNotIn("WHERE", sql)
        self.assertEqual(params, [])


class TestWriteSql(unittest.TestCase):
    """写路径参数化直接断言 builder 层产出的 ``(sql, params)``——
    这才是真正执行时使用的参数对（fetch_sql 模式仅用于调试显示，见下）。"""

    @classmethod
    def setUpClass(cls):
        _fresh_db()

    def _write_sql_params(self, method, *args, **kwargs):
        q = Db.table("user")
        return getattr(q.builder, method)(q.options, *args, **kwargs)

    def test_insert_parameterized(self):
        sql, params = self._write_sql_params("insert", {"name": "n", "age": 3})
        self.assertTrue(sql.upper().startswith("INSERT INTO"))
        self.assertEqual(sql.count("?"), 2)
        self.assertEqual(params, ["n", 3])
        self.assertNotIn("'n'", sql)
        self.assertNotIn("3", sql)

    def test_insert_all_placeholders(self):
        sql, params = self._write_sql_params(
            "insert_all", [{"name": "a", "age": 1}, {"name": "b", "age": 2}])
        self.assertTrue(sql.upper().startswith("INSERT INTO"))
        self.assertEqual(sql.count("?"), 4)
        self.assertEqual(len(params), 4)

    def test_update_parameterized(self):
        q = Db.table("user").where("id", 1)
        sql, params = q.builder.update(q.options, {"name": "x", "age": 9})
        self.assertTrue(sql.upper().startswith("UPDATE"))
        self.assertIn("SET", sql)
        self.assertIn("WHERE", sql)
        self.assertEqual(sql.count("?"), 3)
        self.assertEqual(params, ["x", 9, 1])

    def test_delete_has_where(self):
        q = Db.table("user").where("id", 1)
        sql, params = q.builder.delete(q.options)
        self.assertTrue(sql.upper().startswith("DELETE FROM"))
        self.assertIn("WHERE", sql)
        self.assertEqual(params, [1])

    def test_fetch_sql_inlines_for_display_only(self):
        """钉住 fetch_sql 的文档化行为：输出为调试渲染（值内联）、不执行。"""
        before = Db.table("user").count()
        sql = Db.table("user").fetch_sql().insert({"name": "ghost", "age": 0})
        self.assertIn("'ghost'", sql)  # 显示用内联渲染
        self.assertEqual(Db.table("user").count(), before)  # 未执行

    def test_write_does_not_touch_db(self):
        before = Db.table("user").count()
        Db.table("user").fetch_sql().insert({"name": "ghost", "age": 0})
        self.assertEqual(Db.table("user").count(), before)


if __name__ == "__main__":
    unittest.main()
