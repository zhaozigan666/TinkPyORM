# -*- coding: utf-8 -*-
"""Redis **真实服务端**集成测试（v0.13.0）。

与 ``test_redis.py``（离线假客户端）互补：本文件跑真实 redis-py + 真实
Redis 服务端，覆盖离线测不出来的路径——真实命令语义、版本探测、错误码、
哈希字段级过期、流水线原子性、发布订阅。

运行方式（凭据**不入库**，只从环境变量读）::

    TINKPYORM_REDIS_TEST_URL='redis://:pwd@host:6379/0' python test_redis_live.py

未设置该环境变量（或 redis-py 未安装、服务端不可达）时**整份跳过**，
因此可安全地并入常规测试流程。

所有键都带 ``tinkpyorm_it:`` 前缀，跑完只清自己的键，**绝不 FLUSHDB**。
"""
from __future__ import annotations

import os
import unittest
from urllib.parse import unquote, urlsplit

ENV_URL = "TINKPYORM_REDIS_TEST_URL"

#: 本次测试的键前缀（所有写入都落在这个命名空间下）
PREFIX = "tinkpyorm_it:"

_SKIP_REASON = None
_Db = None
try:  # pragma: no cover - 取决于环境
    from tinkpyorm import Db
    from tinkpyorm.exceptions import (
        ConnectionFailedError, InvalidArgumentException, OrmError, QueryError,
    )
    if not os.environ.get(ENV_URL):
        _SKIP_REASON = f"未设置环境变量 {ENV_URL}，跳过真实服务端集成测试"
    else:
        __import__("redis")     # 依赖探测：缺 redis-py 时整份跳过
        _Db = Db
except ImportError as exc:  # pragma: no cover
    _SKIP_REASON = f"依赖缺失（{exc}），跳过真实服务端集成测试"


def _parts():
    parsed = urlsplit(os.environ[ENV_URL])
    return {
        "host": parsed.hostname or "127.0.0.1",
        "port": parsed.port or 6379,
        "password": unquote(parsed.password) if parsed.password else None,
        "user": unquote(parsed.username) if parsed.username else None,
        "database": int((parsed.path or "/0").lstrip("/") or 0),
    }


def _connect(**overrides):
    """切换到测试库并返回连接（每例独立连接，避免状态互相污染）。"""
    _Db.close()
    _Db._prefix = ""
    data = {"type": "redis"}
    data.update(_parts())
    data.update(overrides)
    options = {"socket_connect_timeout": 8, "socket_timeout": 8}
    options.update(data.pop("options", {}))
    data["options"] = options
    return _Db.config(data)


@unittest.skipIf(_SKIP_REASON is not None, _SKIP_REASON or "")
class LiveBase(unittest.TestCase):
    """公共夹具：每例重新连接，跑完清理自建键。"""

    @classmethod
    def setUpClass(cls):
        conn = _connect()
        try:
            conn.driver.connect()
        except OrmError as exc:  # 服务端不可达时整类跳过
            raise unittest.SkipTest(f"服务端不可达：{exc}")

    def setUp(self):
        self.conn = _connect()
        self.driver = self.conn.driver
        self.store = _Db.redis(PREFIX)
        self.store.clear("it:*")

    @classmethod
    def tearDownClass(cls):
        try:
            _connect().driver.connect()
            _Db.redis(PREFIX).clear("it:*")
        except Exception:
            pass
        finally:
            _Db.close()


# ---------------------------------------------------------------------- #
# 1. 连接与版本
# ---------------------------------------------------------------------- #
class TestLiveConnection(LiveBase):

    def test_ping(self):
        self.assertTrue(self.driver.ping())
        self.assertTrue(self.store.ping())

    def test_version_detected(self):
        version = self.driver.detect_version()
        self.assertGreaterEqual(tuple(version), (4, 0, 0))
        self.assertEqual(str(version), str(self.driver.version))

    def test_topology_detected(self):
        self.driver.connect()
        self.assertIn(self.driver.topology, ("standalone", "cluster", "sentinel"))

    def test_dialect_info(self):
        info = self.driver.dialect_info()
        self.assertEqual(info["driver"], "redis")
        self.assertTrue(info["detected_version"])
        self.assertIn("lmpop", info["features"])

    def test_declared_version_too_high_rejected(self):
        """声明版本高于实际服务端必须在连接期报错。

        注意：必须落在**支持范围内**（8.x 任意小版本），否则会在配置阶段
        就被版本校验拦掉，测不到"声明 vs 实际"这条断言。
        """
        conn = _connect(version="8.99")
        with self.assertRaises(InvalidArgumentException) as ctx:
            conn.driver.connect()
        self.assertIn("高于实际服务端版本", str(ctx.exception))

    def test_auth_failure_is_connection_error(self):
        """密码错误必须是 ConnectionFailedError（而不是查询错误）。"""
        parts = _parts()
        parts["password"] = "definitely-wrong-password"
        _Db.close()
        _Db._prefix = ""
        conn = _Db.config({"type": "redis", "options": {
            "socket_connect_timeout": 5, "socket_timeout": 5}, **parts})
        with self.assertRaises(ConnectionFailedError):
            conn.driver.connect()
        self.assertFalse(conn.driver.is_connected())

    def test_connect_is_not_lazy(self):
        """``connect()`` 返回时已完成握手（可立即报出版本）。"""
        self.driver.close()
        self.driver.connect()
        self.assertIsNotNone(self.driver.server_version)


# ---------------------------------------------------------------------- #
# 2. 字符串 / 过期
# ---------------------------------------------------------------------- #
class TestLiveString(LiveBase):

    def test_set_get_delete(self):
        self.assertTrue(self.store.set("it:s1", "hello"))
        self.assertEqual(self.store.get("it:s1"), "hello")
        self.assertEqual(self.store.exists("it:s1"), 1)
        self.assertEqual(self.store.delete("it:s1"), 1)
        self.assertIsNone(self.store.get("it:s1"))

    def test_set_with_ex_ttl(self):
        self.store.set("it:ttl", "v", ex=60)
        self.assertGreater(self.store.ttl("it:ttl"), 0)
        self.assertLessEqual(self.store.ttl("it:ttl"), 60)

    def test_set_nx(self):
        self.store.set("it:nx", "first", nx=True)
        self.store.set("it:nx", "second", nx=True)
        self.assertEqual(self.store.get("it:nx"), "first")

    def test_get_with_default(self):
        self.assertEqual(self.store.get("it:absent", default="fallback"),
                         "fallback")

    def test_mset_mget(self):
        self.store.mset({"it:a": "1", "it:b": "2"})
        self.assertEqual(self.store.mget("it:a", "it:b", "it:c"),
                         ["1", "2", None])

    def test_incr_decr(self):
        self.assertEqual(self.store.incr("it:cnt", 5), 5)
        self.assertEqual(self.store.incr("it:cnt"), 6)
        self.assertEqual(self.store.decr("it:cnt", 2), 4)

    def test_incrbyfloat(self):
        self.assertAlmostEqual(self.store.incrbyfloat("it:f", 1.5), 1.5)

    def test_append_strlen(self):
        self.store.set("it:app", "ab")
        self.assertEqual(self.store.append("it:app", "cd"), 4)
        self.assertEqual(self.store.strlen("it:app"), 4)

    def test_unicode_value(self):
        self.store.set("it:cn", "中文值 🚀")
        self.assertEqual(self.store.get("it:cn"), "中文值 🚀")

    def test_set_json_roundtrip(self):
        payload = {"name": "张三", "tags": ["vip", "beta"], "n": 3}
        self.store.set_json("it:json", payload)
        self.assertEqual(self.store.get_json("it:json"), payload)

    def test_get_json_missing(self):
        with self.assertRaises(QueryError):
            self.store.get_json("it:nothing")
        self.assertIsNone(self.store.get_json("it:nothing", default=None))

    def test_binary_safe_large_value(self):
        blob = "x" * (1024 * 1024)
        self.store.set("it:big", blob)
        self.assertEqual(len(self.store.get("it:big")), len(blob))

    def test_expire_persist(self):
        self.store.set("it:exp", "v")
        self.assertTrue(self.store.expire("it:exp", 100))
        self.assertGreater(self.store.ttl("it:exp"), 0)
        self.assertTrue(self.store.persist("it:exp"))
        self.assertEqual(self.store.ttl("it:exp"), -1)

    def test_type_and_rename(self):
        self.store.set("it:old", "v")
        self.assertEqual(self.store.type_of("it:old"), "string")
        self.store.rename("it:old", "it:new")
        self.assertIsNone(self.store.get("it:old"))
        self.assertEqual(self.store.get("it:new"), "v")

    def test_getdel(self):
        if not self.driver.supports_feature("getdel"):
            self.skipTest("服务端 < 6.2，无 GETDEL")
        self.store.set("it:gd", "v")
        self.assertEqual(self.store.getdel("it:gd"), "v")
        self.assertIsNone(self.store.get("it:gd"))


# ---------------------------------------------------------------------- #
# 3. 哈希
# ---------------------------------------------------------------------- #
class TestLiveHash(LiveBase):

    def test_hset_hget_hgetall(self):
        self.assertEqual(self.store.hset("it:h", {"name": "张三", "age": 25}), 2)
        self.assertEqual(self.store.hget("it:h", "name"), "张三")
        self.assertEqual(self.store.hgetall("it:h"),
                         {"name": "张三", "age": "25"})

    def test_hdel_hlen_hexists(self):
        self.store.hset("it:h", {"a": 1, "b": 2})
        self.assertTrue(self.store.hexists("it:h", "a"))
        self.assertEqual(self.store.hlen("it:h"), 2)
        self.assertEqual(self.store.hdel("it:h", "a"), 1)
        self.assertFalse(self.store.hexists("it:h", "a"))

    def test_hincrby(self):
        self.assertEqual(self.store.hincrby("it:h", "n", 3), 3)
        self.assertEqual(self.store.hincrby("it:h", "n", 2), 5)

    def test_hkeys_hvals(self):
        self.store.hset("it:h", {"x": "1", "y": "2"})
        self.assertEqual(sorted(self.store.hkeys("it:h")), ["x", "y"])
        self.assertEqual(sorted(self.store.hvals("it:h")), ["1", "2"])

    def test_hmget(self):
        self.store.hset("it:h", {"x": "1", "y": "2"})
        self.assertEqual(self.store.hmget("it:h", "x", "z"), ["1", None])

    def test_hscan_iter(self):
        mapping = {f"f{i}": i for i in range(20)}
        self.store.hset("it:h", mapping)
        seen = {}
        for chunk in self.store.hscan_iter("it:h"):
            seen.update(chunk)
        self.assertEqual(len(seen), 20)

    def test_hash_field_expiry(self):
        """字段级过期需 Redis >= 7.4。"""
        if not self.driver.supports_feature("hash_field_expiry"):
            self.skipTest("服务端 < 7.4，无哈希字段级过期")
        self.store.hset("it:hf", {"a": 1, "b": 2})
        self.assertEqual(self.store.hexpire("it:hf", 60, "a"), [1])
        self.assertGreater(self.store.httl("it:hf", "a")[0], 0)
        self.assertEqual(self.store.httl("it:hf", "b")[0], -1)
        self.assertEqual(self.store.hpersist("it:hf", "a"), [1])


# ---------------------------------------------------------------------- #
# 4. 列表 / 集合 / 有序集合
# ---------------------------------------------------------------------- #
class TestLiveList(LiveBase):

    def test_push_pop_range(self):
        self.store.rpush("it:l", "a", "b", "c")
        self.assertEqual(self.store.lrange("it:l", 0, -1), ["a", "b", "c"])
        self.assertEqual(self.store.lpop("it:l"), "a")
        self.assertEqual(self.store.rpop("it:l"), "c")
        self.assertEqual(self.store.llen("it:l"), 1)

    def test_lindex_lset_trim(self):
        self.store.rpush("it:l", "a", "b", "c")
        self.assertEqual(self.store.lindex("it:l", 1), "b")
        self.store.lset("it:l", 1, "B")
        self.assertEqual(self.store.lindex("it:l", 1), "B")
        self.store.ltrim("it:l", 0, 0)
        self.assertEqual(self.store.llen("it:l"), 1)

    def test_lmove(self):
        if not self.driver.supports_feature("lmove"):
            self.skipTest("服务端 < 6.2，无 LMOVE")
        self.store.rpush("it:src", "a")
        self.assertEqual(self.store.lmove("it:src", "it:dst", "LEFT", "RIGHT"),
                         "a")
        self.assertEqual(self.store.lrange("it:dst", 0, -1), ["a"])


class TestLiveSet(LiveBase):

    def test_sadd_smembers(self):
        self.assertEqual(self.store.sadd("it:s", "a", "b"), 2)
        self.assertEqual(sorted(self.store.smembers("it:s")), ["a", "b"])
        self.assertTrue(self.store.sismember("it:s", "a"))
        self.assertEqual(self.store.scard("it:s"), 2)

    def test_srem(self):
        self.store.sadd("it:s", "a", "b")
        self.assertEqual(self.store.srem("it:s", "a"), 1)

    def test_smismember(self):
        if not self.driver.supports_feature("smismember"):
            self.skipTest("服务端 < 6.2，无 SMISMEMBER")
        self.store.sadd("it:s", "a")
        self.assertEqual(self.store.smismember("it:s", "a", "z"), [True, False])

    def test_sunion_sinter_sdiff(self):
        self.store.sadd("it:s1", "a", "b")
        self.store.sadd("it:s2", "b", "c")
        self.assertEqual(sorted(self.store.sunion("it:s1", "it:s2")),
                         ["a", "b", "c"])
        self.assertEqual(self.store.sinter("it:s1", "it:s2"), ["b"])
        self.assertEqual(self.store.sdiff("it:s1", "it:s2"), ["a"])

    def test_sintercard(self):
        if not self.driver.supports_feature("sintercard"):
            self.skipTest("服务端 < 7.0，无 SINTERCARD")
        self.store.sadd("it:s1", "a", "b")
        self.store.sadd("it:s2", "b", "c")
        self.assertEqual(self.store.sintercard(["it:s1", "it:s2"]), 1)

    def test_sscan_iter(self):
        self.store.sadd("it:s", *[f"m{i}" for i in range(30)])
        self.assertEqual(len(list(self.store.sscan_iter("it:s"))), 30)


class TestLiveZSet(LiveBase):

    def test_zadd_zrange(self):
        self.assertEqual(self.store.zadd("it:z", {"张三": 98.5, "李四": 91}), 2)
        self.assertEqual(self.store.zrange("it:z", 0, -1), ["李四", "张三"])
        self.assertEqual(self.store.zrange("it:z", 0, -1, withscores=True),
                         [("李四", 91.0), ("张三", 98.5)])

    def test_zrange_desc(self):
        self.store.zadd("it:z", {"a": 1, "b": 2})
        self.assertEqual(self.store.zrange("it:z", 0, -1, desc=True), ["b", "a"])

    def test_zscore_zrank(self):
        self.store.zadd("it:z", {"a": 1, "b": 2})
        self.assertEqual(self.store.zscore("it:z", "b"), 2.0)
        self.assertIsNone(self.store.zscore("it:z", "zzz"))
        self.assertEqual(self.store.zrank("it:z", "a"), 0)
        self.assertEqual(self.store.zrank("it:z", "a", desc=True), 1)

    def test_zincrby_zcard_zcount(self):
        self.store.zadd("it:z", {"a": 1})
        self.assertEqual(self.store.zincrby("it:z", 2.5, "a"), 3.5)
        self.assertEqual(self.store.zcard("it:z"), 1)
        self.assertEqual(self.store.zcount("it:z", 0, 10), 1)

    def test_zrangebyscore(self):
        self.store.zadd("it:z", {"a": 1, "b": 5, "c": 9})
        self.assertEqual(self.store.zrangebyscore("it:z", 2, 9), ["b", "c"])

    def test_zpopmin(self):
        if not self.driver.supports_feature("zpopmin"):
            self.skipTest("服务端 < 5.0，无 ZPOPMIN")
        self.store.zadd("it:z", {"a": 1, "b": 2})
        self.assertEqual(self.store.zpopmin("it:z"), [("a", 1.0)])

    def test_zrem(self):
        self.store.zadd("it:z", {"a": 1, "b": 2})
        self.assertEqual(self.store.zrem("it:z", "a"), 1)


# ---------------------------------------------------------------------- #
# 5. 扫描 / 清理
# ---------------------------------------------------------------------- #
class TestLiveScan(LiveBase):

    def test_scan_iter_returns_logical_keys(self):
        self.store.set("it:k1", "v")
        self.store.set("it:other", "v")
        found = set(self.store.scan_iter(match="it:k*"))
        self.assertEqual(found, {"it:k1"})

    def test_scan_iter_all(self):
        self.store.set("it:a", "1")
        self.store.set("it:b", "2")
        self.assertEqual(len(set(self.store.scan_iter(match="it:*"))), 2)

    def test_clear_removes_only_prefixed(self):
        self.store.set("it:c1", "v")
        self.store.set("it:c2", "v")
        self.assertGreaterEqual(self.store.clear("it:c*"), 2)
        self.assertEqual(self.store.get("it:c1"), None)

    def test_clear_rejects_global(self):
        with self.assertRaises(InvalidArgumentException):
            self.store.clear("*")


# ---------------------------------------------------------------------- #
# 6. 流水线 / 发布订阅
# ---------------------------------------------------------------------- #
class TestLivePipeline(LiveBase):

    def test_transaction_executes_all(self):
        with self.store.pipeline() as pipe:
            pipe.set("it:p1", "v")
            pipe.incrby("it:p2", 3)
            pipe.hset("it:p3", {"k": 1})
            results = pipe.execute()
        self.assertEqual(results, [True, 3, 1])
        self.assertEqual(self.store.get("it:p1"), "v")
        self.assertEqual(self.store.get("it:p2"), "3")
        self.assertEqual(self.store.hgetall("it:p3"), {"k": "1"})

    def test_pipeline_is_atomic_under_multi(self):
        """MULTI/EXEC 下命令一起生效（中途看不到中间态由服务端保证）。"""
        pipe = self.store.pipeline(transaction=True)
        pipe.set("it:tx1", "a").set("it:tx2", "b")
        pipe.execute()
        self.assertEqual(self.store.mget("it:tx1", "it:tx2"), ["a", "b"])

    def test_non_transactional_pipeline(self):
        pipe = self.store.pipeline(transaction=False)
        pipe.set("it:np1", "a").incrby("it:np2", 2)
        self.assertEqual(pipe.execute(), [True, 2])

    def test_pipeline_rejects_unknown_command(self):
        pipe = self.store.pipeline()
        with self.assertRaises(InvalidArgumentException):
            pipe.not_a_real_command("it:x")

    def test_pipeline_error_translated(self):
        """流水线里的 WRONGTYPE 必须翻译成项目异常。"""
        self.store.set("it:str", "v")
        pipe = self.store.pipeline()
        pipe.lpush("it:str", "x")     # 对 string 用 list 命令 -> WRONGTYPE
        with self.assertRaises(QueryError):
            pipe.execute()


class TestLivePubSub(LiveBase):

    def test_publish_subscribe(self):
        sub = self.store.subscribe("events")
        try:
            receivers = self.store.publish("events", "hello")
            self.assertGreaterEqual(receivers, 1)
            payload = None
            for _ in range(5):
                message = sub.get_message(timeout=2)
                if message and message.get("type") == "message":
                    payload = message.get("data")
                    break
            self.assertEqual(payload, "hello")
        finally:
            sub.close()

    def test_psubscribe(self):
        sub = self.store.subscribe(patterns="evt:*")
        try:
            self.store.publish("evt:login", "u1")
            payload = None
            for _ in range(5):
                message = sub.get_message(timeout=2)
                if message and message.get("type") in ("pmessage", "message"):
                    payload = message.get("data")
                    break
            self.assertEqual(payload, "u1")
        finally:
            sub.close()


# ---------------------------------------------------------------------- #
# 7. 错误路径
# ---------------------------------------------------------------------- #
class TestLiveErrors(LiveBase):

    def test_wrong_type_translated(self):
        self.store.set("it:wt", "v")
        with self.assertRaises(QueryError) as ctx:
            self.store.lpush("it:wt", "x")
        self.assertIn("Redis", str(ctx.exception))

    def test_server_error_surfaces_as_query_error(self):
        """服务端拒绝的命令必须经 ``_call`` 翻译成 QueryError。

        注意：直接调 ``client.execute_command`` 会绕过归口（这是逃生舱的
        既定语义），因此这里显式走 ``store._call`` 验证翻译路径。
        """
        client = self.store.client()
        with self.assertRaises(QueryError):
            self.store._call(client.execute_command, "NOT_A_COMMAND_XYZ")

    def test_decode_responses_false_returns_bytes(self):
        _Db.close()
        _Db._prefix = ""
        parts = _parts()
        parts["options"] = {"decode_responses": False,
                            "socket_connect_timeout": 8, "socket_timeout": 8}
        _Db.config({"type": "redis", **parts})
        store = _Db.redis(PREFIX)
        store.set("it:bytes", "v")
        self.assertEqual(store.get("it:bytes"), b"v")
        store.delete("it:bytes")


if __name__ == "__main__":
    unittest.main(verbosity=2)
