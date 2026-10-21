# -*- coding: utf-8 -*-
"""MongoDB **真实服务端**集成测试（v0.12.0）。

与 ``test_mongo.py``（离线假客户端）互补：本文件跑真实 pymongo + 真实
MongoDB 服务端，覆盖离线测不出来的路径——异常翻译、写关注、索引命中、
游标分页、聚合与并发。

运行方式（凭据**不入库**，只从环境变量读）::

    TINKPYORM_MONGO_TEST_URI='mongodb://user:pwd@host:27017/?authSource=admin' \\
        python test_mongo_live.py

未设置该环境变量（或 pymongo 未安装、服务端不可达）时**整份跳过**，
因此可安全地并入常规测试流程。

测试库默认 ``tinkpyorm_it``，可用 ``TINKPYORM_MONGO_TEST_DB`` 覆盖；
跑完会删除本文件创建的集合，不碰库内其它数据。
"""
from __future__ import annotations

import os
import random
import unittest

ENV_URI = "TINKPYORM_MONGO_TEST_URI"
ENV_DB = "TINKPYORM_MONGO_TEST_DB"
ENV_BIG = "TINKPYORM_MONGO_TEST_DOCS"

#: 大数据量用例的默认条数（可调小以加速本地验证）
BIG_N = int(os.environ.get(ENV_BIG, "20000"))

#: 本文件创建的集合（跑完删除，避免污染）
_OWN_COLLECTIONS = ("it_users", "it_bulk", "it_big", "it_tx", "it_wc",
                    "it_lookup_orders", "it_lookup_users")

_SKIP_REASON = None
_Db = None
try:  # pragma: no cover - 取决于环境
    from tinkpyorm import Db, UnsupportedOperation
    from tinkpyorm.exceptions import (
        ConnectionFailedError, DuplicateKeyError, InvalidArgumentException,
        OrmError,
    )
    if not os.environ.get(ENV_URI):
        _SKIP_REASON = f"未设置环境变量 {ENV_URI}，跳过真实服务端集成测试"
    else:
        __import__("pymongo")   # 依赖探测：缺 pymongo 时整份跳过
        _Db = Db
except ImportError as exc:  # pragma: no cover
    _SKIP_REASON = f"依赖缺失（{exc}），跳过真实服务端集成测试"


def _connect():
    """切换到测试库并返回连接（每个用例独立连接，避免状态互相污染）。"""
    _Db.close()
    return _Db.config({
        "type": "mongodb",
        "database": os.environ.get(ENV_DB, "tinkpyorm_it"),
        "options": {
            "uri": os.environ[ENV_URI],
            "server_selection_timeout_ms": 8000,
            "connect_timeout_ms": 8000,
        },
    })


@unittest.skipIf(_SKIP_REASON is not None, _SKIP_REASON or "")
class LiveBase(unittest.TestCase):
    """公共夹具：每例重新连接，跑完清理自建集合。"""

    @classmethod
    def setUpClass(cls):
        cls.conn = _connect()
        cls.driver = cls.conn.driver
        try:
            cls.driver.detect_version()
        except OrmError as exc:  # 服务端不可达时整类跳过
            raise unittest.SkipTest(f"服务端不可达：{exc}")

    def setUp(self):
        # 每例重新连回正常配置：避免某例切换到坏 URI 后污染后续用例
        self.conn = _connect()
        self.driver = self.conn.driver
        self.users = _Db.mongo("it_users")
        self.users._exec("delete_many", {})

    @classmethod
    def tearDownClass(cls):
        try:
            db = cls.driver.database()
            for name in _OWN_COLLECTIONS:
                try:
                    db[name].drop()
                except Exception:
                    pass
        except Exception:
            pass


# ---------------------------------------------------------------------- #
# 1. 连接与版本
# ---------------------------------------------------------------------- #
class TestLiveConnection(LiveBase):

    def test_ping(self):
        self.assertTrue(self.driver.ping())

    def test_version_is_exact_when_authenticated(self):
        """已认证时必须拿到**精确版本**，而不是 wire 下界。

        回归：早期实现只用 ``maxWireVersion`` 反查，8.x 一律得到 8.0.0，
        与真实版本（如 8.3.13）不符。
        """
        version = self.driver.detect_version()
        info = self.driver.raw_connection.server_info()
        expected = info.get("version", "")
        self.assertTrue(str(version) in expected or expected.startswith(str(version)),
                        f"探测 {version} 与真实 {expected} 不符")

    def test_topology_detected(self):
        self.assertIn(self.driver.topology,
                      ("replica_set", "sharded", "standalone"))

    def test_transactions_requires_replica_set(self):
        """单节点部署即使版本够也不能报告支持事务（拓扑门禁）。"""
        if self.driver.topology == "standalone":
            self.assertFalse(self.driver.supports_feature("transactions"))
            with self.assertRaises(UnsupportedOperation):
                self.driver.require_feature("transactions")
        else:
            self.assertTrue(self.driver.supports_feature("transactions"))

    def test_dialect_info(self):
        info = self.driver.dialect_info()
        self.assertEqual(info["driver"], "mongodb")
        self.assertIn("topology", info)
        self.assertIn("features", info)


# ---------------------------------------------------------------------- #
# 2. CRUD
# ---------------------------------------------------------------------- #
class TestLiveCrud(LiveBase):

    def test_insert_and_find_by_id(self):
        uid = self.users.insert({"name": "张三", "age": 25})
        doc = self.users.find_by_id(uid)
        self.assertEqual(doc["name"], "张三")
        self.assertEqual(doc["_id"], uid)

    def test_insert_many_and_count(self):
        n = self.users.insert_many([{"i": i} for i in range(50)])
        self.assertEqual(n, 50)
        self.assertEqual(self.users.count(), 50)

    def test_query_with_operators(self):
        self.users.insert_many([
            {"name": "a", "age": 20, "city": "北京"},
            {"name": "b", "age": 30, "city": "上海"},
            {"name": "c", "age": 40, "city": "北京"},
        ])
        self.assertEqual(len(self.users.where("age", ">=", 30).select()), 2)
        self.assertEqual(len(self.users.where("age", "between", [25, 35]).select()), 1)
        self.assertEqual(len(self.users.where("city", "in", ["北京"]).select()), 2)
        self.assertEqual(len(self.users.where("city", "北京").select()), 2)
        self.assertEqual(len(self.users.where("missing", "exists").select()), 0)

    def test_sort_limit_projection(self):
        self.users.insert_many([{"name": f"u{i}", "age": i} for i in range(10)])
        rows = self.users.order("age", "desc").limit(3).select()
        self.assertEqual([r["age"] for r in rows], [9, 8, 7])
        row = self.users.where("name", "u1").field("name").select()[0]
        self.assertIn("name", row)
        self.assertNotIn("age", row)

    def test_update_and_update_many(self):
        uid = self.users.insert({"name": "a", "vip": False})
        self.assertEqual(self.users.where("_id", uid).update({"vip": True}), 1)
        self.assertTrue(self.users.find_by_id(uid)["vip"])
        self.users.insert_many([{"vip": False}, {"vip": False}])
        self.assertEqual(self.users.where("vip", False).update_many({"vip": True}), 2)

    def test_update_raw_operator(self):
        uid = self.users.insert({"name": "a", "n": 1})
        self.users.where("_id", uid).update_raw({"$inc": {"n": 5}})
        self.assertEqual(self.users.find_by_id(uid)["n"], 6)

    def test_replace_keeps_id(self):
        uid = self.users.insert({"name": "a", "age": 1})
        self.users.where("_id", uid).replace({"name": "b"})
        doc = self.users.find_by_id(uid)
        self.assertEqual(doc["name"], "b")
        self.assertEqual(doc["_id"], uid)

    def test_delete_and_delete_many(self):
        uid = self.users.insert({"name": "a"})
        self.assertEqual(self.users.delete(uid), 1)
        self.users.insert_many([{"g": 1}, {"g": 1}, {"g": 2}])
        self.assertEqual(self.users.where("g", 1).delete_many(), 2)
        self.assertEqual(self.users.count(), 1)

    def test_drop_cleanup(self):
        self.users.insert({"a": 1})
        self.assertTrue(self.users.count() >= 1)


# ---------------------------------------------------------------------- #
# 3. 异常翻译（离线测不出的关键路径）
# ---------------------------------------------------------------------- #
class TestLiveErrors(LiveBase):

    def test_duplicate_key_translated(self):
        """唯一索引冲突必须翻译成项目异常，而不是泄漏 pymongo 异常。"""
        self.users.ensure_index("email", unique=True, name="uniq_email")
        self.users.insert({"email": "a@b.c"})
        with self.assertRaises(DuplicateKeyError):
            self.users.insert({"email": "a@b.c"})

    def test_insert_many_duplicate_translated(self):
        """insert_many 撞唯一键抛的是 BulkWriteError，语义上仍须归为重复键。

        注意：索引名必须与前一个用例一致（同字段同选项换名会触发 MongoDB 的
        IndexOptionsConflict），因此这里复用 ``uniq_email``。
        """
        self.users.ensure_index("email", unique=True, name="uniq_email")
        self.users.insert({"email": "x@y.z"})
        with self.assertRaises(DuplicateKeyError):
            self.users.insert_many_ordered([{"email": "x@y.z"}])

    def test_connection_failure_translated(self):
        """连不上必须是 ConnectionFailedError，且 ``connect()`` 快速失败。

        用**独立 Connection**（不入 ``Db`` 全局注册表），避免污染其它用例；
        同时验证 ``connect()`` 会真的握手——pymongo 的 MongoClient 本身是
        惰性连接，若不主动探测，错误会被推迟到第一次查询。
        """
        from tinkpyorm.config import Config
        from tinkpyorm.connection import Connection
        cfg = Config(type="mongodb", database="tinkpyorm_it", options={
            "uri": "mongodb://nouser:badpwd@127.0.0.1:1/?authSource=admin",
            "server_selection_timeout_ms": 600,
            "connect_timeout_ms": 600,
        })
        conn = Connection.from_config(cfg)
        with self.assertRaises(ConnectionFailedError):
            conn.driver.connect()
        self.assertFalse(conn.driver.is_connected())   # 失败后不留半成品

    def test_connect_is_not_lazy(self):
        """正常配置下 ``connect()`` 完成后应已能报出版本（说明真的握了手）。"""
        self.driver.close()
        self.driver.connect()
        self.assertIsNotNone(self.driver.server_version)
        self.assertIsNotNone(self.driver.topology)

    def test_invalid_object_id_on_24_hex_like(self):
        """24 位但非十六进制的 ``_id`` 字符串不应被静默当成 ObjectId。"""
        rows = self.users.where("_id", "z" * 24).select()
        self.assertEqual(len(rows), 0)

    def test_schema_validation(self):
        c = _Db.mongo("it_users")
        c.schema({"age": int}, required=["name"])
        with self.assertRaises(InvalidArgumentException):
            c.insert({"age": 20})
        with self.assertRaises(InvalidArgumentException):
            c.insert({"name": "x", "age": "not-int"})


# ---------------------------------------------------------------------- #
# 4. 写关注与批量写
# ---------------------------------------------------------------------- #
class TestLiveWriteConcern(LiveBase):

    def test_per_collection_write_concern_works(self):
        """回归：早期把 dict 直接传给 pymongo，触发 TypeError。

        pymongo 的 ``get_collection(write_concern=...)`` 只接受
        ``WriteConcern`` 实例，必须由本层转换。
        """
        coll = _Db.mongo("it_wc", write_concern={"w": 1})
        coll._exec("delete_many", {})
        self.assertEqual(coll.insert_many([{"i": i} for i in range(10)]), 10)
        self.assertEqual(coll.count(), 10)

    def test_majority_write_concern_works(self):
        coll = _Db.mongo("it_wc", write_concern={"w": "majority", "j": True})
        coll._exec("delete_many", {})
        self.assertEqual(coll.insert({"a": 1}) is not None, True)

    def test_unknown_write_concern_key_rejected(self):
        with self.assertRaises(InvalidArgumentException):
            _Db.mongo("it_wc", write_concern={"nope": 1}).count()

    def test_bulk_write(self):
        from pymongo import InsertOne
        coll = _Db.mongo("it_bulk")
        coll._exec("delete_many", {})
        result = coll.bulk_write([InsertOne({"k": i}) for i in range(100)])
        self.assertEqual(result["inserted"], 100)
        self.assertEqual(coll.count(), 100)


# ---------------------------------------------------------------------- #
# 5. 性能相关（索引命中、分页、大数据量）
# ---------------------------------------------------------------------- #
class TestLivePerformance(LiveBase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.big = _Db.mongo("it_big")
        cls.big._exec("delete_many", {})
        try:
            cls.big.drop_index("idx_city_age")
        except OrmError:
            pass
        random.seed(42)
        cities = ["北京", "上海", "广州", "深圳", "杭州", "成都"]
        docs = [{"name": f"u{i:06d}", "age": random.randint(18, 65),
                 "city": random.choice(cities),
                 "score": round(random.uniform(0, 100), 2)} for i in range(BIG_N)]
        for start in range(0, len(docs), 5000):
            cls.big.insert_many(docs[start:start + 5000])

    def _stats(self, explain_body):
        """取服务端执行统计（真实扫描量，而非猜测）。"""
        db = self.driver.database()
        result = db.command({"explain": explain_body,
                             "verbosity": "executionStats"})
        return result["executionStats"]

    def test_data_inserted(self):
        self.assertEqual(self.big.count(), BIG_N)

    def test_index_changes_plan_and_scan(self):
        """建索引后：计划从 COLLSCAN 变 IXSCAN，扫描量从全表降到命中数。"""
        before = self._stats({"find": "it_big", "filter": {"city": "北京"}})
        self.big.ensure_index([("city", 1), ("age", -1)], name="idx_city_age")
        after = self._stats({"find": "it_big", "filter": {"city": "北京"}})

        self.assertEqual(before["totalDocsExamined"], BIG_N)      # 全表
        self.assertLess(after["totalDocsExamined"], BIG_N)        # 只查命中
        plan_text = str(self.big.where("city", "北京").explain())
        self.assertIn("IXSCAN", plan_text)

    def test_cursor_pagination_scans_far_less_than_offset(self):
        """深分页：游标（keyset）扫描量与页码无关，偏移量随页码线性增长。"""
        depth = min(1000, BIG_N // 4)
        skip_stats = self._stats({
            "find": "it_big", "filter": {}, "sort": {"_id": 1},
            "skip": depth, "limit": 20,
        })
        last = self.big.order("_id").limit(1, depth).select()[0]["_id"]
        cursor_stats = self._stats({
            "find": "it_big", "filter": {"_id": {"$gt": last}},
            "sort": {"_id": 1}, "limit": 20,
        })
        # 两者都只"检查"少量文档，但偏移量要多走 depth 个索引键
        self.assertGreater(skip_stats["totalKeysExamined"],
                           cursor_stats["totalKeysExamined"])
        self.assertLessEqual(cursor_stats["totalDocsExamined"], 20)

    def test_cursor_pagination_returns_distinct_pages(self):
        first = self.big.order("_id").limit(20).select()
        last_id = first[-1]["_id"]
        second = self.big.order("_id").after(last_id).limit(20).select()
        self.assertEqual(len(second), 20)
        self.assertFalse({d["_id"] for d in first} & {d["_id"] for d in second})

    def test_default_limit_cap_applies(self):
        """未显式 limit 时套用默认上限，避免一次拉回整个集合。"""
        rows = self.big.select()
        self.assertLessEqual(len(rows), 1000)

    def test_aggregate_lookup(self):
        _Db.mongo("it_lookup_users").insert_many(
            [{"_id": 1, "name": "a"}, {"_id": 2, "name": "b"}])
        _Db.mongo("it_lookup_orders").insert_many(
            [{"user_id": 1, "amt": 10}, {"user_id": 1, "amt": 20},
             {"user_id": 2, "amt": 5}])
        rows = _Db.mongo("it_lookup_users").aggregate([
            {"$lookup": {"from": "it_lookup_orders", "localField": "_id",
                         "foreignField": "user_id", "as": "orders"}},
            {"$sort": {"_id": 1}},
        ])
        self.assertEqual(len(rows), 2)
        self.assertEqual(len(rows[0]["orders"]), 2)
        self.assertEqual(len(rows[1]["orders"]), 1)


# ---------------------------------------------------------------------- #
# 6. 配置解析对真实服务端的适配
# ---------------------------------------------------------------------- #
class TestLiveConfig(LiveBase):

    def test_uri_from_env_variable(self):
        """环境变量回退：不显式给 uri 时应能读到 MONGODB_URI。"""
        uri = os.environ[ENV_URI]
        os.environ["MONGODB_URI"] = uri
        try:
            _Db.close()
            conn = _Db.config({"type": "mongodb",
                               "database": os.environ.get(ENV_DB, "tinkpyorm_it")})
            settings = conn.driver._resolve_settings()
            self.assertEqual(settings["uri"], uri)
            self.assertTrue(conn.driver.ping())
        finally:
            del os.environ["MONGODB_URI"]

    def test_uri_parts_built_correctly(self):
        """分散参数 + authSource 拼装出的连接串应能真实连上。"""
        from urllib.parse import urlparse, unquote
        parsed = urlparse(os.environ[ENV_URI])
        if not parsed.username:
            self.skipTest("测试 URI 未含用户名，跳过分散参数拼装用例")
        _Db.close()
        conn = _Db.config({
            "type": "mongodb",
            "database": os.environ.get(ENV_DB, "tinkpyorm_it"),
            "host": parsed.hostname,
            "port": parsed.port,
            "user": unquote(parsed.username),
            "password": unquote(parsed.password or ""),
            "options": {"auth_source": "admin",
                        "server_selection_timeout_ms": 8000},
        })
        self.assertTrue(conn.driver.ping())


if __name__ == "__main__":
    unittest.main(verbosity=2)
