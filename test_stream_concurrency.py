# -*- coding: utf-8 -*-
"""流式读取与并发安全测试（v0.4.0）。

覆盖要点：
- ``chunk()``：分块大小、整除边界、空结果、不改动原 Query、模型集合
- ``cursor()``：真流式逐行产出、大结果集、模型转换、fetch_sql 拒绝
- 连接级线程安全：多线程事务一致性、读写混合、thread_safe=False 兼容
"""
import os
import sys
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from tinkpyorm import Connection, Db, Model
from tinkpyorm.exceptions import InvalidArgumentException, QueryError


def _setup(rows: int = 10) -> None:
    """重建内存库与测试表（批量插入 rows 行）。"""
    Db.close()          # 释放上一用例的连接，避免测试间资源泄漏
    Db.set_config({"database": ":memory:"})
    Db.execute("CREATE TABLE big (id INTEGER PRIMARY KEY, name TEXT, n INTEGER)")
    if rows:
        Db.table("big").insert_all(
            [{"name": "u%d" % i, "n": i} for i in range(rows)])
    Db.clear_cache()
    Db.sql_log_clear()


class Big(Model):
    __table__ = "big"


class TestChunk(unittest.TestCase):
    """分块读取（每批独立查询，线程安全）。"""

    def setUp(self):
        _setup(10)

    def test_chunk_sizes(self):
        sizes = [len(b) for b in Db.table("big").order("id").chunk(4)]
        self.assertEqual(sizes, [4, 4, 2])

    def test_chunk_exact_multiple_has_no_trailing_empty_batch(self):
        sizes = [len(b) for b in Db.table("big").order("id").chunk(5)]
        self.assertEqual(sizes, [5, 5])

    def test_chunk_covers_all_rows_in_order(self):
        ids = [r["id"] for b in Db.table("big").order("id").chunk(3) for r in b]
        self.assertEqual(ids, list(range(1, 11)))

    def test_chunk_respects_where(self):
        ids = [r["id"] for b in Db.table("big").where("n", "<", 3).order("id").chunk(2)
               for r in b]
        self.assertEqual(ids, [1, 2, 3])

    def test_chunk_empty_result(self):
        self.assertEqual(list(Db.table("big").where("id", 999).chunk(3)), [])

    def test_chunk_does_not_mutate_source_query(self):
        q = Db.table("big").order("id")
        list(q.chunk(4))
        self.assertIsNone(q.options["limit"], "chunk 不应改动原 Query 的 limit")

    def test_chunk_returns_models_when_bound(self):
        batches = list(Big.order("id").chunk(4))
        self.assertEqual([len(b) for b in batches], [4, 4, 2])
        for batch in batches:
            for item in batch:
                self.assertIsInstance(item, Big)

    def test_chunk_size_invalid(self):
        with self.assertRaises(InvalidArgumentException):
            list(Db.table("big").chunk(0))


class TestCursor(unittest.TestCase):
    """真流式逐行读取（fetchmany）。"""

    def setUp(self):
        _setup(500)

    def test_cursor_streams_all_rows(self):
        rows = list(Db.table("big").order("id").cursor(chunk_size=64))
        self.assertEqual(len(rows), 500)
        self.assertEqual(rows[0]["id"], 1)
        self.assertEqual(rows[-1]["id"], 500)

    def test_cursor_yields_dict_one_by_one(self):
        it = Db.table("big").order("id").cursor(chunk_size=10)
        first = next(it)
        self.assertIsInstance(first, dict)
        self.assertEqual(first["name"], "u0")

    def test_cursor_respects_where_and_order(self):
        rows = list(Db.table("big").where("n", "<", 10).order("n").cursor())
        self.assertEqual([r["n"] for r in rows], list(range(10)))

    def test_cursor_returns_models_when_bound(self):
        rows = list(Big.order("id").cursor(chunk_size=100))
        self.assertEqual(len(rows), 500)
        self.assertIsInstance(rows[0], Big)

    def test_cursor_fetch_sql_raises(self):
        with self.assertRaises(QueryError):
            list(Db.table("big").fetch_sql().cursor())

    def test_cursor_chunk_size_invalid(self):
        with self.assertRaises(InvalidArgumentException):
            list(Db.table("big").cursor(0))

    def test_cursor_large_dataset(self):
        """2 万行流式求和：验证分块读取路径的正确性（内存恒定）。"""
        _setup(20000)
        total = 0
        count = 0
        for row in Db.table("big").cursor(chunk_size=1000):
            total += row["n"]
            count += 1
        self.assertEqual(count, 20000)
        self.assertEqual(total, sum(range(20000)))


class TestConcurrency(unittest.TestCase):
    """连接级线程安全（RLock 串行化 + 事务状态保护）。"""

    def test_threaded_transactions_are_consistent(self):
        """4 线程 × 50 事务：不应丢写、不应报错。"""
        _setup(0)
        errors = []

        def worker(n: int):
            try:
                for i in range(50):
                    with Db.transaction():
                        Db.table("big").insert({"name": "t%d-%d" % (n, i), "n": i})
            except Exception as exc:  # pragma: no cover - 失败时记录
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(k,)) for k in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [], "并发事务不应抛出异常")
        self.assertEqual(Db.table("big").count(), 200)

    def test_threaded_mixed_read_write(self):
        """2 写 × 2 读并发：计数必须精确，说明事务状态未被交叉覆盖。"""
        _setup(0)
        Db.table("big").insert({"name": "counter", "n": 0})
        errors = []

        def writer():
            try:
                for _ in range(100):
                    with Db.transaction():
                        Db.table("big").where("id", 1).inc("n")
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        def reader():
            try:
                for _ in range(100):
                    Db.table("big").find(1)
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=writer) for _ in range(2)] + \
                  [threading.Thread(target=reader) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        self.assertEqual(Db.table("big").find(1)["n"], 200)

    def test_rollback_isolated_between_threads(self):
        """一个线程回滚不影响其它线程已提交的数据。"""
        _setup(0)
        done = []

        def failing():
            try:
                with Db.transaction():
                    Db.table("big").insert({"name": "ghost", "n": -1})
                    raise RuntimeError("boom")
            except RuntimeError:
                done.append("rolled back")

        def succeeding():
            with Db.transaction():
                Db.table("big").insert({"name": "kept", "n": 1})
            done.append("committed")

        t1 = threading.Thread(target=failing)
        t2 = threading.Thread(target=succeeding)
        t1.start(); t1.join()
        t2.start(); t2.join()

        self.assertIn("rolled back", done)
        self.assertIn("committed", done)
        names = [r["name"] for r in Db.table("big").select()]
        self.assertIn("kept", names)
        self.assertNotIn("ghost", names)

    def test_connection_lock_enabled_by_default(self):
        conn = Connection(":memory:")
        try:
            self.assertIsNotNone(conn._lock)
            self.assertTrue(conn._thread_safe)
        finally:
            conn.close()

    def test_thread_safe_can_be_disabled(self):
        conn = Connection(":memory:", thread_safe=False)
        try:
            self.assertIsNone(conn._lock)
            conn.execute("CREATE TABLE t (a INTEGER)")
            conn.insert("INSERT INTO t (a) VALUES (?)", (1,))
            self.assertEqual(len(conn.query("SELECT * FROM t")), 1)
        finally:
            conn.close()

    def test_transaction_inside_lock_is_reentrant(self):
        """事务内继续执行 SQL 不应死锁（RLock 可重入）。"""
        _setup(0)
        with Db.transaction():
            Db.table("big").insert({"name": "a", "n": 1})
            with Db.transaction():          # 嵌套 -> SAVEPOINT
                Db.table("big").insert({"name": "b", "n": 2})
            self.assertEqual(Db.table("big").count(), 2)
        self.assertEqual(Db.table("big").count(), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
