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


# ---------------------------------------------------------------------- #
# 8. JSON 路径写入：update_json 族（v0.6.0）


class JsonWriteTestCase(JsonTestCase):
    """路径写入的公共夹具与断言辅助。"""

    def raw_sql(self, build):
        """返回**未渲染**的 SET 段 SQL 与其绑定参数（不含 WHERE 段）。

        验证"值确实走了参数绑定"必须用未渲染版本：渲染后的 SQL 已把参数
        内联，看不出绑定痕迹。WHERE 段的参数（此处为主键）与本组断言无关，
        故按 ``WHERE`` 切分后按占位符个数取回 SET 段参数。
        """
        q = Db.table("user").where("id", 1).fetch_sql()
        build(q)
        set_sql, _, _ = q._last_sql.partition(" WHERE ")
        return set_sql, list(q._last_params[:set_sql.count("?")])

    def sql_of(self, build):
        """返回渲染后的完整 SQL（参数已内联，供人读形态）。不落库。"""
        q = Db.table("user").where("id", 1).fetch_sql()
        return build(q)

    def extra(self, name):
        """读回某行的 extra（自动解码为 dict）。"""
        return (Db.table("user").json(["extra"])
                .where("name", name).find()["extra"])

    def jtype(self, name, path):
        """取某路径的 JSON 类型名（``true`` / ``integer`` / ...）。"""
        return Db.query(
            "SELECT json_type(extra, ?) AS t FROM user WHERE name = ?",
            [path, name])[0]["t"]


class TestUpdateJsonSet(JsonWriteTestCase):
    """update_json()：路径赋值（存在覆盖、不存在新增）。"""

    def test_single_path_returns_affected_rows(self):
        self.assertEqual(
            Db.table("user").where("name", "张三").update_json("extra", "$.age", 19),
            1)

    def test_single_path_value_written(self):
        Db.table("user").where("name", "张三").update_json("extra", "$.age", 19)
        self.assertEqual(self.extra("张三")["age"], 19)

    def test_other_keys_untouched(self):
        Db.table("user").where("name", "张三").update_json("extra", "$.age", 19)
        e = self.extra("张三")
        self.assertEqual(e["city"], "北京")
        self.assertEqual(e["tags"], ["vip", "new"])
        self.assertEqual(e["score"], 88.5)

    def test_creates_missing_path(self):
        Db.table("user").where("name", "王五").update_json("extra", "$.level", "gold")
        self.assertEqual(self.extra("王五")["level"], "gold")

    def test_key_order_preserved(self):
        Db.table("user").where("name", "张三").update_json("extra", "$.age", 19)
        self.assertEqual(list(self.extra("张三").keys())[0], "age")

    def test_nested_path(self):
        Db.table("user").where("name", "张三").update_json(
            "extra", "$.tags[0]", "svip")
        self.assertEqual(self.extra("张三")["tags"], ["svip", "new"])

    def test_multi_path_dict_single_statement(self):
        n = Db.table("user").where("name", "张三").update_json(
            "extra", {"$.age": 19, "$.city": "广州"})
        self.assertEqual(n, 1)
        e = self.extra("张三")
        self.assertEqual(e["age"], 19)
        self.assertEqual(e["city"], "广州")

    def test_multi_path_compiles_to_one_set_clause(self):
        """多路径必须压进一条 SET —— 同列多 SET 只有最后一个生效。"""
        sql = self.sql_of(lambda q: q.update_json("extra", {"$.a": 1, "$.b": 2}))
        self.assertEqual(sql.count("`extra` ="), 1)
        self.assertEqual(sql.count("json_set"), 1)

    def test_multi_path_binds_all_values(self):
        sql, params = self.raw_sql(
            lambda q: q.update_json("extra", {"$.a": 1, "$.b": "x"}))
        self.assertEqual(sql.count("?"), 2)
        self.assertEqual(params, [1, "x"])

    def test_nested_object_not_escaped(self):
        """嵌套 dict 若以文本直接绑定会被存成转义字符串。"""
        Db.table("user").where("name", "张三").update_json(
            "extra", "$.profile", {"job": "dev", "skills": ["py"]})
        e = self.extra("张三")
        self.assertIsInstance(e["profile"], dict)
        self.assertEqual(e["profile"], {"job": "dev", "skills": ["py"]})

    def test_nested_object_bind_is_wrapped(self):
        sql, params = self.raw_sql(
            lambda q: q.update_json("extra", "$.o", {"k": 1}))
        self.assertIn("json(?)", sql)
        self.assertEqual(params, ['{"k": 1}'])

    def test_list_value(self):
        Db.table("user").where("name", "王五").update_json("extra", "$.tags", [1, 2])
        self.assertEqual(self.extra("王五")["tags"], [1, 2])

    def test_tuple_value_as_json_array(self):
        Db.table("user").where("name", "王五").update_json("extra", "$.tags", (1, 2))
        self.assertEqual(self.extra("王五")["tags"], [1, 2])

    def test_none_writes_json_null_not_delete(self):
        Db.table("user").where("name", "张三").update_json("extra", "$.age", None)
        e = self.extra("张三")
        self.assertIn("age", e)
        self.assertIsNone(e["age"])

    def test_true_is_json_true(self):
        """直接绑定 True 会被 sqlite3 适配为整数 1，丢失 JSON 类型。"""
        Db.table("user").where("name", "张三").update_json("extra", "$.vip", True)
        self.assertIs(self.extra("张三")["vip"], True)
        self.assertEqual(self.jtype("张三", "$.vip"), "true")

    def test_false_is_json_false(self):
        Db.table("user").where("name", "张三").update_json("extra", "$.vip", False)
        self.assertEqual(self.jtype("张三", "$.vip"), "false")

    def test_bool_bind_sql(self):
        sql, params = self.raw_sql(lambda q: q.update_json("extra", "$.v", True))
        self.assertIn("json(?)", sql)
        self.assertEqual(params, ["true"])

    def test_integer_keeps_json_integer(self):
        Db.table("user").where("name", "张三").update_json("extra", "$.n", 7)
        self.assertEqual(self.jtype("张三", "$.n"), "integer")

    def test_text_keeps_json_text(self):
        Db.table("user").where("name", "张三").update_json("extra", "$.s", "hi")
        self.assertEqual(self.jtype("张三", "$.s"), "text")

    def test_unicode_not_escaped(self):
        Db.table("user").where("name", "张三").update_json("extra", "$.s", "中文")
        raw = Db.query("SELECT extra FROM user WHERE name = ?", ["张三"])[0]["extra"]
        self.assertIn("中文", raw)
        self.assertNotIn("\\u", raw)

    def test_root_path_replaces_document(self):
        Db.table("user").where("name", "张三").update_json("extra", "$", {"only": 1})
        self.assertEqual(self.extra("张三"), {"only": 1})

    def test_qualified_path_accepted(self):
        Db.table("user").where("name", "张三").update_json("extra", "age", 20)
        self.assertEqual(self.extra("张三")["age"], 20)

    def test_with_where_json_condition(self):
        """JSON 条件筛选 + 路径写入（查询与写入同一条件语言）。"""
        n = Db.table("user").where_json("extra", "$.age", ">", 20).update_json(
            "extra", "$.level", "senior")
        self.assertEqual(n, 2)
        self.assertEqual(self.extra("王五")["level"], "senior")
        self.assertNotIn("level", self.extra("张三"))

    def test_affected_zero_when_no_match(self):
        self.assertEqual(
            Db.table("user").where("name", "不存在").update_json("extra", "$.a", 1),
            0)

    def test_multiple_rows_updated(self):
        n = Db.table("user").where_json("extra", "$.age", ">", 0).update_json(
            "extra", "$.seen", True)
        self.assertEqual(n, 3)


class TestUpdateJsonInsert(JsonWriteTestCase):
    """update_json_insert()：仅当路径不存在时写入。"""

    def test_existing_not_overwritten(self):
        Db.table("user").where("name", "张三").update_json_insert("extra", "$.age", 99)
        self.assertEqual(self.extra("张三")["age"], 18)

    def test_missing_is_created(self):
        Db.table("user").where("name", "张三").update_json_insert("extra", "$.level", "normal")
        self.assertEqual(self.extra("张三")["level"], "normal")

    def test_uses_json_insert(self):
        sql = self.sql_of(lambda q: q.update_json_insert("extra", "$.a", 1))
        self.assertIn("json_insert", sql)

    def test_affected_rows(self):
        self.assertEqual(
            Db.table("user").where("name", "张三")
            .update_json_insert("extra", "$.age", 99), 1)

    def test_json_null_path_counts_as_existing(self):
        """路径存在但值为 null 时不应被覆盖（判定的是路径存在性）。"""
        Db.table("user").where("name", "张三").update_json_insert("extra", "$.note", "x")
        e = self.extra("张三")
        self.assertIn("note", e)
        self.assertIsNone(e["note"])


class TestUpdateJsonRemove(JsonWriteTestCase):
    """update_json_remove()：删除路径。"""

    def test_single_path(self):
        Db.table("user").where("name", "张三").update_json_remove("extra", "$.note")
        self.assertNotIn("note", self.extra("张三"))

    def test_other_keys_kept(self):
        Db.table("user").where("name", "张三").update_json_remove("extra", "$.note")
        e = self.extra("张三")
        self.assertEqual(e["age"], 18)
        self.assertEqual(e["city"], "北京")

    def test_multiple_paths_one_statement(self):
        Db.table("user").where("name", "张三").update_json_remove(
            "extra", ["$.note", "$.deleted", "$.score"])
        e = self.extra("张三")
        for k in ("note", "deleted", "score"):
            self.assertNotIn(k, e)

    def test_multiple_paths_compile_to_one_call(self):
        sql = self.sql_of(
            lambda q: q.update_json_remove("extra", ["$.a", "$.b"]))
        self.assertEqual(sql.count("json_remove"), 1)
        self.assertEqual(sql.count("`extra` ="), 1)

    def test_missing_path_silent(self):
        n = Db.table("user").where("name", "王五").update_json_remove(
            "extra", "$.not_exist")
        self.assertEqual(n, 1)
        self.assertNotIn("not_exist", self.extra("王五"))

    def test_nested_path(self):
        Db.table("user").where("name", "张三").update_json_remove("extra", "$.tags[0]")
        self.assertEqual(self.extra("张三")["tags"], ["new"])

    def test_array_element_removal(self):
        Db.table("user").where("name", "李四").update_json_remove("extra", "$.tags")
        self.assertNotIn("tags", self.extra("李四"))

    def test_no_bind_params(self):
        sql, params = self.raw_sql(
            lambda q: q.update_json_remove("extra", "$.a"))
        self.assertEqual(params, [])
        self.assertNotIn("?", sql)


class TestUpdateJsonPatch(JsonWriteTestCase):
    """update_json_patch()：RFC 7396 合并补丁。"""

    def test_recursive_merge(self):
        Db.table("user").where("name", "张三").update_json_patch(
            "extra", {"city": "深圳"})
        e = self.extra("张三")
        self.assertEqual(e["city"], "深圳")
        self.assertEqual(e["age"], 18)

    def test_null_deletes_key(self):
        """补丁里的 null 是"删除键"，与 update_json 的 null 语义相反。"""
        Db.table("user").where("name", "张三").update_json_patch(
            "extra", {"note": None})
        self.assertNotIn("note", self.extra("张三"))

    def test_array_replaced_not_merged(self):
        Db.table("user").where("name", "张三").update_json_patch(
            "extra", {"tags": ["only"]})
        self.assertEqual(self.extra("张三")["tags"], ["only"])

    def test_adds_new_keys(self):
        Db.table("user").where("name", "王五").update_json_patch(
            "extra", {"a": 1, "b": 2})
        e = self.extra("王五")
        self.assertEqual((e["a"], e["b"]), (1, 2))

    def test_nested_object_merged(self):
        Db.table("user").where("name", "张三").update_json_patch(
            "extra", {"meta": {"x": 1}})
        Db.table("user").where("name", "张三").update_json_patch(
            "extra", {"meta": {"y": 2}})
        self.assertEqual(self.extra("张三")["meta"], {"x": 1, "y": 2})

    def test_uses_json_patch(self):
        sql = self.sql_of(lambda q: q.update_json_patch("extra", {"a": 1}))
        self.assertIn("json_patch", sql)

    def test_bool_in_patch_is_json_bool(self):
        Db.table("user").where("name", "张三").update_json_patch(
            "extra", {"flag": True})
        self.assertEqual(self.jtype("张三", "$.flag"), "true")


class TestUpdateJsonIfnull(JsonWriteTestCase):
    """ifnull：列为 SQL NULL 时的兜底行为。"""

    def setUp(self):
        super().setUp()
        Db.execute("INSERT INTO user (id, name, extra) VALUES (9, '空列', NULL)")

    def test_default_is_noop_on_null(self):
        """SQLite 的 json_set(NULL, ...) 返回 NULL，更新静默无效。"""
        Db.table("user").where("name", "空列").update_json("extra", "$.a", 1)
        self.assertIsNone(self.extra("空列"))

    def test_ifnull_object(self):
        Db.table("user").where("name", "空列").update_json(
            "extra", "$.a", 1, ifnull={})
        self.assertEqual(self.extra("空列"), {"a": 1})

    def test_ifnull_array(self):
        Db.table("user").where("name", "空列").update_json(
            "extra", "$[0]", 1, ifnull=[])
        self.assertEqual(self.extra("空列"), [1])

    def test_ifnull_compiles_to_coalesce(self):
        sql = self.sql_of(
            lambda q: q.update_json("extra", "$.a", 1, ifnull={}))
        self.assertIn("COALESCE(`extra`, '{}')", sql)

    def test_ifnull_has_no_bind_param(self):
        """兜底文档是驱动常量字面量，不引入绑定参数。"""
        sql, params = self.raw_sql(
            lambda q: q.update_json("extra", "$.a", 1, ifnull={}))
        self.assertEqual(params, [1])

    def test_ifnull_does_not_affect_non_null(self):
        Db.table("user").where("name", "张三").update_json(
            "extra", "$.age", 19, ifnull={})
        self.assertEqual(self.extra("张三")["age"], 19)

    def test_ifnull_on_remove_and_patch(self):
        Db.table("user").where("name", "空列").update_json_remove(
            "extra", "$.a", ifnull={})
        self.assertEqual(self.extra("空列"), {})
        Db.table("user").where("name", "空列").update_json_patch(
            "extra", {"b": 1}, ifnull={})
        self.assertEqual(self.extra("空列"), {"b": 1})

    def test_invalid_ifnull_rejected(self):
        for bad in ({"x": 1}, [1], "{}", 0, ""):
            with self.assertRaises(InvalidArgumentException):
                Db.table("user").where("id", 1).update_json(
                    "extra", "$.a", 1, ifnull=bad)


class TestUpdateJsonSqlShape(JsonWriteTestCase):
    """SQL 形态与参数绑定顺序。"""

    def test_single_set(self):
        sql = self.sql_of(lambda q: q.update_json("extra", "$.a", 1))
        self.assertEqual(
            sql, "UPDATE `user` SET `extra` = json_set(`extra`, '$.a', 1) "
                 "WHERE `id` = 1")

    def test_multi_pair_order_preserved(self):
        sql = self.sql_of(
            lambda q: q.update_json("extra", {"$.a": 1, "$.b": 2}))
        self.assertLess(sql.index("'$.a'"), sql.index("'$.b'"))

    def test_params_order_matches_placeholders(self):
        sql, params = self.raw_sql(
            lambda q: q.update_json("extra", {"$.a": 1, "$.b": 2}))
        self.assertEqual(sql.count("?"), 2)
        self.assertEqual(params, [1, 2])

    def test_path_is_inlined_as_literal(self):
        """路径是校验后内联的字面量（驱动契约），值一律绑定。"""
        sql, params = self.raw_sql(lambda q: q.update_json("extra", "$.a", 42))
        self.assertIn("'$.a'", sql)
        self.assertEqual(params, [42])

    def test_where_clause_appended(self):
        sql = self.sql_of(lambda q: q.update_json("extra", "$.a", 1))
        self.assertIn("WHERE `id` = 1", sql)

    def test_raw_value_inlined_without_bind(self):
        from tinkpyorm import raw
        sql, params = self.raw_sql(
            lambda q: q.update_json("extra", "$.n", raw("`id` + 10")))
        self.assertIn("`id` + 10", sql)
        self.assertEqual(params, [])

    def test_plain_and_json_fields_can_share_one_statement(self):
        """普通字段与 JSON 字段（不同列）可同条语句更新。"""
        q = Db.table("user").where("id", 1)
        q.options["data"] = {"name": "改名"}
        q.update_json("extra", "$.a", 1)
        sql = q._last_sql
        self.assertIn("`name` = ?", sql)
        self.assertIn("json_set", sql)

    def test_same_column_in_data_and_json_rejected(self):
        q = Db.table("user").where("id", 1)
        q.options["data"] = {"extra": {"x": 1}}
        with self.assertRaises(QueryError):
            q.update_json("extra", "$.a", 1)


class TestUpdateJsonSafety(JsonWriteTestCase):
    """注入面：列名 / 路径 / ifnull 三重校验，值一律绑定。"""

    def test_invalid_column_rejected(self):
        for bad in ("extra) OR 1=1 --", "extra; DROP TABLE user", "", "1e", "a b"):
            with self.assertRaises(InvalidArgumentException):
                Db.table("user").where("id", 1).update_json(bad, "$.a", 1)

    def test_invalid_path_rejected(self):
        for bad in ("$.a' OR '1'='1", "$.a;--", "$[x]", "$.a b", "$..a"):
            with self.assertRaises(InvalidArgumentException):
                Db.table("user").where("id", 1).update_json("extra", bad, 1)

    def test_value_never_inlined(self):
        payload = "x'); DROP TABLE user; --"
        sql, params = self.raw_sql(
            lambda q: q.update_json("extra", "$.s", payload))
        self.assertNotIn("DROP TABLE", sql)
        self.assertEqual(params, [payload])

    def test_table_intact_after_injection_attempts(self):
        for bad_col, bad_path in (("extra) OR 1=1 --", "$.a"),
                                  ("extra", "$.a' OR '1'='1")):
            try:
                Db.table("user").where("id", 1).update_json(bad_col, bad_path, 1)
            except Exception:
                pass
        self.assertEqual(
            Db.query("SELECT count(*) AS n FROM user")[0]["n"], 3)

    def test_missing_value_rejected(self):
        with self.assertRaises(InvalidArgumentException):
            Db.table("user").where("id", 1).update_json("extra", "$.a")

    def test_dict_path_with_value_rejected(self):
        with self.assertRaises(InvalidArgumentException):
            Db.table("user").where("id", 1).update_json("extra", {"$.a": 1}, 5)

    def test_empty_path_map_rejected(self):
        with self.assertRaises(InvalidArgumentException):
            Db.table("user").where("id", 1).update_json("extra", {})

    def test_remove_rejects_value(self):
        with self.assertRaises(InvalidArgumentException):
            Db.table("user").where("id", 1).update_json_remove("extra", "$.a", "v")

    def test_remove_empty_paths_rejected(self):
        with self.assertRaises(InvalidArgumentException):
            Db.table("user").where("id", 1).update_json_remove("extra", [])

    def test_patch_non_dict_rejected(self):
        for bad in ([1, 2], "{}", 3, None):
            with self.assertRaises(InvalidArgumentException):
                Db.table("user").where("id", 1).update_json_patch("extra", bad)

    def test_where_required(self):
        """无条件路径更新会改写全表，必须拒绝。"""
        with self.assertRaises(QueryError):
            Db.table("user").update_json("extra", "$.a", 1)


class TestUpdateJsonIntegration(JsonWriteTestCase):
    """与缓存、事务、模型、fetch_sql 的互操作。"""

    def test_cache_invalidated(self):
        Db.clear_cache()
        Db.table("user").json(["extra"]).where("name", "张三").cache(60).find()
        Db.table("user").where("name", "张三").update_json("extra", "$.age", 19)
        got = Db.table("user").json(["extra"]).where("name", "张三").cache(60).find()
        self.assertEqual(got["extra"]["age"], 19)

    def test_transaction_rollback(self):
        try:
            with Db.transaction():
                Db.table("user").where("name", "张三").update_json("extra", "$.tx", 1)
                raise RuntimeError("boom")
        except RuntimeError:
            pass
        self.assertNotIn("tx", self.extra("张三"))

    def test_transaction_commit(self):
        with Db.transaction():
            Db.table("user").where("name", "张三").update_json("extra", "$.tx", 1)
        self.assertEqual(self.extra("张三")["tx"], 1)

    def test_fetch_sql_returns_string(self):
        q = Db.table("user").where("id", 1).fetch_sql()
        out = q.update_json("extra", "$.a", 1)
        self.assertIsInstance(out, str)
        self.assertIn("json_set", out)

    def test_fetch_sql_does_not_write(self):
        q = Db.table("user").where("name", "张三").fetch_sql()
        q.update_json("extra", "$.a", 1)
        self.assertNotIn("a", self.extra("张三"))

    def test_interleaved_writes_both_preserved(self):
        """路径级写入在 SQL 内完成，交错执行也不丢更新。

        对照组（"读出 → Python 改 → 写回"）：两次读之后各写回整列，
        后写者会整体覆盖先写者，先写者的改动丢失。本方法没有读改写
        窗口，故两次改动都保留。
        """
        Db.execute("CREATE TABLE doc (id INTEGER PRIMARY KEY, val TEXT)")
        Db.table("doc").insert({"id": 1, "val": {"x": 0}})
        # 模拟两个逻辑操作各自"读"（时序上与读改写一致）
        Db.table("doc").json(["val"]).find(1)
        Db.table("doc").json(["val"]).find(1)
        Db.table("doc").where("id", 1).update_json("val", "$.a", 1)
        Db.table("doc").where("id", 1).update_json("val", "$.b", 2)
        got = Db.table("doc").json(["val"]).find(1)["val"]
        self.assertEqual(got, {"x": 0, "a": 1, "b": 2})

    def test_read_modify_write_loses_update_as_contrast(self):
        """反向对照：Python 侧读改写确实会丢更新（本方法要解决的问题）。"""
        Db.execute("CREATE TABLE doc2 (id INTEGER PRIMARY KEY, val TEXT)")
        Db.table("doc2").insert({"id": 1, "val": {"n": 0}})
        a = Db.table("doc2").json(["val"]).find(1)["val"]
        b = Db.table("doc2").json(["val"]).find(1)["val"]
        a["n"] += 1
        Db.table("doc2").where("id", 1).update({"val": a})
        b["n"] += 1
        Db.table("doc2").where("id", 1).update({"val": b})
        self.assertEqual(Db.table("doc2").json(["val"]).find(1)["val"]["n"], 1)

    def test_arithmetic_increment_via_raw_expression(self):
        """结合 ``raw()`` 可在 SQL 内做算术，两次自增都保留。"""
        from tinkpyorm import raw
        Db.execute("CREATE TABLE cnt (id INTEGER PRIMARY KEY, val TEXT)")
        Db.table("cnt").insert({"id": 1, "val": {"n": 0}})
        expr = raw("json_extract(`val`, '$.n') + 1")
        Db.table("cnt").where("id", 1).update_json("val", "$.n", expr)
        Db.table("cnt").where("id", 1).update_json("val", "$.n", expr)
        self.assertEqual(Db.table("cnt").json(["val"]).find(1)["val"]["n"], 2)


class TestModelUpdateJson(JsonWriteTestCase):
    """模型层入口：where 写法与 Query 一致。"""

    def test_class_update_json_with_dict_where(self):
        class User(Model):
            __table__ = "user"
            __json__ = ["extra"]

        n = User.update_json("extra", "$.age", 77, where={"name": "张三"})
        self.assertEqual(n, 1)
        self.assertEqual(self.extra("张三")["age"], 77)

    def test_class_update_json_with_pk_where(self):
        class User(Model):
            __table__ = "user"
            __json__ = ["extra"]

        User.update_json("extra", "$.age", 55, where=2)
        self.assertEqual(self.extra("李四")["age"], 55)

    def test_class_update_json_with_callable_where(self):
        class User(Model):
            __table__ = "user"
            __json__ = ["extra"]

        User.update_json("extra", "$.mark", 1,
                         where=lambda q: q.where("name", "王五"))
        self.assertEqual(self.extra("王五")["mark"], 1)

    def test_class_update_json_multi_path(self):
        class User(Model):
            __table__ = "user"
            __json__ = ["extra"]

        User.update_json("extra", {"$.a": 1, "$.b": 2}, where={"name": "王五"})
        e = self.extra("王五")
        self.assertEqual((e["a"], e["b"]), (1, 2))

    def test_class_update_json_insert(self):
        class User(Model):
            __table__ = "user"
            __json__ = ["extra"]

        User.update_json_insert("extra", "$.age", 99, where={"name": "张三"})
        self.assertEqual(self.extra("张三")["age"], 18)

    def test_class_update_json_remove(self):
        class User(Model):
            __table__ = "user"
            __json__ = ["extra"]

        User.update_json_remove("extra", "$.note", where={"name": "张三"})
        self.assertNotIn("note", self.extra("张三"))

    def test_class_update_json_patch(self):
        class User(Model):
            __table__ = "user"
            __json__ = ["extra"]

        User.update_json_patch("extra", {"note": None}, where={"name": "张三"})
        self.assertNotIn("note", self.extra("张三"))

    def test_trailing_where_not_provided_is_rejected(self):
        class User(Model):
            __table__ = "user"
            __json__ = ["extra"]

        with self.assertRaises(QueryError):
            User.update_json("extra", "$.a", 1)


class TestJsonWriteDriverContract(unittest.TestCase):
    """驱动契约：写入侧扩展点与 JSON 绑定。"""

    def test_base_driver_write_methods_raise(self):
        d = _NoJsonDriver(Config(database="x", type="mysql"))
        for call in (lambda: d.json_set("`c`", [("'$.a'", "?")]),
                     lambda: d.json_insert("`c`", [("'$.a'", "?")]),
                     lambda: d.json_remove("`c`", ["'$.a'"]),
                     lambda: d.json_patch("`c`", "?")):
            with self.assertRaises(UnsupportedOperation):
                call()

    def test_base_json_bind_is_passthrough(self):
        d = _NoJsonDriver(Config(database="x", type="mysql"))
        self.assertEqual(d.json_bind("?", {"a": 1}), "?")
        self.assertEqual(d.json_bind("?", True), "?")

    def test_unsupported_driver_write_raises(self):
        from tinkpyorm import SQLiteDriver
        register_driver("mysql", _NoJsonDriver)
        conn = Connection(config=Config(database=":memory:", type="mysql"))
        q = Query(conn, table="t")
        with self.assertRaises(UnsupportedOperation):
            q.where("id", 1).update_json("extra", "$.a", 1)

    def test_sqlite_json_bind_wraps_containers_and_bool(self):
        from tinkpyorm import SQLiteDriver
        d = SQLiteDriver(Config(database=":memory:", type="sqlite"))
        self.assertEqual(d.json_bind("?", {"a": 1}), "json(?)")
        self.assertEqual(d.json_bind("?", [1]), "json(?)")
        self.assertEqual(d.json_bind("?", (1,)), "json(?)")
        self.assertEqual(d.json_bind("?", True), "json(?)")
        self.assertEqual(d.json_bind("?", 1), "?")
        self.assertEqual(d.json_bind("?", "s"), "?")
        self.assertEqual(d.json_bind("?", None), "?")

    def test_sqlite_json_set_shape(self):
        from tinkpyorm import SQLiteDriver
        d = SQLiteDriver(Config(database=":memory:", type="sqlite"))
        self.assertEqual(d.json_set("`c`", [("'$.a'", "?")]),
                         "json_set(`c`, '$.a', ?)")
        self.assertEqual(d.json_set("`c`", [("'$.a'", "?"), ("'$.b'", "?")]),
                         "json_set(`c`, '$.a', ?, '$.b', ?)")

    def test_sqlite_json_set_requires_pairs(self):
        from tinkpyorm import SQLiteDriver
        d = SQLiteDriver(Config(database=":memory:", type="sqlite"))
        with self.assertRaises(InvalidArgumentException):
            d.json_set("`c`", [])

    def test_sqlite_json_remove_shape(self):
        from tinkpyorm import SQLiteDriver
        d = SQLiteDriver(Config(database=":memory:", type="sqlite"))
        self.assertEqual(d.json_remove("`c`", ["'$.a'", "'$.b'"]),
                         "json_remove(`c`, '$.a', '$.b')")

    def test_sqlite_json_patch_shape(self):
        from tinkpyorm import SQLiteDriver
        d = SQLiteDriver(Config(database=":memory:", type="sqlite"))
        self.assertEqual(d.json_patch("`c`", "?"), "json_patch(`c`, ?)")

    def test_json_update_mode_validated(self):
        from tinkpyorm.builder import Builder, JsonUpdate
        conn = Connection(config=Config(database=":memory:", type="sqlite"))
        b = Builder(conn)
        bad = JsonUpdate("e", "$.a", 1, "upsert")
        with self.assertRaises(QueryError):
            b._build_json_update([bad])

    def test_builder_folds_mixed_modes_into_one_set_clause(self):
        """异类操作按序左嵌套，保证占位符顺序与参数顺序一致。"""
        from tinkpyorm.builder import Builder, JsonUpdate
        conn = Connection(config=Config(database=":memory:", type="sqlite"))
        b = Builder(conn)
        specs = [JsonUpdate("e", "$.a", 1, "set"),
                 JsonUpdate("e", None, {"p": 2}, "patch"),
                 JsonUpdate("e", "$.b", None, "remove")]
        set_sql, params = b._build_json_update(specs)
        self.assertEqual(len(set_sql), 1)
        self.assertIn("json_remove(json_patch(json_set(`e`, '$.a', ?), json(?)), '$.b')",
                      set_sql[0])
        self.assertEqual(params, [1, '{"p": 2}'])


if __name__ == "__main__":
    unittest.main(verbosity=2)
