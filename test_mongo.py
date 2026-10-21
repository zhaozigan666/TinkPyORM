# -*- coding: utf-8 -*-
"""MongoDB 数据访问层测试（v0.12.0）。

全部**离线**运行：用一个内存版假 pymongo（``_FakeMongoClient``）顶替真实
客户端，覆盖过滤条件构造、输入校验、异常翻译与版本能力矩阵。

真实服务端集成测试需外部实例（需认证），见 ``wiki/30-MongoDB支持.md``。
"""
import os
import sys
import types
import unittest

from tinkpyorm import Db, UnsupportedOperation
from tinkpyorm.drivers.mongodb import (
    MONGO_ENV_KEYS, MONGO_FEATURES, MONGO_REMOVED, translate_mongo_error,
)
from tinkpyorm.exceptions import (
    ConnectionFailedError, DuplicateKeyError, InvalidArgumentException,
    OrmError, QueryError,
)
from tinkpyorm.versioning import Version, parse_version


# ---------------------------------------------------------------------- #
# 假 pymongo：按 pymongo 的公开 API 形状实现，覆盖本层用到的全部方法
# ---------------------------------------------------------------------- #
class _FakeObjectId:
    """最小的 ObjectId 替身（24 位十六进制）。"""

    __slots__ = ("_hex",)

    def __init__(self, hex_value):
        self._hex = str(hex_value).lower()

    def __eq__(self, other):
        return isinstance(other, _FakeObjectId) and other._hex == self._hex

    def __hash__(self):
        return hash(self._hex)

    def __repr__(self):
        return f"ObjectId('{self._hex}')"

    def __str__(self):
        return self._hex


class _FakeResult:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _FakeCursor:
    def __init__(self, docs):
        self._docs = list(docs)

    def __iter__(self):
        return iter(self._docs)

    def limit(self, n):
        self._docs = self._docs[:n]
        return self

    def skip(self, n):
        self._docs = self._docs[n:]
        return self

    def sort(self, key_or_list, direction=None):
        if isinstance(key_or_list, str):
            keys = [(key_or_list, direction if direction else 1)]
        else:
            keys = list(key_or_list)
        for field, direction in reversed(keys):
            self._docs.sort(key=lambda d: d.get(field),
                            reverse=direction < 0)
        return self


class _FakeCollection:
    """内存集合：实现本层用到的 pymongo 集合方法。"""

    def __init__(self, name):
        self.name = name
        self.docs = []
        self.indexes = {"_id_": {"key": [("_id", 1)]}}
        self._seq = 0

    # -- 读 -- #
    def find(self, filter=None, projection=None, sort=None, skip=None,
             limit=None):
        docs = [d for d in self.docs if self._match(d, filter or {})]
        cursor = _FakeCursor(docs)
        if sort:
            cursor.sort(sort)
        if skip:
            cursor.skip(skip)
        if limit:
            cursor.limit(limit)
        if projection:
            cursor = _FakeCursor([self._project(d, projection)
                                   for d in cursor])
        return cursor

    def count_documents(self, filter=None):
        return len([d for d in self.docs if self._match(d, filter or {})])

    def distinct(self, field):
        return list({d.get(field) for d in self.docs if field in d})

    def aggregate(self, pipeline):
        # 仅支持 $match / $sort / $limit / $skip 的最小实现
        docs = list(self.docs)
        for stage in pipeline:
            key = next(iter(stage))
            value = stage[key]
            if key == "$match":
                docs = [d for d in docs if self._match(d, value)]
            elif key == "$sort":
                _FakeCursor(docs).sort(value)
            elif key == "$limit":
                docs = docs[:value]
            elif key == "$skip":
                docs = docs[value:]
        return _FakeCursor(docs)

    # -- 写 -- #
    def insert_one(self, doc):
        payload = dict(doc)
        if "_id" not in payload:
            self._seq += 1
            payload["_id"] = _FakeObjectId("%024x" % self._seq)
        self.docs.append(payload)
        return _FakeResult(inserted_id=payload["_id"])

    def insert_many(self, docs, ordered=True):
        ids = []
        for doc in docs:
            ids.append(self.insert_one(doc).inserted_id)
        return _FakeResult(inserted_ids=ids)

    def update_one(self, filter, update, upsert=False):
        for doc in self.docs:
            if self._match(doc, filter):
                doc.update(update.get("$set", {}))
                return _FakeResult(modified_count=1, matched_count=1)
        if upsert:
            self.insert_one(update.get("$set", {}))
            return _FakeResult(modified_count=0, matched_count=0,
                               upserted_id=1)
        return _FakeResult(modified_count=0, matched_count=0)

    def update_many(self, filter, update, upsert=False):
        count = 0
        for doc in self.docs:
            if self._match(doc, filter):
                doc.update(update.get("$set", {}))
                count += 1
        return _FakeResult(modified_count=count, matched_count=count)

    def replace_one(self, filter, doc, upsert=False):
        for i, existing in enumerate(self.docs):
            if self._match(existing, filter):
                keep_id = existing.get("_id")
                self.docs[i] = dict(doc)
                self.docs[i]["_id"] = keep_id
                return _FakeResult(modified_count=1, matched_count=1)
        if upsert:
            self.insert_one(doc)
        return _FakeResult(modified_count=0, matched_count=0)

    def delete_one(self, filter):
        for i, doc in enumerate(self.docs):
            if self._match(doc, filter):
                del self.docs[i]
                return _FakeResult(deleted_count=1)
        return _FakeResult(deleted_count=0)

    def delete_many(self, filter):
        keep = [d for d in self.docs if not self._match(d, filter)]
        removed = len(self.docs) - len(keep)
        self.docs = keep
        return _FakeResult(deleted_count=removed)

    def bulk_write(self, requests):
        inserted = matched = modified = deleted = 0
        for req in requests:
            kind = type(req).__name__
            if kind == "InsertOne":
                self.insert_one(req._doc)
                inserted += 1
            elif kind == "UpdateOne":
                matched += 1
                modified += 1
            elif kind == "DeleteOne":
                deleted += 1
        return _FakeResult(inserted_count=inserted, matched_count=matched,
                           modified_count=modified, deleted_count=deleted,
                           upserted_count=0)

    # -- 索引 -- #
    def create_index(self, keys, **kwargs):
        name = kwargs.get("name") or "idx_%s_%s" % (
            self.name, "_".join(k[0] for k in keys))
        self.indexes[name] = {"key": [(k[0], k[1]) for k in keys]}
        return name

    def drop_index(self, name):
        self.indexes.pop(name, None)

    def list_indexes(self):
        return [dict(v, name=k) for k, v in self.indexes.items()]

    # -- 内部：过滤匹配 -- #
    @staticmethod
    def _match(doc, filter_doc):
        for field, expr in filter_doc.items():
            if field == "$and":
                if not all(_FakeCollection._match(doc, c) for c in expr):
                    return False
                continue
            if field == "$or":
                if not any(_FakeCollection._match(doc, c) for c in expr):
                    return False
                continue
            if field == "$not":
                if _FakeCollection._match(doc, expr):
                    return False
                continue
            value = doc.get(field)
            if isinstance(expr, dict) and any(
                    str(k).startswith("$") for k in expr):
                for op, operand in expr.items():
                    if not _FakeCollection._match_op(value, op, operand):
                        return False
                continue
            if expr is None:
                if value is not None:
                    return False
                continue
            if value != expr:
                return False
        return True

    @staticmethod
    def _match_op(value, op, operand):
        if op == "$ne":
            return value != operand
        if op == "$gt":
            return value is not None and value > operand
        if op == "$gte":
            return value is not None and value >= operand
        if op == "$lt":
            return value is not None and value < operand
        if op == "$lte":
            return value is not None and value <= operand
        if op == "$in":
            return value in operand
        if op == "$nin":
            return value not in operand
        if op == "$exists":
            return (value is not None) == bool(operand)
        if op == "$regex":
            import re
            pattern = operand.pattern if hasattr(operand, "pattern") \
                else operand
            flags = operand.flags if hasattr(operand, "flags") else 0
            return value is not None and re.search(pattern, str(value),
                                                   flags) is not None
        return True

    @staticmethod
    def _project(doc, projection):
        include = {k for k, v in projection.items() if v}
        exclude = {k for k, v in projection.items() if not v}
        if include:
            out = {k: v for k, v in doc.items() if k in include or k == "_id"}
            return out
        return {k: v for k, v in doc.items() if k not in exclude}


class _FakeDatabase:
    def __init__(self, name):
        self.name = name
        self.collections = {}

    def __getitem__(self, name):
        return self.collections.setdefault(name, _FakeCollection(name))

    def get_collection(self, name, **kwargs):
        return self[name]

    def command(self, cmd, *args, **kwargs):
        if isinstance(cmd, dict) and "explain" in cmd:
            return {"queryPlanner": {"winningPlan": {"stage": "IXSCAN"}}}
        return {"ok": 1}


class _FakeAdmin:
    def __init__(self, client):
        self._client = client

    def command(self, cmd, *args, **kwargs):
        if cmd in ("hello", "isMaster"):
            reply = {"maxWireVersion": self._client.wire_version, "ok": 1}
            if self._client.topology == "replica_set":
                reply["setName"] = "rs0"
            elif self._client.topology == "sharded":
                reply["msg"] = "isdbgrid"
            return reply
        if cmd == "ping":
            return {"ok": 1}
        return {"ok": 1}


class _FakeMongoClient:
    """假 MongoClient：记录连接参数，便于断言配置解析结果。"""

    #: 由 _install_fake_pymongo() 设置（决定 hello 命令返回的 wire 版本）
    wire_version = 21                 # 默认按 MongoDB 7.0
    #: 由 _install_fake_pymongo() 设置（replica_set / sharded / standalone）
    topology = "replica_set"
    #: 由 _install_fake_pymongo() 设置：是否提供 server_info（buildInfo）
    has_buildinfo = False
    #: buildInfo 返回的精确版本号
    server_version = "7.0.0"

    def __init__(self, uri=None, **kwargs):
        self.uri = uri
        self.kwargs = kwargs
        self.databases = {}
        self.closed = False

    def __getitem__(self, name):
        return self.databases.setdefault(name, _FakeDatabase(name))

    @property
    def admin(self):
        return _FakeAdmin(self)

    def server_info(self):
        if not self.has_buildinfo:
            raise NotImplementedError("fake: buildInfo 不可用")
        return {"version": self.server_version}

    def close(self):
        self.closed = True


def _install_fake_pymongo(wire_version=21, topology="replica_set",
                          has_buildinfo=False, server_version="7.0.0"):
    """把假 pymongo 装进 ``sys.modules``（仅本测试进程内生效）。"""
    _FakeMongoClient.wire_version = wire_version
    _FakeMongoClient.topology = topology
    _FakeMongoClient.has_buildinfo = has_buildinfo
    _FakeMongoClient.server_version = server_version
    pymongo = types.ModuleType("pymongo")
    pymongo.MongoClient = _FakeMongoClient
    pymongo.version = "4.0.0-fake"
    errors = types.ModuleType("pymongo.errors")

    class DuplicateKeyError(Exception):
        pass

    class OperationFailure(Exception):
        pass

    class ServerSelectionTimeoutError(Exception):
        pass

    class InvalidId(Exception):
        pass

    errors.DuplicateKeyError = DuplicateKeyError
    errors.OperationFailure = OperationFailure
    errors.ServerSelectionTimeoutError = ServerSelectionTimeoutError
    errors.InvalidId = InvalidId
    pymongo.errors = errors
    sys.modules["pymongo"] = pymongo
    sys.modules["pymongo.errors"] = errors

    bson = types.ModuleType("bson")
    bson.ObjectId = _FakeObjectId
    bson_errors = types.ModuleType("bson.errors")

    class InvalidIdBson(Exception):
        pass

    bson_errors.InvalidId = InvalidIdBson
    bson.errors = bson_errors
    sys.modules["bson"] = bson
    sys.modules["bson.errors"] = bson_errors
    return pymongo


def _mongo(version=None, wire_version=21, topology="replica_set",
           has_buildinfo=False, server_version="7.0.0", **options):
    """切换到 MongoDB 配置（假客户端），返回 Connection。"""
    _install_fake_pymongo(wire_version, topology=topology,
                          has_buildinfo=has_buildinfo,
                          server_version=server_version)
    Db.close()
    config = {"type": "mongodb", "database": "app",
              "options": {"uri": "mongodb://127.0.0.1:27017"}}
    if version is not None:
        config["version"] = version
    config["options"].update(options)
    return Db.config(config)


# ---------------------------------------------------------------------- #
# 1. 版本解析与能力矩阵
# ---------------------------------------------------------------------- #
class TestMongoVersion(unittest.TestCase):

    def test_default_is_4_0(self):
        self.assertEqual(parse_version(None, product="mongodb"),
                         Version(4, 0, 0))

    def test_supported_versions(self):
        for text in ("4.0", "4.2", "4.4", "5.0", "6.0", "7.0", "8.0"):
            self.assertTrue(parse_version(text, product="mongodb"))

    def test_unsupported_versions_rejected(self):
        for text in ("3.6", "4.1", "4.3", "9.0", "abc"):
            with self.assertRaises(InvalidArgumentException, msg=text):
                parse_version(text, product="mongodb")

    def test_feature_matrix_minimums(self):
        self.assertEqual(MONGO_FEATURES["transactions"], Version(4, 0, 0))
        self.assertEqual(MONGO_FEATURES["merge_stage"], Version(4, 2, 0))
        self.assertEqual(MONGO_FEATURES["union_with"], Version(4, 4, 0))
        self.assertEqual(MONGO_FEATURES["set_window_fields"], Version(5, 0, 0))
        self.assertEqual(MONGO_FEATURES["densify_stage"], Version(5, 1, 0))
        self.assertEqual(MONGO_FEATURES["documents_stage"], Version(6, 0, 0))
        self.assertEqual(MONGO_FEATURES["rank_fusion"], Version(8, 0, 0))

    def test_removed_features(self):
        self.assertEqual(MONGO_REMOVED["geo_haystack"], Version(4, 4, 0))
        self.assertEqual(MONGO_REMOVED["op_query"], Version(5, 1, 0))
        self.assertEqual(MONGO_REMOVED["mongodb_cr_auth"], Version(4, 0, 0))

    def test_supports_feature_by_detected_version(self):
        _mongo(wire_version=17)      # MongoDB 6.0
        driver = Db.get_connection().driver
        self.assertEqual(driver.detect_version(), Version(6, 0, 0))
        self.assertEqual(driver.version, Version(6, 0, 0))
        self.assertTrue(driver.supports_feature("transactions"))
        self.assertTrue(driver.supports_feature("documents_stage"))
        self.assertFalse(driver.supports_feature("rank_fusion"))

    def test_require_feature_raises_with_version(self):
        _mongo(wire_version=13)      # MongoDB 5.0
        driver = Db.get_connection().driver
        driver.detect_version()
        with self.assertRaises(UnsupportedOperation) as ctx:
            driver.require_feature("documents_stage")
        self.assertIn("documents_stage", str(ctx.exception))
        self.assertIn("5.0.0", str(ctx.exception))

    def test_unknown_feature_rejected(self):
        _mongo()
        with self.assertRaises(InvalidArgumentException):
            Db.get_connection().driver.supports_feature("no_such_feature")

    def test_declared_version_higher_than_server_rejected(self):
        _mongo(version="8.0", wire_version=13)   # 实际只有 5.0
        driver = Db.get_connection().driver
        with self.assertRaises(InvalidArgumentException) as ctx:
            driver.connect()
        self.assertIn("高于实际服务端版本", str(ctx.exception))

    def test_declared_version_lower_than_server_allowed(self):
        _mongo(version="4.2", wire_version=21)      # 实际 7.0
        driver = Db.get_connection().driver
        self.assertEqual(driver.declared_version, Version(4, 2, 0))
        self.assertEqual(driver.detect_version(), Version(7, 0, 0))
        self.assertEqual(driver.server_version, Version(7, 0, 0))

    def test_exact_version_prefers_buildinfo(self):
        """有 buildInfo 权限时必须拿精确版本，而不是 wire 下界。

        回归：真实服务端是 8.3.13，只用 maxWireVersion 反查会得到 8.0.0。
        """
        _mongo(wire_version=25, has_buildinfo=True, server_version="8.3.13")
        driver = Db.get_connection().driver
        self.assertEqual(driver.detect_version(), Version(8, 3, 13))

    def test_version_falls_back_to_wire_without_buildinfo(self):
        _mongo(wire_version=25, has_buildinfo=False)
        driver = Db.get_connection().driver
        self.assertEqual(driver.detect_version(), Version(8, 0, 0))

    def test_topology_detected_replica_set(self):
        _mongo(topology="replica_set")
        driver = Db.get_connection().driver
        driver.detect_version()
        self.assertEqual(driver.topology, "replica_set")

    def test_topology_detected_standalone(self):
        _mongo(topology="standalone")
        driver = Db.get_connection().driver
        driver.detect_version()
        self.assertEqual(driver.topology, "standalone")

    def test_topology_detected_sharded(self):
        _mongo(topology="sharded")
        driver = Db.get_connection().driver
        driver.detect_version()
        self.assertEqual(driver.topology, "sharded")

    def test_transactions_gated_by_topology(self):
        """单节点即使版本够也不能报告支持事务（版本门禁之外还有拓扑门禁）。"""
        _mongo(wire_version=25, topology="standalone")
        driver = Db.get_connection().driver
        driver.detect_version()
        self.assertFalse(driver.supports_feature("transactions"))
        with self.assertRaises(UnsupportedOperation) as ctx:
            driver.require_feature("transactions")
        self.assertIn("standalone", str(ctx.exception))

    def test_transactions_allowed_on_replica_set(self):
        _mongo(wire_version=25, topology="replica_set")
        driver = Db.get_connection().driver
        driver.detect_version()
        self.assertTrue(driver.supports_feature("transactions"))

    def test_tcpology_absent_does_not_gate(self):
        """拓扑未知时不做拓扑拦截（避免误杀），仅按版本判断。"""
        _mongo(wire_version=25)
        driver = Db.get_connection().driver
        self.assertIsNone(driver.topology)
        self.assertTrue(driver.supports_feature("transactions"))

    def test_dialect_info_includes_topology(self):
        _mongo(topology="replica_set")
        driver = Db.get_connection().driver
        driver.detect_version()
        self.assertEqual(driver.dialect_info()["topology"], "replica_set")

    def test_dialect_info(self):
        _mongo(wire_version=21)
        driver = Db.get_connection().driver
        driver.detect_version()
        info = driver.dialect_info()
        self.assertEqual(info["driver"], "mongodb")
        self.assertEqual(info["version"], "7.0.0")
        self.assertIn("features", info)


# ---------------------------------------------------------------------- #
# 2. 配置解析
# ---------------------------------------------------------------------- #
class TestMongoConfig(unittest.TestCase):

    def test_uri_option_used(self):
        _mongo()
        driver = Db.get_connection().driver
        settings = driver._resolve_settings()
        self.assertEqual(settings["uri"], "mongodb://127.0.0.1:27017")
        self.assertEqual(settings["database"], "app")

    def test_missing_database_raises_clear_error(self):
        _install_fake_pymongo()
        Db.close()
        conn = Db.config({"type": "mongodb",
                          "options": {"uri": "mongodb://127.0.0.1:27017"}})
        with self.assertRaises(InvalidArgumentException) as ctx:
            conn.driver._resolve_settings()
        self.assertIn("数据库名", str(ctx.exception))

    def test_missing_host_falls_back_to_localhost(self):
        """未给 host/uri 时回退到 127.0.0.1（与 MySQL 驱动一致）。"""
        _install_fake_pymongo()
        Db.close()
        conn = Db.config({"type": "mongodb", "database": "app"})
        settings = conn.driver._resolve_settings()
        self.assertEqual(settings["host"], "127.0.0.1")
        self.assertEqual(settings["port"], 27017)

    def test_env_variable_fallback(self):
        _install_fake_pymongo()
        Db.close()
        os.environ["MONGODB_URI"] = "mongodb://env-host:27017"
        os.environ["MONGODB_DATABASE"] = "envdb"
        try:
            conn = Db.config({"type": "mongodb"})
            settings = conn.driver._resolve_settings()
            self.assertEqual(settings["uri"], "mongodb://env-host:27017")
            self.assertEqual(settings["database"], "envdb")
        finally:
            del os.environ["MONGODB_URI"]
            del os.environ["MONGODB_DATABASE"]

    def test_explicit_config_beats_env(self):
        _install_fake_pymongo()
        os.environ["MONGODB_DATABASE"] = "envdb"
        try:
            Db.close()
            conn = Db.config({"type": "mongodb", "database": "explicit",
                              "options": {"uri": "mongodb://h:27017"}})
            settings = conn.driver._resolve_settings()
            self.assertEqual(settings["database"], "explicit")
        finally:
            del os.environ["MONGODB_DATABASE"]

    def test_config_resolver_beats_env(self):
        _install_fake_pymongo()
        os.environ["MONGODB_DATABASE"] = "envdb"
        try:
            Db.close()
            conn = Db.config({
                "type": "mongodb",
                "options": {
                    "uri": "mongodb://h:27017",
                    "config_resolver": lambda: {"database": "centerdb"},
                },
            })
            settings = conn.driver._resolve_settings()
            self.assertEqual(settings["database"], "centerdb")
        finally:
            del os.environ["MONGODB_DATABASE"]

    def test_config_resolver_must_return_dict(self):
        _install_fake_pymongo()
        Db.close()
        conn = Db.config({
            "type": "mongodb", "database": "app",
            "options": {"uri": "mongodb://h:27017",
                        "config_resolver": lambda: "not-a-dict"},
        })
        with self.assertRaises(InvalidArgumentException):
            conn.driver._resolve_settings()

    def test_config_resolver_must_be_callable(self):
        _install_fake_pymongo()
        Db.close()
        conn = Db.config({
            "type": "mongodb", "database": "app",
            "options": {"uri": "mongodb://h:27017",
                        "config_resolver": "nope"},
        })
        with self.assertRaises(InvalidArgumentException):
            conn.driver._resolve_settings()

    def test_timeout_options_mapped_to_pymongo_names(self):
        _mongo(server_selection_timeout_ms=1500, max_pool_size=50)
        settings = Db.get_connection().driver._resolve_settings()
        self.assertEqual(settings["options"]["serverSelectionTimeoutMS"], 1500)
        self.assertEqual(settings["options"]["maxPoolSize"], 50)

    def test_write_concern_mapped(self):
        _mongo(write_concern={"w": "majority", "j": True, "wtimeout": 500})
        settings = Db.get_connection().driver._resolve_settings()
        self.assertEqual(settings["options"]["w"], "majority")
        self.assertTrue(settings["options"]["j"])
        self.assertEqual(settings["options"]["wtimeoutMS"], 500)

    def test_unknown_write_concern_key_rejected(self):
        _mongo(write_concern={"bogus": 1})
        with self.assertRaises(InvalidArgumentException):
            Db.get_connection().driver._resolve_settings()

    def test_uri_built_from_parts(self):
        _install_fake_pymongo()
        Db.close()
        conn = Db.config({"type": "mongodb", "database": "app",
                          "host": "db.internal", "port": 27018,
                          "user": "u", "password": "p@ss",
                          "options": {"auth_source": "admin"}})
        uri = conn.driver._build_uri(conn.driver._resolve_settings())
        self.assertIn("db.internal:27018", uri)
        self.assertIn("authSource=admin", uri)
        self.assertNotIn("p@ss", uri)     # 已 URL 编码

    def test_env_keys_documented(self):
        self.assertIn("MONGODB_URI", MONGO_ENV_KEYS["uri"])
        self.assertIn("MONGODB_DATABASE", MONGO_ENV_KEYS["database"])


# ---------------------------------------------------------------------- #
# 3. 条件构造
# ---------------------------------------------------------------------- #
class TestMongoFilter(unittest.TestCase):

    def setUp(self):
        _mongo()

    def _users(self):
        """新建集合实例（链式状态在终端方法后才复位，故每条断言独立实例）。"""
        return Db.mongo("users")

    def test_equality(self):
        self.assertEqual(self._users().where("name", "张三")._build_filter(),
                         {"name": "张三"})

    def test_two_arg_form_is_equality(self):
        self.assertEqual(self._users().where("name", "张三")._build_filter(),
                         {"name": "张三"})

    def test_comparison_operators(self):
        cases = {
            ">": {"$gt": 18}, ">=": {"$gte": 18}, "<": {"$lt": 18},
            "<=": {"$lte": 18},
        }
        for op, expected in cases.items():
            self.assertEqual(
                self._users().where("age", op, 18)._build_filter(),
                {"age": expected}, msg=op)

    def test_not_equal(self):
        self.assertEqual(self._users().where("age", "!=", 18)._build_filter(),
                         {"age": {"$ne": 18}})

    def test_in_and_not_in(self):
        self.assertEqual(self._users().where("age", "in", [1, 2])._build_filter(),
                         {"age": {"$in": [1, 2]}})
        self.assertEqual(
            self._users().where("age", "not in", [1, 2])._build_filter(),
            {"age": {"$nin": [1, 2]}})

    def test_between(self):
        self.assertEqual(
            self._users().where("age", "between", [18, 30])._build_filter(),
            {"age": {"$gte": 18, "$lte": 30}})

    def test_null_and_exists(self):
        self.assertEqual(self._users().where("x", None)._build_filter(),
                         {"x": None})
        self.assertEqual(self._users().where("x", "exists")._build_filter(),
                         {"x": {"$exists": True}})
        self.assertEqual(
            self._users().where("x", "not exists")._build_filter(),
            {"x": {"$exists": False}})

    def test_list_value_implies_in(self):
        self.assertEqual(self._users().where("age", [1, 2])._build_filter(),
                         {"age": {"$in": [1, 2]}})

    def test_same_field_conditions_merged(self):
        filt = self._users().where("age", ">=", 18).where("age", "<=", 30) \
            ._build_filter()
        self.assertEqual(filt, {"age": {"$gte": 18, "$lte": 30}})

    def test_different_fields_combined(self):
        filt = self._users().where("age", ">=", 18).where("name", "张三") \
            ._build_filter()
        self.assertEqual(filt, {"age": {"$gte": 18}, "name": "张三"})

    def test_or_conditions_grouped(self):
        filt = self._users().where("age", ">=", 18).where_or("name", "张三") \
            ._build_filter()
        self.assertIn("$and", filt)
        self.assertIn({"$or": [{"name": "张三"}]}, filt["$and"])

    def test_where_raw_merged(self):
        filt = self._users().where("age", ">=", 18).where_raw(
            {"$expr": {"$gt": ["$balance", 100]}})._build_filter()
        self.assertIn("$expr", str(filt))

    def test_unsupported_operator_rejected(self):
        with self.assertRaises(QueryError):
            self._users().where("age", "like-ish", 1)

    def test_invalid_field_name_rejected(self):
        for bad in ("", "  ", "$where", 123):
            with self.assertRaises(InvalidArgumentException, msg=repr(bad)):
                self._users().where(bad, 1)

    def test_like_becomes_regex(self):
        expr = self._users().where("name", "like", "张%")._build_filter()
        self.assertIn("$regex", expr["name"])

    def test_in_requires_list(self):
        with self.assertRaises(QueryError):
            self._users().where("age", "in", 18)

    def test_between_requires_pair(self):
        with self.assertRaises(QueryError):
            self._users().where("age", "between", [1, 2, 3])

    def test_dict_form_shorthand(self):
        filt = self._users().where({"a": 1, "b": 2})._build_filter()
        self.assertEqual(filt, {"a": 1, "b": 2})


# ---------------------------------------------------------------------- #
# 4. 排序 / 投影 / 分页
# ---------------------------------------------------------------------- #
class TestMongoSortProjectPage(unittest.TestCase):

    def setUp(self):
        _mongo()
        self.users = Db.mongo("users")

    def test_order_ascending(self):
        self.assertEqual(self.users.order("age")._build_sort(), [("age", 1)])

    def test_order_descending(self):
        self.assertEqual(self.users.order("age", "desc")._build_sort(),
                         [("age", -1)])

    def test_order_compound(self):
        self.assertEqual(
            self.users.order("city", "asc").order("age", "desc")
            ._build_sort(), [("city", 1), ("age", -1)])

    def test_invalid_direction_rejected(self):
        with self.assertRaises(InvalidArgumentException):
            self.users.order("age", "sideways")

    def test_projection(self):
        self.users.field("name", "age")
        self.assertEqual(self.users._projection, {"name": 1, "age": 1})

    def test_exclude(self):
        self.users.exclude("secret")
        self.assertEqual(self.users._projection, {"secret": 0})

    def test_limit_validation(self):
        with self.assertRaises(InvalidArgumentException):
            self.users.limit(0)
        with self.assertRaises(InvalidArgumentException):
            self.users.limit("x")

    def test_page_validation(self):
        with self.assertRaises(InvalidArgumentException):
            self.users.page(0)
        with self.assertRaises(InvalidArgumentException):
            self.users.page(1, 0)
        with self.assertRaises(InvalidArgumentException):
            self.users.page(1, 100000)

    def test_page_computes_skip(self):
        self.users.page(3, 20)
        self.assertEqual(self.users._skip, 40)
        self.assertEqual(self.users._limit, 20)

    def test_after_builds_keyset_filter(self):
        filt = self.users.after("507f1f77bcf86cd799439011")._build_filter()
        self.assertIn("_id", filt)
        self.assertIn("$gt", filt["_id"])

    def test_default_limit_cap(self):
        self.assertEqual(self.users._effective_limit(), 1000)


# ---------------------------------------------------------------------- #
# 5. 增删改查
# ---------------------------------------------------------------------- #
class TestMongoCrud(unittest.TestCase):

    def setUp(self):
        _mongo()
        self.users = Db.mongo("users")

    def test_insert_returns_id(self):
        uid = self.users.insert({"name": "张三", "age": 25})
        self.assertIsNotNone(uid)

    def test_insert_requires_dict(self):
        with self.assertRaises(InvalidArgumentException):
            self.users.insert(["not", "a", "dict"])

    def test_insert_rejects_dollar_field(self):
        with self.assertRaises(InvalidArgumentException):
            self.users.insert({"$set": 1})

    def test_insert_many(self):
        n = self.users.insert_many([{"name": "a"}, {"name": "b"}])
        self.assertEqual(n, 2)

    def test_insert_many_empty(self):
        self.assertEqual(self.users.insert_many([]), 0)

    def test_select_returns_collection(self):
        self.users.insert_many([{"name": "a", "age": 20},
                                {"name": "b", "age": 30}])
        rows = self.users.select()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["name"], "a")

    def test_select_applies_where(self):
        self.users.insert_many([{"name": "a", "age": 20},
                                {"name": "b", "age": 30}])
        rows = self.users.where("age", ">=", 25).select()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["name"], "b")

    def test_select_applies_sort_and_limit(self):
        self.users.insert_many([{"name": "a", "age": 20},
                                {"name": "b", "age": 30},
                                {"name": "c", "age": 25}])
        rows = self.users.order("age", "desc").limit(2).select()
        self.assertEqual([r["age"] for r in rows], [30, 25])

    def test_select_projection(self):
        self.users.insert({"name": "a", "age": 20, "secret": "x"})
        row = self.users.field("name").select()[0]
        self.assertIn("name", row)
        self.assertNotIn("age", row)

    def test_find_by_id(self):
        uid = self.users.insert({"name": "a"})
        self.assertIsNotNone(self.users.find_by_id(uid))
        self.assertIsNone(self.users.find_by_id("ffffffffffffffffffffffff"))

    def test_find_returns_none_when_missing(self):
        self.assertIsNone(self.users.find("nope"))

    def test_count(self):
        self.users.insert_many([{"name": "a"}, {"name": "b"}])
        self.assertEqual(self.users.count(), 2)
        self.assertEqual(self.users.where("name", "a").count(), 1)

    def test_distinct(self):
        self.users.insert_many([{"city": "北京"}, {"city": "北京"},
                                {"city": "上海"}])
        self.assertEqual(sorted(self.users.distinct("city")), ["上海", "北京"])

    def test_update_modifies(self):
        uid = self.users.insert({"name": "a", "age": 20})
        n = self.users.where("_id", uid).update({"age": 26})
        self.assertEqual(n, 1)
        self.assertEqual(self.users.find_by_id(uid)["age"], 26)

    def test_update_requires_non_empty(self):
        with self.assertRaises(InvalidArgumentException):
            self.users.update({})

    def test_update_many(self):
        self.users.insert_many([{"name": "a", "vip": False},
                                {"name": "b", "vip": False}])
        n = self.users.where("vip", False).update_many({"vip": True})
        self.assertEqual(n, 2)

    def test_update_raw_requires_operator(self):
        with self.assertRaises(InvalidArgumentException):
            self.users.update_raw({"age": 1})

    def test_replace_keeps_id(self):
        uid = self.users.insert({"name": "a", "age": 20})
        self.users.where("_id", uid).replace({"name": "b"})
        doc = self.users.find_by_id(uid)
        self.assertEqual(doc["name"], "b")
        self.assertEqual(doc["_id"], uid)

    def test_delete_by_id(self):
        uid = self.users.insert({"name": "a"})
        self.assertEqual(self.users.delete(uid), 1)
        self.assertIsNone(self.users.find_by_id(uid))

    def test_delete_many(self):
        self.users.insert_many([{"name": "a"}, {"name": "b"}])
        self.assertEqual(self.users.delete_many(), 2)

    def test_aggregate(self):
        self.users.insert_many([{"name": "a", "age": 20},
                                {"name": "b", "age": 30}])
        rows = self.users.aggregate([{"$match": {"age": {"$gte": 25}}}])
        self.assertEqual(len(rows), 1)

    def test_aggregate_requires_pipeline(self):
        with self.assertRaises(InvalidArgumentException):
            self.users.aggregate([])

    def test_state_reset_after_terminal(self):
        self.users.where("age", ">=", 18)
        self.users.select()
        self.assertEqual(self.users._build_filter(), {})
        self.assertIsNone(self.users._limit)

    def test_chain_state_resets_between_queries(self):
        self.users.insert_many([{"name": "a", "age": 20},
                                {"name": "b", "age": 30}])
        first = self.users.where("age", ">=", 25).select()
        second = self.users.select()
        self.assertEqual(len(first), 1)
        self.assertEqual(len(second), 2)


# ---------------------------------------------------------------------- #
# 6. 索引
# ---------------------------------------------------------------------- #
class TestMongoIndex(unittest.TestCase):

    def setUp(self):
        _mongo()
        self.users = Db.mongo("users")

    def test_ensure_index_creates(self):
        self.users.ensure_index("email", unique=True)
        names = [i["name"] for i in self.users.indexes()]
        self.assertTrue(any("email" in n for n in names))

    def test_ensure_index_compound(self):
        self.users.ensure_index([("city", 1), ("age", -1)])
        self.assertGreaterEqual(len(self.users.indexes()), 2)

    def test_ensure_index_tuple_form(self):
        self.users.ensure_index(("email", "desc"))
        self.assertGreaterEqual(len(self.users.indexes()), 2)

    def test_invalid_index_fields_rejected(self):
        with self.assertRaises(InvalidArgumentException):
            self.users.ensure_index(123)

    def test_drop_index(self):
        self.users.ensure_index("email", name="idx_email")
        self.users.drop_index("idx_email")
        self.assertNotIn("idx_email", [i["name"] for i in self.users.indexes()])

    def test_explain_returns_plan(self):
        plan = self.users.where("age", ">=", 18).explain()
        self.assertIn("queryPlanner", plan)


# ---------------------------------------------------------------------- #
# 7. Schema 校验
# ---------------------------------------------------------------------- #
class TestMongoSchema(unittest.TestCase):

    def setUp(self):
        _mongo()
        self.users = Db.mongo("users")

    def test_required_field_enforced(self):
        self.users.schema({"name": str, "age": int}, required=["name"])
        with self.assertRaises(InvalidArgumentException) as ctx:
            self.users.insert({"age": 20})
        self.assertIn("必填", str(ctx.exception))

    def test_type_mismatch_rejected(self):
        self.users.schema({"age": int})
        with self.assertRaises(InvalidArgumentException):
            self.users.insert({"age": "twenty"})

    def test_bool_not_accepted_as_int(self):
        self.users.schema({"flag": int})
        with self.assertRaises(InvalidArgumentException):
            self.users.insert({"flag": True})

    def test_float_accepts_int(self):
        self.users.schema({"score": float})
        self.users.insert({"score": 88})

    def test_undeclared_fields_allowed(self):
        self.users.schema({"age": int})
        self.users.insert({"age": 1, "extra": "ok"})

    def test_invalid_schema_type_rejected(self):
        with self.assertRaises(InvalidArgumentException):
            self.users.schema({"x": set})


# ---------------------------------------------------------------------- #
# 8. 异常翻译
# ---------------------------------------------------------------------- #
class TestMongoErrorTranslation(unittest.TestCase):

    def test_duplicate_key(self):
        class FakeDuplicateKeyError(Exception):
            pass

        FakeDuplicateKeyError.__name__ = "DuplicateKeyError"
        err = translate_mongo_error(FakeDuplicateKeyError("dup"))
        self.assertIsInstance(err, DuplicateKeyError)
        self.assertIn("唯一键冲突", str(err))

    def test_connection_failure(self):
        class FakeTimeoutError(Exception):
            pass

        FakeTimeoutError.__name__ = "ServerSelectionTimeoutError"
        err = translate_mongo_error(FakeTimeoutError("timeout"))
        self.assertIsInstance(err, ConnectionFailedError)

    def test_operation_failure(self):
        class FakeOperationFailure(Exception):
            pass

        FakeOperationFailure.__name__ = "OperationFailure"
        err = translate_mongo_error(FakeOperationFailure("bad op"))
        self.assertIsInstance(err, QueryError)

    def test_invalid_id_is_argument_error(self):
        class FakeInvalidId(Exception):
            pass

        FakeInvalidId.__name__ = "InvalidId"
        err = translate_mongo_error(FakeInvalidId("bad id"))
        self.assertIsInstance(err, InvalidArgumentException)

    def test_orm_error_passthrough(self):
        original = QueryError("原样返回")
        self.assertIs(translate_mongo_error(original), original)

    def test_unknown_error_becomes_query_error(self):
        class WeirdError(Exception):
            pass

        err = translate_mongo_error(WeirdError("?"))
        self.assertIsInstance(err, QueryError)

    def test_auth_failure_is_connection_error(self):
        """认证失败（码 13/18）应归为连接错误，而非查询错误。"""
        class FakeOperationFailure(Exception):
            code = 13

        FakeOperationFailure.__name__ = "OperationFailure"
        err = translate_mongo_error(FakeOperationFailure("requires auth"))
        self.assertIsInstance(err, ConnectionFailedError)

    def test_auth_failure_detected_by_message(self):
        class FakeOperationFailure(Exception):
            pass

        FakeOperationFailure.__name__ = "OperationFailure"
        err = translate_mongo_error(
            FakeOperationFailure("Command insert requires authentication"))
        self.assertIsInstance(err, ConnectionFailedError)

    def test_bulk_write_duplicate_key_is_duplicate_error(self):
        """insert_many 撞唯一键抛 BulkWriteError，语义上仍是重复键。"""
        class FakeBulkWriteError(Exception):
            details = {"writeErrors": [{"code": 11000, "errmsg": "E11000"}]}

        FakeBulkWriteError.__name__ = "BulkWriteError"
        err = translate_mongo_error(FakeBulkWriteError("batch op errors"))
        self.assertIsInstance(err, DuplicateKeyError)

    def test_bulk_write_other_error_stays_query_error(self):
        class FakeBulkWriteError(Exception):
            details = {"writeErrors": [{"code": 121, "errmsg": "doc too big"}]}

        FakeBulkWriteError.__name__ = "BulkWriteError"
        err = translate_mongo_error(FakeBulkWriteError("batch op errors"))
        self.assertIsInstance(err, QueryError)

    def test_all_translated_errors_are_orm_errors(self):
        for name in ("DuplicateKeyError", "InvalidId", "InvalidDocument",
                     "ConfigurationError", "ServerSelectionTimeoutError",
                     "ConnectionFailure", "OperationFailure"):
            cls = type(name, (Exception,), {})
            self.assertIsInstance(translate_mongo_error(cls("x")), OrmError)


# ---------------------------------------------------------------------- #
# 9. 驱动契约与集成面
# ---------------------------------------------------------------------- #
class TestMongoDriverContract(unittest.TestCase):

    def test_mongo_driver_registered(self):
        from tinkpyorm.drivers import available_drivers
        self.assertIn("mongodb", available_drivers())

    def test_driver_capability_flags(self):
        _mongo()
        driver = Db.get_connection().driver
        self.assertTrue(driver.supports_mongo)
        self.assertFalse(driver.supports_document)
        self.assertFalse(driver.supports_fts)
        self.assertFalse(driver.supports_upsert)
        self.assertFalse(driver.supports_transactions)

    def test_non_mongo_driver_rejects_mongo_collection(self):
        Db.close()
        Db.config({"database": ":memory:"})
        with self.assertRaises(UnsupportedOperation):
            Db.mongo("users")

    def test_invalid_collection_name_rejected(self):
        _mongo()
        for bad in ("", "1abc", "a-b", "a b"):
            with self.assertRaises(InvalidArgumentException, msg=repr(bad)):
                Db.mongo(bad)

    def test_write_concern_unknown_key_rejected_before_pymongo(self):
        """未知写关注键在导入 pymongo 之前就被拒绝（离线可测）。"""
        _mongo()
        coll = Db.mongo("users", write_concern={"bogus": 1})
        with self.assertRaises(InvalidArgumentException):
            coll.count()

    def test_connection_failure_is_raised_eagerly(self):
        """hello 完全无响应时 connect() 应快速失败，而不是留个半成品。"""
        _mongo()

        class Boom(Exception):
            pass

        client = Db.get_connection().driver

        def _broken(*a, **k):
            raise Boom("no hello")

        # 让两条探测路径都失败
        original = client._version_from_buildinfo
        client._version_from_buildinfo = lambda: None
        try:
            client._client = type("C", (), {"admin": type(
                "A", (), {"command": staticmethod(_broken)})()})()
            with self.assertRaises(ConnectionFailedError):
                client._detect_version()
        finally:
            client._version_from_buildinfo = original
            client._client = None

    def test_collection_calls_translate_third_party_errors(self):
        """集合层不得泄漏 pymongo 异常（真实服务端上认证失败即此路径）。"""
        _mongo()

        class Boom(Exception):
            pass

        Boom.__name__ = "OperationFailure"
        coll = Db.mongo("users")

        def _boom(*args, **kwargs):
            raise Boom("requires authentication")

        coll._raw = lambda: type("C", (), {
            "insert_one": _boom, "find": _boom, "count_documents": _boom,
        })()
        with self.assertRaises(ConnectionFailedError):
            coll.insert({"a": 1})

    def test_ping_and_close(self):
        _mongo()
        driver = Db.get_connection().driver
        self.assertTrue(driver.ping())
        driver.close()
        self.assertFalse(driver.is_connected())

    def test_config_type_alias_mongo(self):
        from tinkpyorm.config import normalize_type
        self.assertEqual(normalize_type("mongo"), "mongodb")


if __name__ == "__main__":
    unittest.main(verbosity=2)
