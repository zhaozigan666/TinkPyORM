"""DocumentCollection 全覆盖测试（P0/P1，计划见对话）。

聚焦 test_collection.py 未覆盖的五类盲区：

P0（正确性脆弱点）
- 语义不变性：同一查询在 promote 前后结果集必须完全一致
- 缓存失效：Collection 写入须使同表 Query 缓存失效
- 注入面：字段/路径/操作符/值均经白名单与参数绑定防护
- 并发：多线程自增无丢失更新、批量插入无损坏

P1（真实使用必经路径）
- 持久化文件库：关闭重开后 promote 注册表与索引重建、数据 intact
- 事务：写入回滚/提交语义
- schema 校验矩阵：覆盖 insert_all / patch / 嵌套路径
- DDL 脏环境：多表共存时提升列解析不串表、多提升列全识别

P2 的性能基准见 scripts/perf_collection.py（独立脚本，不进 unittest）。
"""
import os
import tempfile
import threading
import unittest

from tinkpyorm import Db, DocumentCollection, raw
from tinkpyorm.exceptions import (
    InvalidArgumentException, QueryError, TransactionError,
)


def _fresh():
    """每个用例独立的内存库 + 独立集合。"""
    Db.close()
    Db.set_config({"database": ":memory:"})
    return Db.collection("docs")


def _ids(docs, chain):
    """执行链式查询，返回结果文档的 _id 升序列表（类型无关比较用）。"""
    return sorted(d["_id"] for d in chain.select())


class TestSemanticInvariance(unittest.TestCase):
    """promote 只应改变"走列加速"，不能改变查询结果。

    不变性断言：同一查询在【未提升】与【已提升】两个同数据集合上，
    返回的 _id 集合必须完全一致（值类型差异不在此断言范围）。
    """

    def _twins(self, rows):
        """建两个同数据集合，promote 第二个的 age 与 profile.city。"""
        Db.close()
        Db.set_config({"database": ":memory:"})
        off = Db.collection("docs_off")
        on = Db.collection("docs_on")
        for r in rows:
            off.insert(dict(r))
            on.insert(dict(r))
        on.promote("age")
        on.promote("profile.city")
        return off, on

    def test_invariance_filter_eq(self):
        off, on = self._twins([
            {"name": "a", "age": 25, "profile": {"city": "北京"}},
            {"name": "b", "age": 17, "profile": {"city": "上海"}},
            {"name": "c", "age": 30, "profile": {"city": "北京"}},
        ])
        # 字符串相等条件（两路都走 json_extract 文本比较，结果稳定）
        q = lambda d: d.where("profile.city", "=", "北京")
        self.assertEqual(_ids(off, q(off)), _ids(on, q(on)))

    def test_invariance_filter_numeric_eq(self):
        off, on = self._twins([
            {"age": 25}, {"age": 17}, {"age": 30},
        ])
        # 精确相等（数值/文本相等都成立，不依赖类型亲和）
        q = lambda d: d.where("age", 25)
        self.assertEqual(_ids(off, q(off)), _ids(on, q(on)))

    def test_invariance_order_limit(self):
        off, on = self._twins([
            {"name": "a", "age": 3},
            {"name": "b", "age": 1},
            {"name": "c", "age": 2},
        ])
        q = lambda d: d.order("age", "desc").limit(2)
        self.assertEqual(_ids(off, q(off)), _ids(on, q(on)))

    def test_invariance_distinct_group(self):
        off, on = self._twins([
            {"profile": {"city": "北京"}},
            {"profile": {"city": "上海"}},
            {"profile": {"city": "北京"}},
        ])
        # 提升列 path 与 json_extract 路径两路，聚合分组结果一致
        self.assertEqual(
            off.group_counts("profile.city"),
            on.group_counts("profile.city"))
        self.assertEqual(
            sorted(off.distinct("profile.city")),
            sorted(on.distinct("profile.city")))

    def test_schema_mode_does_not_change_query_semantics(self):
        """collection_schema_mode 开启只影响写入校验，不改变查询语义。"""
        rows = [{"age": 25, "name": "a"}, {"age": 17, "name": "b"}]
        Db.close()
        Db.set_config({"database": ":memory:", "collection_schema_mode": False})
        off = Db.collection("docs")
        Db.close()
        Db.set_config({"database": ":memory:", "collection_schema_mode": True})
        on = Db.collection("docs")
        on.schema({"age": int, "name": str})
        for r in rows:
            off.insert(dict(r))
            on.insert(dict(r))  # on 模式下合法
        q = lambda d: d.where("age", ">=", 18)
        self.assertEqual(_ids(off, q(off)), _ids(on, q(on)))


class TestCacheInvalidation(unittest.TestCase):

    def setUp(self):
        self.docs = _fresh()

    def test_collection_write_invalidates_query_cache(self):
        """Collection 写入后，同表显式 cache 的 Query 必须反映新数据。"""
        self.docs.insert({"a": 1})
        first = [d["_id"] for d in
                 Db.table("docs").cache(60).select()]
        self.assertEqual(len(first), 1)
        self.docs.insert({"a": 2})          # Collection 写入
        second = [d["_id"] for d in
                  Db.table("docs").cache(60).select()]
        self.assertEqual(len(second), 2)    # 缓存被失效，非陈旧快照

    def test_update_invalidates_then_select_reflects(self):
        uid = self.docs.insert({"a": 1})
        self.docs.where("_id", uid).update({"a": 9})
        self.assertEqual(self.docs.where("a", 9).count(), 1)
        self.assertEqual(self.docs.where("a", 1).count(), 0)


class TestInjectionSurface(unittest.TestCase):
    """所有外部字符串经白名单校验或参数绑定，无法逃逸出 SQL 结构。"""

    def setUp(self):
        self.docs = _fresh()

    def test_value_with_quote_is_parameterized(self):
        self.docs.insert({"name": "O'Brien"})
        self.assertEqual(self.docs.where("name", "O'Brien").count(), 1)

    def test_malicious_value_cannot_break_out(self):
        payload = "'); DROP TABLE docs; --"
        self.docs.insert({"name": payload})
        # 表仍在，数据 intact（参数绑定，payload 只是普通文本）
        self.assertEqual(self.docs.count(), 1)
        self.assertEqual(self.docs.where("name", payload).count(), 1)
        tables = {r["name"] for r in
                  self.docs.conn.query(
                      "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn("docs", tables)

    def test_op_injection_rejected(self):
        with self.assertRaises(QueryError):
            self.docs.where("age", "1; DROP TABLE docs", 5)

    def test_path_quote_rejected(self):
        with self.assertRaises(InvalidArgumentException):
            self.docs.where("a'b", 1)

    def test_collection_name_injection_rejected(self):
        for bad in ("docs; DROP", "docs`x`", "docs'", "docs\"y\""):
            with self.assertRaises(InvalidArgumentException):
                Db.collection(bad)


class TestConcurrency(unittest.TestCase):
    """多线程下连接级 RLock + 单语句原子写入的正确性。"""

    def setUp(self):
        self.docs = _fresh()

    def test_concurrent_insert_unique_ids(self):
        n = 20
        markers = {}
        errors = []

        def worker(i):
            try:
                uid = self.docs.insert({"i": i, "marker": f"m{i}"})
                markers[i] = uid
            except Exception as e:  # pragma: no cover - 不应触发
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,))
                   for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        self.assertEqual(self.docs.count(), n)
        self.assertEqual(len(set(markers.values())), n)  # _id 全唯一
        got = {d["marker"] for d in self.docs.select()}
        self.assertEqual(got, {f"m{i}" for i in range(n)})

    def test_concurrent_distinct_updates_no_corruption(self):
        n = 15
        uids = [self.docs.insert({"counter": -1}) for _ in range(n)]

        def worker(i):
            self.docs.where("_id", uids[i]).update_path({"counter": i})

        threads = [threading.Thread(target=worker, args=(i,))
                   for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        counters = sorted(d["counter"] for d in self.docs.select())
        self.assertEqual(counters, list(range(n)))  # 每文档各写各的，无串扰

    def test_atomic_increment_no_lost_update(self):
        """N 个线程各发一条单语句自增：SQLite 串行化写，最终精确 +N。

        证明"单语句路径写入"是原子的——读改写交给我们写的 SQL 表达式，
        不在应用层做 read-modify-write（对比：应用层自增会丢更新）。
        """
        n = 30
        docs = Db.collection("docs")
        uid = docs.insert({"counter": 0})  # 真正建表并插入首行
        docs = Db.collection("docs")

        def worker():
            Db.table("docs").where("_id", uid).update_json_ops(
                "data",
                [("set", "$.counter",
                  raw("json_extract(data,'$.counter')+1"))])

        threads = [threading.Thread(target=worker) for _ in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(docs.find(uid)["counter"], n)


class TestPersistence(unittest.TestCase):
    """文件库关闭重开后，元数据（提升列、索引）与数据一致性保持。"""

    def setUp(self):
        self.path = tempfile.mktemp(suffix=".db")

    def tearDown(self):
        Db.close()
        if os.path.exists(self.path):
            os.remove(self.path)

    def _indexes(self):
        return {r["name"] for r in
                Db.get_connection().query(
                    "SELECT name FROM sqlite_master WHERE type='index'")}

    def test_promote_and_index_survive_reopen(self):
        Db.set_config({"database": self.path})
        docs = Db.collection("docs")
        docs.insert_all([
            {"age": 25, "profile": {"city": "北京"}},
            {"age": 17, "profile": {"city": "上海"}},
        ])
        docs.promote("age")
        docs.ensure_index("profile.city")
        # 关闭连接
        Db.close()

        # 重新打开同一文件
        Db.set_config({"database": self.path})
        docs2 = Db.collection("docs")
        # 提升列注册表从 DDL 重建
        self.assertEqual(docs2._resolve_field("age"), ("col", "age"))
        self.assertIn("idx_docs_profile_city", self._indexes())
        # 数据 intact
        self.assertEqual(docs2.count(), 2)
        self.assertEqual(
            sorted(d["age"] for d in docs2.where("age", ">", 0).select()),
            [17, 25])


class TestTransaction(unittest.TestCase):

    def setUp(self):
        self.docs = _fresh()

    def test_rollback_discards_collection_write(self):
        self.docs.insert({"a": 1})          # 事务外：建表并落库
        conn = Db.get_connection()
        conn.begin()
        self.docs.insert({"a": 2})
        self.assertEqual(self.docs.count(), 2)
        conn.rollback()                     # 仅回滚事务内写入
        self.assertEqual(self.docs.count(), 1)
        self.assertEqual(self.docs.where("a", 2).count(), 0)

    def test_commit_keeps_collection_write(self):
        self.docs.insert({"a": 1})          # 事务外：建表并落库
        conn = Db.get_connection()
        conn.begin()
        self.docs.insert({"a": 2})
        conn.commit()
        self.assertEqual(self.docs.count(), 2)

    def test_context_manager_rollback_on_exception(self):
        self.docs.insert({"a": 1})          # 基线已提交
        try:
            with Db.transaction():
                self.docs.insert({"a": 99})
                raise RuntimeError("boom")
        except RuntimeError:
            pass
        # 事务内写入被回滚，基线保留
        self.assertEqual(self.docs.count(), 1)
        self.assertEqual(self.docs.where("a", 99).count(), 0)


class TestSchemaMatrix(unittest.TestCase):
    """schema 校验须覆盖所有写入入口（insert/insert_all/update/
    update_path/patch），且嵌套路径也校验。"""

    def _on(self):
        Db.close()
        Db.set_config({"database": ":memory:",
                       "collection_schema_mode": True})
        docs = Db.collection("docs")
        docs.schema({"age": int, "name": str, "profile.city": str})
        return docs

    def test_on_validates_insert_all(self):
        docs = self._on()
        with self.assertRaises(InvalidArgumentException):
            docs.insert_all([{"age": 1}, {"age": "x"}])
        uid = docs.insert_all([{"age": 1}, {"name": "a"}])
        self.assertEqual(uid, 2)

    def test_on_validates_patch(self):
        docs = self._on()
        uid = docs.insert({"age": 1, "name": "a",
                           "profile": {"city": "北京"}})
        with self.assertRaises(InvalidArgumentException):
            docs.where("_id", uid).patch({"age": "bad"})
        with self.assertRaises(InvalidArgumentException):
            docs.where("_id", uid).patch({"name": 123})
        # patch 删除键 / 嵌套对象合法
        docs.where("_id", uid).patch({"profile": {"city": "上海"},
                                      "tmp": None})
        self.assertEqual(docs.find(uid)["profile"]["city"], "上海")
        self.assertNotIn("tmp", docs.find(uid))

    def test_nested_path_type_check(self):
        docs = self._on()
        with self.assertRaises(InvalidArgumentException):
            docs.insert({"profile": {"city": 123}})
        uid = docs.insert({"profile": {"city": "ok"}})
        self.assertEqual(docs.find(uid)["profile"]["city"], "ok")

    def test_off_accepts_anything(self):
        Db.close()
        Db.set_config({"database": ":memory:",
                       "collection_schema_mode": False})
        docs = Db.collection("docs")
        docs.schema({"age": int})
        uid = docs.insert({"age": "whatever"})
        self.assertEqual(docs.find(uid)["age"], "whatever")


class TestDdlDirtyEnv(unittest.TestCase):
    """多表共存 / 多个提升列时，解析不串表、不漏识别。"""

    def setUp(self):
        self.docs = _fresh()
        self.other = Db.collection("other")

    def test_promoted_parse_ignores_other_tables(self):
        self.other.insert({"x": 1})
        self.other.promote("x")          # other 表有提升列
        self.docs.insert({"age": 1})
        self.docs.promote("age")
        # docs 只识别自己的提升列
        self.assertEqual(self.docs._promoted(), {"$.age": "age"})
        # other 只识别自己的
        self.assertEqual(self.other._promoted(), {"$.x": "x"})

    def test_multiple_promotions_both_recognized(self):
        self.docs.insert({"age": 1, "profile": {"city": "北京"}})
        self.docs.promote("age")
        self.docs.promote("profile.city")
        pm = self.docs._promoted()
        self.assertEqual(pm.get("$.age"), "age")
        self.assertEqual(pm.get("$.profile.city"), "profile_city")
        self.assertEqual(self.docs._resolve_field("profile.city"),
                         ("col", "profile_city"))


if __name__ == "__main__":
    unittest.main()
