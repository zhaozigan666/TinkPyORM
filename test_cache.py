# -*- coding: utf-8 -*-
"""查询缓存测试（v0.4.0：进程级缓存 + TTL + LRU + 写操作自动失效）。

覆盖要点：
- 跨 Query 实例命中（旧实现为实例级存储，必然穿透）
- TTL 倒计时过期、TTL<=0 不缓存、负数报错
- find / select / value / count / column 全覆盖
- 写操作（insert / update / delete）自动按表失效
- 结果隔离（修改返回值不污染缓存）
- 自定义键、旧签名兼容、后端可替换、LRU 容量与清理
"""
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from tinkpyorm import CacheStore, Db, MemoryCacheStore, cache
from tinkpyorm.exceptions import InvalidArgumentException


def _setup_table(rows: int = 3):
    """重建内存库与测试表；返回默认连接。"""
    Db.close()          # 释放上一用例的连接，避免测试间资源泄漏
    Db.set_config({"database": ":memory:"})
    Db.execute("CREATE TABLE data (id INTEGER PRIMARY KEY, url TEXT, v INTEGER)")
    for i in range(rows):
        Db.table("data").insert({"url": "u%d" % i, "v": i})
    Db.clear_cache()
    Db.sql_log_clear()
    return Db.get_connection()


class TestQueryCache(unittest.TestCase):
    """链式查询的缓存行为。"""

    def setUp(self):
        self.conn = _setup_table()

    def tearDown(self):
        Db.clear_cache()

    def _sql_count(self) -> int:
        return len(Db.get_sql_log())

    # ------------------------------------------------------------------ #
    # 命中
    # ------------------------------------------------------------------ #
    def test_cross_query_instance_hit(self):
        """跨 Query 实例命中：第二次查询不再产生 SQL。

        这是 v0.4.0 的核心修复——旧实现把缓存挂在 Query 实例上，
        而每次 ``Db.name()`` 都会新建 Query，导致缓存从不命中。
        """
        first = Db.name("data").where("url", "u1").cache(10).find()
        self.assertEqual(first["v"], 1)
        self.assertEqual(self._sql_count(), 1)

        second = Db.name("data").where("url", "u1").cache(10).find()
        self.assertEqual(second["v"], 1)
        self.assertEqual(self._sql_count(), 1, "第二次查询应命中缓存")

    def test_select_cached(self):
        rows = Db.table("data").cache(10).select()
        self.assertEqual(len(rows), 3)
        self.assertEqual(self._sql_count(), 1)
        rows2 = Db.table("data").cache(10).select()
        self.assertEqual(len(rows2), 3)
        self.assertEqual(self._sql_count(), 1)

    def test_value_and_column_cached(self):
        self.assertEqual(Db.table("data").where("id", 2).cache(10).value("url"), "u1")
        count_after_first = self._sql_count()
        self.assertEqual(Db.table("data").where("id", 2).cache(10).value("url"), "u1")
        self.assertEqual(self._sql_count(), count_after_first)

        Db.clear_cache()
        Db.sql_log_clear()
        self.assertEqual(Db.table("data").cache(10).column("v"), [0, 1, 2])
        self.assertEqual(self._sql_count(), 1)
        self.assertEqual(Db.table("data").cache(10).column("v"), [0, 1, 2])
        self.assertEqual(self._sql_count(), 1)

    def test_count_cached(self):
        self.assertEqual(Db.table("data").cache(10).count(), 3)
        self.assertEqual(self._sql_count(), 1)
        self.assertEqual(Db.table("data").cache(10).count(), 3)
        self.assertEqual(self._sql_count(), 1)

    def test_model_query_cached(self):
        """模型静态查询同样受益于缓存。"""
        Db.execute("CREATE TABLE IF NOT EXISTS m_user (id INTEGER PRIMARY KEY, name TEXT)")
        Db.execute("DELETE FROM m_user")
        Db.table("m_user").insert({"name": "alice"})

        from tinkpyorm import Model

        class MUser(Model):
            __table__ = "m_user"

        Db.clear_cache()
        Db.sql_log_clear()
        self.assertEqual(MUser.where("name", "alice").cache(10).find()["name"], "alice")
        self.assertEqual(self._sql_count(), 1)
        self.assertEqual(MUser.where("name", "alice").cache(10).find()["name"], "alice")
        self.assertEqual(self._sql_count(), 1)

    # ------------------------------------------------------------------ #
    # 过期
    # ------------------------------------------------------------------ #
    def test_ttl_expires(self):
        """cache(1) 表示 1 秒后失效（倒计时语义）。"""
        q = Db.name("data").where("url", "u1").cache(1)
        q.find()
        Db.sql_log_clear()
        time.sleep(1.05)
        q.find()
        self.assertEqual(self._sql_count(), 1, "TTL 过期后应重新查询数据库")

    def test_zero_ttl_never_caches(self):
        Db.name("data").where("url", "u1").cache(0).find()
        self.assertEqual(self._sql_count(), 1)
        Db.name("data").where("url", "u1").cache(0).find()
        self.assertEqual(self._sql_count(), 2, "TTL=0 应每次穿透")

    def test_negative_ttl_raises(self):
        with self.assertRaises(InvalidArgumentException):
            Db.name("data").cache(-1)

    # ------------------------------------------------------------------ #
    # 失效
    # ------------------------------------------------------------------ #
    def test_insert_invalidates(self):
        Db.name("data").where("url", "u1").cache(60).find()
        self.assertEqual(self._sql_count(), 1)
        Db.table("data").insert({"url": "u1", "v": 999})
        Db.sql_log_clear()
        got = Db.name("data").where("url", "u1").cache(60).find()
        self.assertEqual(self._sql_count(), 1, "写操作后缓存应已失效")
        self.assertIn(got["v"], (1, 999))

    def test_update_invalidates(self):
        Db.name("data").where("url", "u1").cache(60).find()
        Db.table("data").where("url", "u1").update({"v": 99})
        Db.sql_log_clear()
        self.assertEqual(Db.name("data").where("url", "u1").cache(60).find()["v"], 99)
        self.assertEqual(self._sql_count(), 1)

    def test_delete_invalidates(self):
        Db.name("data").where("url", "u1").cache(60).find()
        Db.table("data").where("url", "u1").delete()
        Db.sql_log_clear()
        self.assertIsNone(Db.name("data").where("url", "u1").cache(60).find())
        self.assertEqual(self._sql_count(), 1)

    def test_insert_all_invalidates(self):
        Db.name("data").cache(60).count()
        Db.table("data").insert_all([{"url": "x", "v": 7}, {"url": "y", "v": 8}])
        Db.sql_log_clear()
        self.assertEqual(Db.name("data").cache(60).count(), 5)
        self.assertEqual(self._sql_count(), 1)

    def test_raw_sql_requires_manual_clear(self):
        """裸 SQL 不会自动清缓存，需手动 clear_cache（文档语义）。"""
        Db.name("data").cache(60).count()
        Db.execute("UPDATE data SET v = 42")
        self.assertEqual(Db.name("data").cache(60).count(), 3, "裸 SQL 后仍读缓存")
        Db.clear_cache()
        Db.sql_log_clear()
        Db.name("data").cache(60).count()
        self.assertEqual(self._sql_count(), 1)

    # ------------------------------------------------------------------ #
    # 隔离与键
    # ------------------------------------------------------------------ #
    def test_find_result_isolation(self):
        """修改 find() 的返回值不得污染缓存内容。"""
        row = Db.name("data").where("url", "u1").cache(60).find()
        row["v"] = -1
        Db.sql_log_clear()
        again = Db.name("data").where("url", "u1").cache(60).find()
        self.assertEqual(again["v"], 1)
        self.assertEqual(self._sql_count(), 0, "第二次应命中缓存")

    def test_select_result_isolation(self):
        rows = Db.name("data").where("url", "u1").cache(60).select()
        rows[0]["v"] = -1
        again = Db.name("data").where("url", "u1").cache(60).select()
        self.assertEqual(again[0]["v"], 1)

    def test_custom_key(self):
        k = "my-key"
        Db.name("data").where("url", "u1").cache(60, k).find()
        self.assertEqual(self._sql_count(), 1)
        Db.name("data").where("url", "u2").cache(60, k).find()  # 同键 -> 命中
        self.assertEqual(self._sql_count(), 1, "自定义键应复用同一条缓存")
        # 写操作按表清空，自定义键同样失效
        Db.table("data").where("url", "u1").update({"v": 5})
        Db.sql_log_clear()
        Db.name("data").where("url", "u1").cache(60, k).find()
        self.assertEqual(self._sql_count(), 1)

    def test_legacy_signature(self):
        """兼容旧写法 cache(key, expire)。"""
        Db.name("data").where("url", "u1").cache("legacy-key", 120).find()
        self.assertEqual(self._sql_count(), 1)
        Db.name("data").where("url", "u1").cache("legacy-key", 120).find()
        self.assertEqual(self._sql_count(), 1)

    def test_different_sql_not_shared(self):
        Db.name("data").where("url", "u1").cache(60).find()
        Db.sql_log_clear()
        Db.name("data").where("url", "u2").cache(60).find()
        self.assertEqual(self._sql_count(), 1, "不同条件应各自成键")


class TestMemoryCacheStore(unittest.TestCase):
    """缓存后端自身的行为（不经 ORM）。"""

    def test_set_get_delete_clear(self):
        s = MemoryCacheStore(max_items=10)
        self.assertEqual(s.get("a"), (False, None))
        s.set("a", [{"x": 1}], 60)
        self.assertEqual(s.get("a"), (True, [{"x": 1}]))
        self.assertTrue(s.delete("a"))
        self.assertFalse(s.delete("a"))
        s.set("b", 1, 60)
        s.set("c", 2, 60)
        self.assertEqual(s.clear(prefix="b"), 1)
        self.assertEqual(s.clear(), 1)
        self.assertEqual(len(s), 0)

    def test_none_value_distinguishable(self):
        """命中且值为 None 时，必须能与"未命中"区分。"""
        s = MemoryCacheStore()
        s.set("k", None, 60)
        hit, value = s.get("k")
        self.assertTrue(hit)
        self.assertIsNone(value)
        s.delete("k")
        hit2, _ = s.get("k")
        self.assertFalse(hit2)

    def test_ttl_and_purge(self):
        s = MemoryCacheStore()
        s.set("soon", 1, 0.05)
        self.assertEqual(s.get("soon"), (True, 1))
        time.sleep(0.06)
        self.assertEqual(s.get("soon"), (False, None))

        s.set("p1", 1, 0.05)
        s.set("p2", 2, 60)
        time.sleep(0.06)
        self.assertEqual(s.purge(), 1, "purge 只清理已过期条目")
        self.assertEqual(len(s), 1)

    def test_lru_eviction(self):
        s = MemoryCacheStore(max_items=3)
        for i in range(3):
            s.set("k%d" % i, i, 60)
        s.get("k0")           # k0 变为最近使用
        s.set("k3", 3, 60)    # 触发淘汰，应淘汰 k1
        self.assertFalse(s.get("k1")[0])
        self.assertTrue(s.get("k0")[0])
        self.assertEqual(len(s), 3)

    def test_stats(self):
        s = MemoryCacheStore(max_items=5)
        s.set("a", 1, 60)
        s.get("a")
        s.get("miss")
        st = s.stats()
        self.assertEqual(st["items"], 1)
        self.assertEqual(st["hits"], 1)
        self.assertEqual(st["misses"], 1)
        self.assertEqual(st["max_items"], 5)

    def test_custom_backend_replaceable(self):
        """set_store 可注入自定义后端，Db 读取时使用该后端。"""
        class CountingStore(CacheStore):
            def __init__(self):
                self.sets = 0
                self.mem = MemoryCacheStore()

            def get(self, key):
                return self.mem.get(key)

            def set(self, key, value, ttl=None):
                self.sets += 1
                self.mem.set(key, value, ttl)

            def delete(self, key):
                return self.mem.delete(key)

            def clear(self, prefix=None):
                return self.mem.clear(prefix)

        _setup_table()
        s = CountingStore()
        old = cache.set_store(s)
        try:
            self.assertIs(Db.cache_store(), s)
            Db.name("data").where("url", "u0").cache(30).find()
            self.assertEqual(s.sets, 1)
        finally:
            cache.set_store(old)
        self.assertIsInstance(Db.cache_store(), MemoryCacheStore)


if __name__ == "__main__":
    unittest.main(verbosity=2)
