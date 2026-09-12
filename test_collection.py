"""DocumentCollection（v0.8.0 文档集合）单元测试。

覆盖：幂等建表 / CRUD / 点路径条件与操作符词汇 / 排序分页 / 聚合 /
表达式索引 / 字段提升（生成列）/ schema 校验开关 / 边界与非法输入。
"""
import unittest

from tinkpyorm import Db, DocumentCollection
from tinkpyorm.exceptions import (
    InvalidArgumentException, QueryError,
)


def _fresh():
    """每个用例独立的内存库 + 独立集合。"""
    Db.close()
    Db.set_config({"database": ":memory:"})
    return Db.collection("docs")


class TestCreateAndCrud(unittest.TestCase):

    def setUp(self):
        self.docs = _fresh()

    def test_ensure_table_idempotent(self):
        docs2 = Db.collection("docs")  # 同表重复打开
        uid = self.docs.insert({"name": "a"})
        self.assertEqual(docs2.find(uid)["name"], "a")

    def test_insert_returns_id(self):
        uid = self.docs.insert({"name": "张三", "age": 25})
        self.assertIsInstance(uid, int)
        self.assertGreater(uid, 0)

    def test_find_by_pk(self):
        uid = self.docs.insert({"name": "张三", "age": 25})
        doc = self.docs.find(uid)
        self.assertEqual(doc["_id"], uid)
        self.assertEqual(doc["name"], "张三")
        self.assertEqual(doc["age"], 25)

    def test_find_missing_returns_none(self):
        self.assertIsNone(self.docs.find(9999))

    def test_insert_with_explicit_id(self):
        self.docs.insert({"_id": 100, "name": "a"})
        self.assertEqual(self.docs.find(100)["name"], "a")

    def test_doc_id_not_duplicated_from_data(self):
        uid = self.docs.insert({"_id": 7, "name": "a"})
        doc = self.docs.find(uid)
        self.assertEqual(doc["_id"], 7)

    def test_insert_all(self):
        n = self.docs.insert_all([{"a": 1}, {"a": 2}, {"a": 3}])
        self.assertEqual(n, 3)
        self.assertEqual(self.docs.count(), 3)

    def test_insert_all_mixed_id_rejected(self):
        with self.assertRaises(QueryError):
            self.docs.insert_all([{"a": 1}, {"_id": 5, "a": 2}])

    def test_insert_all_empty(self):
        self.assertEqual(self.docs.insert_all([]), 0)

    def test_nested_doc_roundtrip(self):
        doc = {"profile": {"city": "北京", "geo": {"lat": 39.9}},
               "tags": ["vip", "beta"], "active": True, "score": None}
        uid = self.docs.insert(doc)
        back = self.docs.find(uid)
        self.assertEqual(back["profile"]["city"], "北京")
        self.assertEqual(back["profile"]["geo"]["lat"], 39.9)
        self.assertEqual(back["tags"], ["vip", "beta"])
        self.assertIs(back["active"], True)
        self.assertIsNone(back["score"])

    def test_update_replaces_whole_doc(self):
        uid = self.docs.insert({"name": "a", "age": 1, "extra": "x"})
        n = self.docs.where("_id", uid).update({"name": "b"})
        self.assertEqual(n, 1)
        doc = self.docs.find(uid)
        self.assertEqual(doc["name"], "b")
        self.assertNotIn("age", doc)      # 整文档替换，未提字段消失
        self.assertNotIn("extra", doc)

    def test_update_ignores_doc_id(self):
        uid = self.docs.insert({"name": "a"})
        self.docs.where("_id", uid).update({"_id": 999, "name": "b"})
        self.assertEqual(self.docs.find(uid)["name"], "b")
        self.assertIsNone(self.docs.find(999))

    def test_update_without_where_rejected(self):
        self.docs.insert({"a": 1})
        with self.assertRaises(QueryError):
            self.docs.update({"a": 2})

    def test_update_path_partial(self):
        uid = self.docs.insert({"name": "a", "age": 1,
                                "profile": {"city": "北京"}})
        n = self.docs.where("_id", uid).update_path(
            {"age": 2, "profile.city": "上海"})
        self.assertEqual(n, 1)
        doc = self.docs.find(uid)
        self.assertEqual(doc["age"], 2)
        self.assertEqual(doc["profile"]["city"], "上海")
        self.assertEqual(doc["name"], "a")     # 其余字段保留

    def test_patch_merges_and_deletes(self):
        uid = self.docs.insert({"profile": {"city": "北京", "zip": "100"},
                                "tmp": 1})
        self.docs.where("_id", uid).patch({"profile": {"city": "上海"},
                                           "tmp": None})
        doc = self.docs.find(uid)
        # RFC 7396：对象递归合并，未提及的 zip 键保留
        self.assertEqual(doc["profile"], {"city": "上海", "zip": "100"})
        self.assertNotIn("tmp", doc)                        # null 删键

    def test_delete_by_pk_and_condition(self):
        uid = self.docs.insert({"a": 1})
        self.assertEqual(self.docs.delete(uid), 1)
        self.docs.insert_all([{"a": 1}, {"a": 2}])
        self.assertEqual(self.docs.where("a", 1).delete(), 1)
        self.assertEqual(self.docs.count(), 1)

    def test_non_dict_rejected(self):
        with self.assertRaises(QueryError):
            self.docs.insert([1, 2])


class TestConditions(unittest.TestCase):

    def setUp(self):
        self.docs = _fresh()
        self.uids = self.docs.insert_all([
            {"name": "张三", "age": 25, "profile": {"city": "北京"},
             "tags": ["vip", "beta"], "active": True},
            {"name": "李四", "age": 17, "profile": {"city": "上海"},
             "tags": ["beta"], "active": False},
            {"name": "王五", "age": 30, "profile": {"city": "北京"},
             "tags": [], "score": 3.5},
        ])

    def _names(self, *chain):
        return sorted(d["name"] for d in chain[0].select())

    def test_eq_shorthand(self):
        got = self._names(self.docs.where("profile.city", "北京"))
        self.assertEqual(got, ["张三", "王五"])

    def test_comparison_ops(self):
        got = self._names(self.docs.where("age", ">=", 25))
        self.assertEqual(got, ["张三", "王五"])
        got = self._names(self.docs.where("age", "<", 18))
        self.assertEqual(got, ["李四"])

    def test_in_op(self):
        got = self._names(self.docs.where("age", "in", [17, 30]))
        self.assertEqual(got, ["李四", "王五"])

    def test_list_value_means_in(self):
        got = self._names(self.docs.where("age", [17, 30]))
        self.assertEqual(got, ["李四", "王五"])

    def test_between_and_like(self):
        got = self._names(self.docs.where("age", "between", [18, 30]))
        self.assertEqual(got, ["张三", "王五"])
        got = self._names(self.docs.where("name", "like", "张%"))
        self.assertEqual(got, ["张三"])

    def test_null_semantics(self):
        self.docs.insert({"name": "赵六", "age": None})
        got = self._names(self.docs.where("age", None))   # null 或缺失
        self.assertEqual(got, ["赵六"])
        got = self._names(self.docs.where("age", "exists"))
        self.assertEqual(got, ["张三", "李四", "王五", "赵六"])
        got = self._names(self.docs.where("score", "not exists"))
        self.assertEqual(got, ["张三", "李四", "赵六"])

    def test_contains_array(self):
        got = self._names(self.docs.where("tags", "contains", "vip"))
        self.assertEqual(got, ["张三"])

    def test_bool_comparison(self):
        got = self._names(self.docs.where("active", True))
        self.assertEqual(got, ["张三"])

    def test_where_dict_form(self):
        got = self._names(self.docs.where(
            {"profile.city": "北京", "age": 30}))
        self.assertEqual(got, ["王五"])

    def test_where_or(self):
        got = self._names(self.docs.where("age", 25)
                          .where_or("age", 17))
        self.assertEqual(got, ["张三", "李四"])

    def test_where_length(self):
        got = self._names(self.docs.where_length("tags", ">=", 2))
        self.assertEqual(got, ["张三"])

    def test_array_index_path(self):
        got = self._names(self.docs.where("tags[0]", "=", "vip"))
        self.assertEqual(got, ["张三"])

    def test_bad_operator_rejected(self):
        with self.assertRaises(QueryError):
            self.docs.where("age", "$gte", 18)

    def test_bad_path_rejected(self):
        with self.assertRaises(InvalidArgumentException):
            self.docs.where("age; DROP TABLE docs", 1)

    def test_missing_value_rejected(self):
        with self.assertRaises(QueryError):
            self.docs.where("age")


class TestReadHelpers(unittest.TestCase):

    def setUp(self):
        self.docs = _fresh()
        self.docs.insert_all([
            {"name": "a", "age": 3, "profile": {"city": "北京"}},
            {"name": "b", "age": 1, "profile": {"city": "上海"}},
            {"name": "c", "age": 2, "profile": {"city": "北京"}},
        ])

    def test_order_and_limit(self):
        got = [d["name"] for d in
               self.docs.order("age", "desc").limit(2).select()]
        self.assertEqual(got, ["a", "c"])

    def test_order_promoted_and_page(self):
        self.docs.promote("age")
        # ages: a=3, b=1, c=2 -> 升序 [b, c, a]，第 2 页（每页 2 条）= [a]
        got = [d["name"] for d in
               self.docs.order("age").page(2, 2).select()]
        self.assertEqual(got, ["a"])

    def test_value_and_column(self):
        self.assertEqual(self.docs.where("name", "b").value("age"), 1)
        self.assertEqual(self.docs.where("name", "zzz").value("age", -1), -1)
        self.assertEqual(self.docs.value("profile.city"), "北京")
        self.assertEqual(self.docs.column("profile.city"),
                         ["北京", "上海", "北京"])

    def test_distinct(self):
        self.assertEqual(sorted(self.docs.distinct("profile.city")),
                         ["上海", "北京"])

    def test_group_counts(self):
        rows = self.docs.group_counts("profile.city")
        self.assertEqual(rows[0], {"value": "北京", "count": 2})
        self.assertEqual(rows[1], {"value": "上海", "count": 1})

    def test_select_returns_collection_wrapper(self):
        result = self.docs.select()
        self.assertEqual(result.first()["name"], "a")
        self.assertEqual(result.column("name"), ["a", "b", "c"])


class TestIndexAndPromote(unittest.TestCase):

    def setUp(self):
        self.docs = _fresh()
        self.docs.insert_all([
            {"age": 25, "profile": {"city": "北京"}},
            {"age": 17, "profile": {"city": "上海"}},
        ])

    def _indexes(self):
        return {r["name"] for r in
                self.docs.conn.query(
                    "SELECT name FROM sqlite_master WHERE type='index'")}

    def test_expression_index(self):
        self.docs.ensure_index("age")
        self.assertIn("idx_docs_age", self._indexes())
        self.docs.ensure_index("age")  # 幂等

    def test_expression_index_nested_custom_name(self):
        self.docs.ensure_index("profile.city", name="city_idx")
        self.assertIn("city_idx", self._indexes())

    def test_unique_index_allows_missing(self):
        self.docs.ensure_index("uid", unique=True)
        self.docs.insert({"uid": "x"})
        self.docs.insert({"name": "no uid"})   # 缺失路径互不冲突
        self.docs.insert({"name": "no uid2"})

    def test_promote_generated_column(self):
        self.docs.promote("age")
        cols = self.docs._columns()
        self.assertIn("age", cols)
        # 提升列自动反映文档数据
        got = sorted(d["age"] for d in self.docs.where("age", ">", 0).select())
        self.assertEqual(got, [17, 25])

    def test_promote_nested_uses_joined_name(self):
        self.docs.promote("profile.city")
        self.assertIn("profile_city", self.docs._columns())
        got = sorted(d["profile"]["city"]
                     for d in self.docs.where("profile.city", "=", "北京")
                     .select())
        self.assertEqual(got, ["北京"])

    def test_promoted_condition_switches_to_column(self):
        self.docs.promote("age")
        self.assertEqual(self.docs._resolve_field("age"), ("col", "age"))
        # 提升前 old path 语义不变
        got = sorted(d["age"] for d in self.docs.where("age", ">=", 18)
                     .select())
        self.assertEqual(got, [25])

    def test_promote_rejects_bad_input(self):
        with self.assertRaises(InvalidArgumentException):
            self.docs.promote("$")                 # 根路径
        with self.assertRaises(InvalidArgumentException):
            self.docs.promote("tags[0]")           # 数组索引
        with self.assertRaises(InvalidArgumentException):
            self.docs.promote("data")              # 保留列
        self.docs.promote("age")
        with self.assertRaises(QueryError):
            self.docs.promote("age")               # 重复提升

    def test_promote_with_type_and_custom_column(self):
        self.docs.promote("age", sql_type="INTEGER", column="user_age")
        self.assertIn("user_age", self.docs._columns())
        self.assertEqual(self.docs._resolve_field("age"), ("col", "user_age"))


class TestSchemaValidation(unittest.TestCase):

    def setUp(self):
        Db.close()
        Db.set_config({"database": ":memory:"})

    def test_off_by_default(self):
        docs = Db.collection("docs")
        docs.schema({"age": int})
        uid = docs.insert({"age": "not-an-int"})   # 默认关闭：不校验
        self.assertEqual(docs.find(uid)["age"], "not-an-int")

    def test_on_rejects_wrong_type(self):
        Db.set_config({"database": ":memory:", "collection_schema_mode": True})
        docs = Db.collection("docs")
        docs.schema({"age": int, "name": str, "tags": list})
        with self.assertRaises(InvalidArgumentException):
            docs.insert({"age": "17"})
        with self.assertRaises(InvalidArgumentException):
            docs.insert({"name": 123})
        with self.assertRaises(InvalidArgumentException):
            docs.insert({"tags": "vip"})

    def test_on_accepts_correct_and_missing(self):
        Db.set_config({"database": ":memory:", "collection_schema_mode": True})
        docs = Db.collection("docs")
        docs.schema({"age": int, "score": float, "flag": bool})
        uid = docs.insert({"age": 17, "score": 1})     # float 宽容 int
        docs.insert({"flag": True})                     # 缺失不校验
        docs.insert({"age": None})                      # None 放行
        self.assertEqual(docs.find(uid)["score"], 1)

    def test_bool_int_mutually_exclusive(self):
        Db.set_config({"database": ":memory:", "collection_schema_mode": True})
        docs = Db.collection("docs")
        docs.schema({"age": int, "flag": bool})
        with self.assertRaises(InvalidArgumentException):
            docs.insert({"age": True})
        with self.assertRaises(InvalidArgumentException):
            docs.insert({"flag": 1})

    def test_on_validates_update_and_paths(self):
        Db.set_config({"database": ":memory:", "collection_schema_mode": True})
        docs = Db.collection("docs")
        docs.schema({"age": int})
        uid = docs.insert({"age": 1})
        with self.assertRaises(InvalidArgumentException):
            docs.where("_id", uid).update({"age": "x"})
        with self.assertRaises(InvalidArgumentException):
            docs.where("_id", uid).update_path({"age": "x"})

    def test_bad_schema_type_rejected(self):
        docs = Db.collection("docs")
        with self.assertRaises(InvalidArgumentException):
            docs.schema({"age": "int"})    # 必须是类型对象
        with self.assertRaises(InvalidArgumentException):
            docs.schema({"a b": int})      # 非法路径


class TestEdges(unittest.TestCase):

    def setUp(self):
        self.docs = _fresh()

    def test_bad_collection_name(self):
        for bad in ("1abc", "a-b", "a b", "", "  "):
            with self.assertRaises(InvalidArgumentException):
                Db.collection(bad)

    def test_bad_index_name(self):
        with self.assertRaises(InvalidArgumentException):
            self.docs.ensure_index("a", name="bad name")

    def test_update_path_empty_rejected(self):
        with self.assertRaises(QueryError):
            self.docs.where("_id", 1).update_path({})

    def test_patch_empty_rejected(self):
        with self.assertRaises(QueryError):
            self.docs.where("_id", 1).patch({})

    def test_extract_missing_and_array(self):
        self.docs.insert({"tags": ["a", "b"], "profile": {"city": "北京"}})
        self.assertEqual(self.docs.value("tags[1]"), "b")
        self.assertIsNone(self.docs.value("tags[9]"))
        self.assertIsNone(self.docs.value("nope.deep"))

    def test_doc_id_key_wins_over_data(self):
        self.docs.insert({"_id": 5, "data": "doc-key-data"})
        doc = self.docs.find(5)
        self.assertEqual(doc["_id"], 5)
        self.assertEqual(doc["data"], "doc-key-data")  # 文档键 data 正常存取

    def test_table_name_isolation(self):
        other = Db.collection("other")
        self.docs.insert({"a": 1})
        other.insert({"b": 2})
        self.assertEqual(self.docs.count(), 1)
        self.assertEqual(other.count(), 1)
        self.assertEqual(self.docs.table, "docs")
        self.assertEqual(other.table, "other")


if __name__ == "__main__":
    unittest.main()
