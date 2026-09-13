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
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from tinkpyorm import (CacheStore, Db, FileCacheStore, MemoryCacheStore,
                       cache)
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


class TestFileCacheStore(unittest.TestCase):
    """FileCacheStore 单元行为（v0.9.1）。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="torm_cache_")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_set_get_roundtrip(self):
        s = FileCacheStore(self.dir)
        s.set("k1", {"a": 1, "b": ["x", "y"]})
        s.set("k2", [1, 2, 3])
        self.assertEqual(s.get("k1"), (True, {"a": 1, "b": ["x", "y"]}))
        self.assertEqual(s.get("k2"), (True, [1, 2, 3]))

    def test_miss_distinguishable(self):
        s = FileCacheStore(self.dir)
        s.set("k", None)
        self.assertEqual(s.get("k"), (True, None))   # 命中但值为 None
        self.assertEqual(s.get("nope"), (False, None))  # 未命中

    def test_ttl_expires_and_file_removed(self):
        import os
        s = FileCacheStore(self.dir)
        s.set("k", "v", ttl=0.15)
        self.assertEqual(len(os.listdir(self.dir)), 1)
        time.sleep(0.2)
        self.assertEqual(s.get("k"), (False, None))
        self.assertEqual(os.listdir(self.dir), [])   # 过期即清文件

    def test_no_ttl_persists(self):
        s = FileCacheStore(self.dir)
        s.set("k", "v")   # 无 TTL 永久
        self.assertEqual(s.get("k"), (True, "v"))

    def test_purge_keeps_valid_entries(self):
        import os
        s = FileCacheStore(self.dir)
        s.set("expired", 1, ttl=0.15)
        s.set("alive", 2)
        time.sleep(0.2)
        self.assertEqual(s.purge(), 1)
        self.assertEqual(s.get("alive"), (True, 2))
        self.assertEqual(os.listdir(self.dir),
                         [n for n in os.listdir(self.dir) if "alive" not in n]
                         or os.listdir(self.dir))
        self.assertEqual(len(s), 1)

    def test_delete(self):
        s = FileCacheStore(self.dir)
        s.set("k", 1)
        self.assertTrue(s.delete("k"))
        self.assertFalse(s.delete("k"))
        self.assertEqual(s.get("k"), (False, None))

    def test_clear_with_prefix(self):
        s = FileCacheStore(self.dir)
        for k in ("db\x00t1\x00a", "db\x00t1\x00b", "db\x00t2\x00c"):
            s.set(k, k)
        self.assertEqual(s.clear("db\x00t1\x00"), 2)
        self.assertEqual(s.get("db\x00t2\x00c"), (True, "db\x00t2\x00c"))
        self.assertEqual(len(s), 1)
        self.assertEqual(s.clear(), 1)
        self.assertEqual(len(s), 0)

    def test_lru_eviction_by_mtime(self):
        s = FileCacheStore(self.dir, max_items=3)
        for k in ("a", "b", "c"):
            s.set(k, k)
            time.sleep(0.02)
        s.get("a")            # 触碰 a 的 mtime，使其晚于 b
        time.sleep(0.02)
        s.set("d", "d")       # 超容量 → 淘汰最旧的 b
        self.assertEqual(s.get("b"), (False, None))
        self.assertEqual(s.get("a"), (True, "a"))
        self.assertEqual(s.get("c"), (True, "c"))
        self.assertEqual(s.get("d"), (True, "d"))

    def test_stats_structure(self):
        s = FileCacheStore(self.dir)
        s.set("k", 1)
        s.get("k")
        s.get("miss")
        st = s.stats()
        self.assertEqual(st["backend"], "file")
        self.assertEqual(st["dir"], self.dir)
        self.assertEqual(st["items"], 1)
        self.assertEqual(st["hits"], 1)
        self.assertEqual(st["misses"], 1)
        self.assertAlmostEqual(st["hit_rate"], 0.5)

    def test_empty_dir_rejected(self):
        with self.assertRaises(InvalidArgumentException):
            FileCacheStore("")
        with self.assertRaises(InvalidArgumentException):
            FileCacheStore("   ")

    def test_dir_auto_created(self):
        import os
        sub = os.path.join(self.dir, "nested", "cache")
        s = FileCacheStore(sub)
        self.assertTrue(os.path.isdir(sub))
        s.set("k", 1)
        self.assertEqual(s.get("k"), (True, 1))

    def test_corrupt_file_tolerated(self):
        import os
        s = FileCacheStore(self.dir)
        s.set("k", 1)
        # 覆盖为损坏内容
        name = [n for n in os.listdir(self.dir) if n.endswith(".json")][0]
        with open(os.path.join(self.dir, name), "w", encoding="utf-8") as fh:
            fh.write("{not json")
        self.assertEqual(s.get("k"), (False, None))  # 容错：当未命中

    def test_cross_instance_persistence(self):
        """同目录新实例读到旧数据 —— 模拟进程重启。"""
        s1 = FileCacheStore(self.dir)
        s1.set("persist", {"rows": [1, 2]}, ttl=60)
        s2 = FileCacheStore(self.dir)   # 新实例（新"进程"）
        self.assertEqual(s2.get("persist"), (True, {"rows": [1, 2]}))


class TestFileCacheWiring(unittest.TestCase):
    """set_config 缓存后端接线（v0.9.1）。"""

    def setUp(self):
        Db.close()
        self.dir = tempfile.mkdtemp(prefix="torm_wiring_")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.addCleanup(Db.close)
        self.addCleanup(cache.set_store, None)

    def test_file_backend_enabled_via_set_config(self):
        Db.set_config({"database": ":memory:",
                       "cache_backend": "file", "cache_dir": self.dir})
        self.assertIsInstance(Db.cache_store(), FileCacheStore)
        Db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
        Db.table("t").insert({"v": "a"})
        self.assertEqual(len(Db.table("t").cache(60).select()), 1)
        st = Db.cache_store().stats()
        self.assertEqual(st["backend"], "file")
        self.assertEqual(st["items"], 1)
        # 第二次查询命中文件缓存
        self.assertEqual(len(Db.table("t").cache(60).select()), 1)
        self.assertEqual(Db.cache_store().stats()["hits"], 1)

    def test_write_invalidates_file_cache(self):
        Db.set_config({"database": ":memory:",
                       "cache_backend": "file", "cache_dir": self.dir})
        Db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
        Db.table("t").insert({"v": "a"})
        self.assertEqual(len(Db.table("t").cache(60).select()), 1)
        Db.table("t").insert({"v": "b"})   # 写失效
        rows = Db.table("t").cache(60).select()
        self.assertEqual(len(rows), 2)

    def test_same_dir_reuses_store(self):
        """重复 set_config 同目录：复用实例，条目不丢。"""
        Db.set_config({"database": ":memory:",
                       "cache_backend": "file", "cache_dir": self.dir})
        s1 = Db.cache_store()
        s1.set("keep", 1)
        Db.set_config({"database": ":memory:",
                       "cache_backend": "file", "cache_dir": self.dir})
        s2 = Db.cache_store()
        self.assertIs(s1, s2)
        self.assertEqual(s2.get("keep"), (True, 1))

    def test_switch_back_to_memory(self):
        Db.set_config({"database": ":memory:",
                       "cache_backend": "file", "cache_dir": self.dir})
        self.assertIsInstance(Db.cache_store(), FileCacheStore)
        Db.set_config({"database": ":memory:", "cache_backend": "memory"})
        self.assertIsInstance(Db.cache_store(), MemoryCacheStore)

    def test_memory_config_keeps_custom_store(self):
        """未传缓存键的 set_config 不打扰自定义后端。"""
        _setup_table()
        custom = MemoryCacheStore()
        old = cache.set_store(custom)
        try:
            Db.set_config({"database": ":memory:"})
            self.assertIs(Db.cache_store(), custom)
        finally:
            cache.set_store(old)

    def test_missing_cache_dir_raises(self):
        with self.assertRaises(InvalidArgumentException):
            Db.set_config({"database": ":memory:", "cache_backend": "file"})

    def test_invalid_backend_raises(self):
        with self.assertRaises(InvalidArgumentException):
            Db.set_config({"database": ":memory:",
                           "cache_backend": "redis", "cache_dir": self.dir})


if __name__ == "__main__":
    unittest.main(verbosity=2)
