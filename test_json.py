# -*- coding: utf-8 -*-
"""JSON 查询与自动格式化测试（v0.5.0）。

覆盖：
    1. 写入侧 dict/list 自动序列化（insert / insert_all / update）
    2. 读取侧自动格式化为 Python dict（find / select / value / column /
       chunk / cursor / paginate / 模型属性）
    3. JSON 路径条件：等值、比较、in、between、like、null、exists、
       contains、length、type
    4. field_json / order_json 与别名生成
    5. 路径规范化与安全性校验（列名 / 别名 / 路径注入面）
    6. 驱动契约：NoSQL 恒等透传、不支持 JSON 的驱动显式报错
    7. 与查询缓存、软删除、链式普通条件的互操作
"""
import json
import unittest

from tinkpyorm import (
    Db, Model, Query, Connection, SQLDriver, NoSQLDriver,
    JsonWhere, UnsupportedOperation, InvalidArgumentException, QueryError,
    normalize_json_path, register_driver,
)
from tinkpyorm.config import Config


class _NoJsonDriver(SQLDriver):
    """不支持 JSON 的假驱动，用于验证能力开关的行为。

    使用 ``mysql`` 作为注册名：该名字已在 Config 的合法类型列表中
    （为未来 MySQL 驱动预留），因此无需改动生产代码即可完成注册。
    """

    name = "mysql"
    supports_json = False

    def connect(self):
        return None

    def close(self):
        pass

    def select(self, sql, params=()):
        return []

    def execute(self, sql, params=()):
        return 0

    def insert(self, sql, params=()):
        return 0


class _FakeNoSQL(NoSQLDriver):
    """模拟 MongoDB / Redis 的 NoSQL 驱动（无第三方依赖）。"""

    name = "redis"

    def connect(self):
        return None

    def close(self):
        pass


class JsonTestCase(unittest.TestCase):
    """公共夹具：内存库 + 三行含 JSON 列的记录。"""

    def setUp(self):
        Db.close()
        Db.set_config({"database": ":memory:"})
        Db.execute("CREATE TABLE user ("
                   "id INTEGER PRIMARY KEY, name TEXT, extra TEXT)")
        self.data = [
            {"name": "张三", "extra": {"age": 18, "city": "北京",
                                       "tags": ["vip", "new"],
                                       "deleted": False, "note": None,
                                       "score": 88.5}},
            {"name": "李四", "extra": {"age": 25, "city": "上海",
                                       "tags": ["new"], "deleted": True,
                                       "score": 92.0}},
            {"name": "王五", "extra": {"age": 30, "city": "北京",
                                       "tags": [], "score": 75.0}},
        ]
        Db.table("user").insert_all(self.data)

    def names(self, query):
        return [r["name"] for r in query.select()]


# ---------------------------------------------------------------------- #
# 1. 写入侧：Python 对象 -> JSON 文本
# ---------------------------------------------------------------------- #
class TestWriteEncoding(JsonTestCase):

    def test_insert_encodes_dict(self):
        raw = Db.query("SELECT extra FROM user WHERE name = ?", ["张三"])[0]
        self.assertIsInstance(raw["extra"], str)
        self.assertEqual(json.loads(raw["extra"])["city"], "北京")

    def test_insert_all_encodes_dict(self):
        raw = Db.query("SELECT extra FROM user WHERE name = ?", ["王五"])[0]
        self.assertEqual(json.loads(raw["extra"])["tags"], [])

    def test_update_encodes_dict(self):
        Db.table("user").where("name", "王五").update(
            {"extra": {"age": 31, "city": "北京", "tags": ["old"]}})
        row = Db.table("user").json().where("name", "王五").find()
        self.assertEqual(row["extra"]["age"], 31)
        self.assertEqual(row["extra"]["tags"], ["old"])

    def test_insert_plain_values_untouched(self):
        Db.table("user").insert({"name": "赵六", "extra": None})
        raw = Db.query("SELECT extra FROM user WHERE name = ?", ["赵六"])[0]
        self.assertIsNone(raw["extra"])

    def test_last_sql_shows_json_text(self):
        Db.table("user").where("name", "李四").update({"extra": {"a": 1}})
        self.assertIn("{\"a\": 1}", Db.get_last_sql())


# ---------------------------------------------------------------------- #
# 2. 读取侧：JSON 文本 -> Python dict
# ---------------------------------------------------------------------- #
class TestAutoFormat(JsonTestCase):

    def test_auto_sniff_select(self):
        rows = Db.table("user").json().select()
        self.assertIsInstance(rows[0]["extra"], dict)
        self.assertEqual(rows[0]["extra"]["tags"], ["vip", "new"])

    def test_explicit_fields_select(self):
        rows = Db.table("user").json(["extra"]).select()
        self.assertIsInstance(rows[0]["extra"], dict)

    def test_json_false_disables(self):
        rows = Db.table("user").json(False).select()
        self.assertIsInstance(rows[0]["extra"], str)

    def test_json_str_shorthand(self):
        rows = Db.table("user").json("extra").select()
        self.assertIsInstance(rows[0]["extra"], dict)

    def test_find_returns_dict(self):
        row = Db.table("user").json().where("name", "张三").find()
        self.assertIsInstance(row, dict)
        self.assertIsInstance(row["extra"], dict)
        self.assertIsNone(row["extra"]["note"])

    def test_find_by_pk_returns_dict(self):
        row = Db.table("user").json().find(1)
        self.assertIsInstance(row["extra"], dict)

    def test_value_returns_dict(self):
        v = Db.table("user").json(["extra"]).where("name", "李四").value("extra")
        self.assertIsInstance(v, dict)
        self.assertEqual(v["city"], "上海")

    def test_column_returns_dicts(self):
        col = Db.table("user").json(["extra"]).column("extra")
        self.assertTrue(all(isinstance(x, dict) for x in col))
        self.assertEqual([x["city"] for x in col], ["北京", "上海", "北京"])

    def test_chunk_returns_dicts(self):
        got = []
        for batch in Db.table("user").json().order("id").chunk(2):
            got.extend(batch)
        self.assertEqual(len(got), 3)
        self.assertTrue(all(isinstance(r["extra"], dict) for r in got))

    def test_cursor_returns_dicts(self):
        got = list(Db.table("user").json().order("id").cursor(1))
        self.assertEqual(len(got), 3)
        self.assertTrue(all(isinstance(r["extra"], dict) for r in got))

    def test_paginate_items_dict(self):
        page = Db.table("user").json().paginate(list_rows=2, page=1)
        self.assertTrue(all(isinstance(r["extra"], dict) for r in page.items))

    def test_returns_are_independent_copies(self):
        """修改返回值的 JSON 对象不得影响后续查询。"""
        first = Db.table("user").json().where("name", "张三").find()
        first["extra"]["city"] = "被篡改"
        second = Db.table("user").json().where("name", "张三").find()
        self.assertEqual(second["extra"]["city"], "北京")

    def test_non_json_text_kept(self):
        Db.table("user").insert({"name": "文本", "extra": "普通字符串"})
        row = Db.table("user").json().where("name", "文本").find()
        self.assertEqual(row["extra"], "普通字符串")


# ---------------------------------------------------------------------- #
# 3. 模型层
# ---------------------------------------------------------------------- #
class UserModel(Model):
    __table__ = "user"
    __json__ = ["extra"]


class TestModelJson(JsonTestCase):

    def test_model_json_attribute_is_dict(self):
        u = UserModel.find(1)
        self.assertIsInstance(u.extra, dict)
        self.assertEqual(u.extra["city"], "北京")

    def test_model_to_dict_is_dict(self):
        u = UserModel.find(1)
        self.assertIsInstance(u.to_dict()["extra"], dict)

    def test_model_select_collection(self):
        users = UserModel.where_json("extra", "$.city", "北京").select()
        self.assertEqual(len(users), 2)
        self.assertTrue(all(isinstance(u.extra, dict) for u in users))

    def test_model_create_and_read_back(self):
        u = UserModel.create({"name": "钱七", "extra": {"age": 40, "tags": ["x"]}})
        again = UserModel.find(u.id)
        self.assertEqual(again.extra["age"], 40)

    def test_deserialize_is_idempotent(self):
        u = UserModel()
        payload = {"age": 1}
        self.assertIs(u._deserialize("extra", payload), payload)

    def test_json_type_declaration(self):
        class Typed(Model):
            __table__ = "user"
            __type__ = {"extra": "json"}

        self.assertIsInstance(Typed.find(1).extra, dict)


# ---------------------------------------------------------------------- #
# 4. JSON 路径条件
# ---------------------------------------------------------------------- #
class TestWhereJson(JsonTestCase):

    def test_numeric_compare(self):
        self.assertEqual(self.names(Db.table("user").where_json(
            "extra", "$.age", ">", 18)), ["李四", "王五"])

    def test_three_arg_sugar_equals(self):
        self.assertEqual(self.names(Db.table("user").where_json(
            "extra", "$.city", "北京")), ["张三", "王五"])

    def test_three_arg_sugar_number(self):
        self.assertEqual(self.names(Db.table("user").where_json(
            "extra", "$.age", 25)), ["李四"])

    def test_bool_mapped_to_json_bool(self):
        self.assertEqual(self.names(Db.table("user").where_json(
            "extra", "$.deleted", False)), ["张三"])
        self.assertEqual(self.names(Db.table("user").where_json(
            "extra", "$.deleted", True)), ["李四"])

    def test_float_compare(self):
        self.assertEqual(self.names(Db.table("user").where_json(
            "extra", "$.score", ">=", 90)), ["李四"])

    def test_where_json_or(self):
        got = self.names(Db.table("user")
                         .where_json("extra", "$.age", ">", 28)
                         .where_json_or("extra", "$.city", "上海"))
        self.assertEqual(sorted(got), ["李四", "王五"])

    def test_where_json_in(self):
        self.assertEqual(self.names(Db.table("user").where_json(
            "extra", "$.age", "in", [18, 30])), ["张三", "王五"])

    def test_where_json_between(self):
        self.assertEqual(self.names(Db.table("user").where_json(
            "extra", "$.age", "between", [20, 29])), ["李四"])

    def test_where_json_like(self):
        Db.table("user").insert({"name": "赵六", "extra": {"city": "北京市"}})
        self.assertEqual(self.names(Db.table("user").where_json(
            "extra", "$.city", "like", "北京%")), ["张三", "王五", "赵六"])

    def test_where_json_null_operator(self):
        """is null 表示"值为 null 或路径不存在"。"""
        got = self.names(Db.table("user").where_json("extra", "$.note", "is null"))
        self.assertEqual(sorted(got), ["张三", "李四", "王五"])

    def test_where_json_not_null_operator(self):
        got = self.names(Db.table("user").where_json(
            "extra", "$.note", "is not null"))
        self.assertEqual(got, [])

    def test_where_json_none_value_becomes_is_null(self):
        got = self.names(Db.table("user").where_json("extra", "$.note", None))
        self.assertEqual(len(got), 3)

    def test_where_json_exists_distinguishes_null(self):
        """note 键存在但值为 null，exists 仍应命中；不存在的键则不命中。"""
        self.assertTrue(Db.table("user").where_json_exists("extra", "$.note")
                        .where("name", "张三").find())
        self.assertIsNone(Db.table("user").where_json_exists("extra", "$.nokey")
                          .where("name", "张三").find())

    def test_where_json_not_exists(self):
        self.assertEqual(self.names(Db.table("user").where_json_not_exists(
            "extra", "$.nokey")), ["张三", "李四", "王五"])
        # deleted 键：张三 False / 李四 True 均存在，仅王五缺失
        self.assertEqual(self.names(Db.table("user").where_json_not_exists(
            "extra", "$.deleted")), ["王五"])

    def test_where_json_null_helper(self):
        self.assertEqual(len(self.names(
            Db.table("user").where_json_null("extra", "$.note"))), 3)

    def test_where_json_not_null_helper(self):
        self.assertEqual(len(self.names(
            Db.table("user").where_json_not_null("extra", "$.city"))), 3)

    def test_where_json_contains(self):
        self.assertEqual(self.names(Db.table("user").where_json_contains(
            "extra", "$.tags", "vip")), ["张三"])

    def test_where_json_not_contains(self):
        self.assertEqual(sorted(self.names(Db.table("user").where_json_not_contains(
            "extra", "$.tags", "vip"))), ["李四", "王五"])

    def test_where_json_length(self):
        self.assertEqual(self.names(Db.table("user").where_json_length(
            "extra", "$.tags", ">=", 2)), ["张三"])
        self.assertEqual(sorted(self.names(Db.table("user").where_json_length(
            "extra", "$.tags", 1))), ["李四"])

    def test_where_json_type(self):
        self.assertEqual(len(self.names(Db.table("user").where_json_type(
            "extra", "$.tags", "array"))), 3)
        self.assertEqual(len(self.names(Db.table("user").where_json_type(
            "extra", "$.age", "integer"))), 3)
        self.assertEqual(self.names(Db.table("user").where_json_type(
            "extra", "$.note", "null")), ["张三"])

    def test_nested_path(self):
        Db.table("user").insert({
            "name": "深层", "extra": {"profile": {"contact": {"mail": "a@b.c"}}}})
        self.assertEqual(self.names(Db.table("user").where_json(
            "extra", "$.profile.contact.mail", "a@b.c")), ["深层"])

    def test_path_without_dollar_prefix(self):
        self.assertEqual(self.names(Db.table("user").where_json(
            "extra", "age", ">", 28)), ["王五"])

    def test_mix_with_plain_where(self):
        got = self.names(Db.table("user")
                         .where_json("extra", "$.age", ">", 18)
                         .where("name", "!=", "张三")
                         .where_like("name", "%李%"))
        self.assertEqual(got, ["李四"])

    def test_condition_is_jsonwhere_object(self):
        q = Db.table("user").where_json("extra", "$.age", ">", 18)
        conds = [c for _, c in q.options["where"]]
        self.assertTrue(any(isinstance(c, JsonWhere) for c in conds))

    def test_where_json_with_cache(self):
        q = Db.table("user").json().where_json("extra", "$.city", "北京").cache(10)
        self.assertEqual(len(q.select()), 2)
        rows = Db.table("user").json().where_json(
            "extra", "$.city", "北京").cache(10).select()
        self.assertIsInstance(rows[0]["extra"], dict)

    def test_no_match_returns_empty(self):
        self.assertEqual(self.names(Db.table("user").where_json(
            "extra", "$.age", ">", 999)), [])


# ---------------------------------------------------------------------- #
# 5. field_json / order_json
# ---------------------------------------------------------------------- #
class TestJsonFields(JsonTestCase):

    def test_field_json_with_alias(self):
        rows = Db.table("user").field_json("extra", "$.city", "city").select()
        self.assertEqual([r["city"] for r in rows], ["北京", "上海", "北京"])

    def test_field_json_auto_alias(self):
        rows = Db.table("user").field_json("extra", "$.city").select()
        self.assertIn("city", rows[0])

    def test_field_json_alias_from_array_path(self):
        rows = Db.table("user").field_json("extra", "$.tags[0]").select()
        self.assertIn("tags", rows[0])

    def test_field_json_root_falls_back_to_column(self):
        rows = Db.table("user").field_json("extra", "$").select()
        self.assertIsInstance(rows[0]["extra"], dict)

    def test_field_json_merges_with_field_either_order(self):
        a = Db.table("user").field_json("extra", "$.city").field("name").select()
        b = Db.table("user").field("name").field_json("extra", "$.city").select()
        for rows in (a, b):
            self.assertEqual(set(rows[0].keys()), {"name", "city"})

    def test_field_json_multiple(self):
        rows = Db.table("user").field_json("extra", "$.city") \
            .field_json("extra", "$.age").field("name").select()
        self.assertEqual(set(rows[0].keys()), {"name", "city", "age"})

    def test_field_json_duplicate_alias_suffixed(self):
        rows = Db.table("user").field_json("extra", "$.city") \
            .field_json("extra", "$.city").field("name").select()
        self.assertIn("city_2", rows[0])

    def test_field_json_returns_json_object(self):
        rows = Db.table("user").field_json("extra", "$.tags").select()
        self.assertIsInstance(rows[0]["tags"], list)

    def test_field_json_sql_shape(self):
        """未指定 field() 时保留 * 并追加 JSON 字段（追加语义）。"""
        sql = Db.table("user").field_json("extra", "$.city", "city").fetch_sql().select()
        self.assertEqual(
            sql,
            "SELECT *, json_extract(`extra`, '$.city') AS `city` FROM `user`")

    def test_field_json_sql_shape_with_field(self):
        sql = Db.table("user").field("name") \
            .field_json("extra", "$.city", "city").fetch_sql().select()
        self.assertEqual(
            sql, "SELECT `name`, json_extract(`extra`, '$.city') AS `city` "
                 "FROM `user`")

    def test_order_json_desc(self):
        got = self.names(Db.table("user").order_json("extra", "$.age", "desc"))
        self.assertEqual(got, ["王五", "李四", "张三"])

    def test_order_json_asc_default(self):
        got = self.names(Db.table("user").order_json("extra", "$.score"))
        self.assertEqual(got, ["王五", "张三", "李四"])

    def test_order_json_invalid_direction(self):
        with self.assertRaises(InvalidArgumentException):
            Db.table("user").order_json("extra", "$.age", "sideways")


# ---------------------------------------------------------------------- #
# 6. 路径规范与安全
# ---------------------------------------------------------------------- #
class TestPathAndSafety(unittest.TestCase):

    def test_normalize(self):
        cases = {
            None: "$", "": "$", "$": "$",
            "a": "$.a", ".a": "$.a", "a.b": "$.a.b",
            "[0]": "$[0]", "$.a[0].b": "$.a[0].b",
            "$.\"带 空格\"": "$.\"带 空格\"",
        }
        for raw, expect in cases.items():
            self.assertEqual(normalize_json_path(raw), expect, raw)

    def test_normalize_rejects_injection(self):
        for bad in ["$.a' OR '1'='1", "$.a); DROP TABLE user; --",
                    "$.a\u0000", "$.a b", "$$.a"]:
            with self.assertRaises(InvalidArgumentException, msg=bad):
                normalize_json_path(bad)

    def test_path_is_inlined_as_escaped_literal(self):
        sql = Db.table("user").where_json(
            "extra", "$.\"it's\"", "=", 1).fetch_sql().select()
        self.assertIn("'$.\"it''s\"'", sql)
        self.assertIn("= 1", sql)


class TestSafety(JsonTestCase):

    def test_invalid_column_rejected(self):
        with self.assertRaises(InvalidArgumentException):
            Db.table("user").where_json("extra) OR 1=1 --", "$.a", 1).select()

    def test_invalid_alias_rejected(self):
        with self.assertRaises(InvalidArgumentException):
            Db.table("user").field_json("extra", "$.a", "x; DROP TABLE user")

    def test_invalid_alias_rejected_in_order(self):
        with self.assertRaises(InvalidArgumentException):
            Db.table("user").order_json("extra; --", "$.a")

    def test_complex_value_rejected(self):
        with self.assertRaises(QueryError):
            Db.table("user").where_json("extra", "$.tags", "=", ["vip"]).select()

    def test_unknown_operator_rejected(self):
        with self.assertRaises(QueryError):
            Db.table("user").where_json("extra", "$.a", "~=", 1).fetch_sql().select()

    def test_json_length_bad_op_rejected(self):
        with self.assertRaises(QueryError):
            Db.table("user").where_json_length(
                "extra", "$.tags", "~~", 1).fetch_sql().select()

    def test_where_json_type_needs_name(self):
        with self.assertRaises(InvalidArgumentException):
            Db.table("user").where_json_type("extra", "$.a", "")


# ---------------------------------------------------------------------- #
# 7. 驱动契约（多数据库扩展点）
# ---------------------------------------------------------------------- #
class TestDriverContract(unittest.TestCase):
    """驱动能力契约：多数据库扩展的验证入口。

    测试期间会向注册表临时登记假驱动，tearDown 中还原，避免污染其它用例。
    """

    def setUp(self):
        from tinkpyorm import drivers as _drivers
        self._drivers = _drivers
        self._backup = dict(_drivers._DRIVERS)

    def tearDown(self):
        self._drivers._DRIVERS.clear()
        self._drivers._DRIVERS.update(self._backup)

    def test_nosql_decode_is_identity(self):
        """MongoDB / Redis 返回原生对象，编解码必须恒等。"""
        d = _FakeNoSQL(Config(database="x", type="redis"))
        payload = {"a": [1, 2]}
        self.assertIs(d.json_decode(payload), payload)
        self.assertIs(d.json_encode(payload), payload)
        self.assertTrue(d.supports_json)

    def test_sqlite_driver_declares_json(self):
        from tinkpyorm import SQLiteDriver
        d = SQLiteDriver(Config(database=":memory:", type="sqlite"))
        self.assertTrue(d.supports_json)
        self.assertEqual(d.json_extract("`c`", "$.a"),
                         "json_extract(`c`, '$.a')")
        self.assertIn("json_each", d.json_contains("`c`", "$.a"))
        self.assertIn("count(*)", d.json_length("`c`", "$.a"))
        self.assertIn("json_type", d.json_type("`c`", "$.a"))
        self.assertIn("json_type", d.json_exists("`c`", "$.a"))

    def test_sqlite_decode_passthrough_for_plain_text(self):
        from tinkpyorm import SQLiteDriver
        d = SQLiteDriver(Config(database=":memory:", type="sqlite"))
        self.assertEqual(d.json_decode("hello"), "hello")
        self.assertEqual(d.json_decode("[1,2]"), [1, 2])
        self.assertEqual(d.json_decode(None), None)

    def test_unsupported_driver_raises(self):
        register_driver("mysql", _NoJsonDriver)
        conn = Connection(config=Config(database=":memory:", type="mysql"))
        q = Query(conn, table="t")
        with self.assertRaises(UnsupportedOperation):
            q.where_json("extra", "$.a", "=", 1).fetch_sql().select()

    def test_base_driver_json_methods_raise(self):
        d = _NoJsonDriver(Config(database="x", type="mysql"))
        for call in (lambda: d.json_extract("c", "$"),
                     lambda: d.json_exists("c", "$"),
                     lambda: d.json_contains("c", "$"),
                     lambda: d.json_length("c", "$"),
                     lambda: d.json_type("c", "$")):
            with self.assertRaises(UnsupportedOperation):
                call()


if __name__ == "__main__":
    unittest.main(verbosity=2)
