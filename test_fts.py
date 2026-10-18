# -*- coding: utf-8 -*-
"""FTS5 全文检索单元测试（v0.10.0）。

覆盖：索引生命周期 / 列推导 / 查询语法 / trigram 中文契约 / 相关度排序 /
高亮与摘要 / 触发器同步 / 事务回滚 / 缓存联动 / 能力边界 / 模型集成。
"""
import unittest

from tinkpyorm import Db, FtsIndex, Model, fts_quote, UnsupportedOperation
from tinkpyorm.exceptions import InvalidArgumentException, QueryError


def _seed():
    """建内容表并写入 3 行样例数据。"""
    Db.close()
    Db.set_config({"database": ":memory:", "sql_log_enabled": False})
    Db.execute("CREATE TABLE docs (id INTEGER PRIMARY KEY, "
               "title TEXT, body TEXT, category TEXT)")
    Db.table("docs").insert_all([
        {"title": "SQLite 全文检索", "body": "FTS5 是 SQLite 的全文检索扩展",
         "category": "tech"},
        {"title": "Python ORM 实战", "body": "查询构造器与模型映射",
         "category": "tech"},
        {"title": "红烧肉教程", "body": "家常红烧肉的做法步骤",
         "category": "life"},
    ])


def _index(**kwargs):
    """默认索引：title 权重 5、body 权重 1，已注册好数据。"""
    _seed()
    options = {"columns": {"title": 5.0, "body": 1.0}}
    options.update(kwargs)
    idx = Db.fts("docs", **options)
    idx.create(rebuild=True)
    return idx


class TestLifecycle(unittest.TestCase):
    """建索引 / 删索引 / 维护命令。"""

    def test_create_is_idempotent(self):
        idx = _index()
        idx.create()                      # 再建一次不应报错
        self.assertEqual(idx.info()["rows"], 3)

    def test_infer_columns_and_rowid(self):
        _seed()
        idx = Db.fts("docs")
        self.assertEqual(idx.columns, [])
        idx.create(rebuild=True)
        self.assertEqual(idx.columns, ["title", "body", "category"])
        self.assertEqual(idx.content_rowid, "id")
        self.assertEqual(idx.info()["mode"], "external")

    def test_drop_removes_table_and_triggers(self):
        idx = _index()
        idx.drop()
        self.assertFalse(idx.info()["exists"])
        rows = Db.get_connection().query(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE ?",
            ("docs_fts_%",))
        self.assertEqual(rows, [])
        idx.drop()                        # 幂等

    def test_rebuild_indexes_rows_inserted_before_create(self):
        _seed()                            # 先有数据、后建索引
        idx = Db.fts("docs", columns=["title", "body"])
        idx.create()                       # 不 rebuild，索引为空
        self.assertEqual(idx.search("全文检索").count(), 0)
        idx.rebuild()
        self.assertEqual(idx.search("全文检索").count(), 1)

    def test_optimize_and_integrity_check(self):
        idx = _index()
        idx.optimize()
        self.assertTrue(idx.integrity_check())

    def test_search_before_create_raises(self):
        _seed()
        idx = Db.fts("docs", columns=["title"])
        with self.assertRaises(QueryError) as ctx:
            idx.search("全文检索")
        self.assertIn("create()", str(ctx.exception))


class TestQuerySyntax(unittest.TestCase):
    """FTS5 查询语法与边界。"""

    def setUp(self):
        self.idx = _index()

    def test_single_term(self):
        self.assertEqual(self.idx.search("红烧肉").count(), 1)

    def test_boolean_or(self):
        self.assertEqual(self.idx.search("SQLite OR 红烧肉").count(), 2)

    def test_boolean_not(self):
        self.assertEqual(self.idx.search("SQLite NOT 红烧肉").count(), 1)

    def test_phrase(self):
        self.assertEqual(self.idx.search('"全文检索"').count(), 1)

    def test_column_filter(self):
        rows = self.idx.search("title:红烧肉").select()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["title"], "红烧肉教程")

    def test_prefix_query(self):
        self.assertEqual(self.idx.search("红烧肉*").count(), 1)

    def test_near_query(self):
        # trigram 下词项同样需 ≥3 字符："全文检" 是合法三字词元
        self.assertEqual(self.idx.search("NEAR(SQLite 全文检, 10)").count(), 1)
        self.assertEqual(self.idx.search("NEAR(SQLite 全文检, 2)").count(), 0)

    def test_two_char_trigram_returns_zero(self):
        """trigram 契约：少于 3 个字符的查询串必然 0 命中。"""
        self.assertEqual(self.idx.search("检索").count(), 0)
        self.assertEqual(self.idx.search("红烧").count(), 0)

    def test_empty_query_rejected(self):
        with self.assertRaises(InvalidArgumentException):
            self.idx.search("   ")

    def test_syntax_error_maps_to_query_error(self):
        with self.assertRaises(QueryError):
            self.idx.search("((((").count()

    def test_special_characters_are_bound_not_executed(self):
        with self.assertRaises(QueryError):
            self.idx.search('"; DROP TABLE docs; --').count()
        self.assertTrue(self.idx.info()["exists"])   # 表仍在

    def test_quote_helper(self):
        self.assertEqual(fts_quote("C++ 入门"), '"C++ 入门"')
        self.assertEqual(fts_quote('a"b'), '"a""b"')
        self.assertEqual(self.idx.search(fts_quote("红烧肉")).count(), 1)


class TestRanking(unittest.TestCase):

    def setUp(self):
        self.idx = _index()

    def test_rank_orders_by_bm25(self):
        rows = self.idx.search("全文检索 OR 模型映射").rank().select()
        self.assertGreater(len(rows), 0)
        scores = [r["_score"] for r in rows]
        self.assertEqual(scores, sorted(scores))      # bm25 越小越相关

    def test_column_weight_affects_order(self):
        # title 权重 5 > body 权重 1，标题命中应排在正文命中前
        rows = self.idx.search("红烧肉").rank().select()
        self.assertEqual(rows[0]["title"], "红烧肉教程")

    def test_reverse_rank(self):
        rows = self.idx.search("全文检索 OR 模型映射").rank(reverse=True).select()
        scores = [r["_score"] for r in rows]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_order_by_column_overrides_rank(self):
        rows = self.idx.search("SQLite OR 红烧肉").order("title", "desc").select()
        titles = [r["title"] for r in rows]
        self.assertEqual(titles, sorted(titles, reverse=True))


class TestHighlight(unittest.TestCase):

    def setUp(self):
        self.idx = _index()

    def test_highlight_marks_term(self):
        row = self.idx.search("全文检索").highlight("body").select()[0]
        self.assertIn("<em>全文检索</em>", row["_highlight"])

    def test_highlight_custom_markers(self):
        row = self.idx.search("全文检索").highlight("body", "[", "]").select()[0]
        self.assertIn("[全文检索]", row["_highlight"])

    def test_highlight_marker_is_escaped(self):
        row = self.idx.search("全文检索").highlight("body", "'", "'").select()[0]
        self.assertIn("'全文检索'", row["_highlight"])

    def test_snippet_truncates(self):
        row = self.idx.search("全文检索").snippet("body", tokens=4).select()[0]
        self.assertIn("<em>全文检索</em>", row["_snippet"])

    def test_highlight_unknown_column_raises(self):
        with self.assertRaises(InvalidArgumentException):
            self.idx.search("全文检索").highlight("nope").select()


class TestSync(unittest.TestCase):
    """外模式触发器：主表增删改即时同步到索引。"""

    def setUp(self):
        self.idx = _index()

    def test_insert_syncs(self):
        Db.table("docs").insert({"title": "向量检索入门", "body": "索引结构",
                                 "category": "tech"})
        self.assertEqual(self.idx.search("向量检索").count(), 1)

    def test_update_syncs(self):
        Db.table("docs").where("id", 3).update(
            {"title": "清蒸鱼教程", "body": "清蒸鱼的做法"})
        self.assertEqual(self.idx.search("红烧肉").count(), 0)
        self.assertEqual(self.idx.search("清蒸鱼").count(), 1)

    def test_delete_syncs(self):
        Db.table("docs").where("id", 3).delete()
        self.assertEqual(self.idx.search("红烧肉").count(), 0)

    def test_rollback_keeps_index_consistent(self):
        conn = Db.get_connection()
        with self.assertRaises(RuntimeError):
            with conn.transaction():
                Db.table("docs").insert({"title": "事务回滚行", "body": "回滚测试",
                                         "category": "tmp"})
                raise RuntimeError("boom")
        self.assertEqual(self.idx.search("事务回滚行").count(), 0)

    def test_insert_all_then_rebuild(self):
        Db.table("docs").insert_all([
            {"title": "批量甲", "body": "批量数据一", "category": "bulk"},
            {"title": "批量乙", "body": "批量数据二", "category": "bulk"},
        ])
        self.idx.rebuild()
        self.assertEqual(self.idx.search("批量数据").count(), 2)


class TestContentFilters(unittest.TestCase):

    def setUp(self):
        self.idx = _index()

    def test_where_on_content_table(self):
        rows = self.idx.search("SQLite OR 红烧肉").where(
            "category", "tech").select()
        self.assertEqual([r["title"] for r in rows], ["SQLite 全文检索"])

    def test_where_with_dict(self):
        rows = self.idx.search("SQLite OR 红烧肉").where(
            {"category": "life"}).select()
        self.assertEqual(len(rows), 1)

    def test_standalone_rejects_where(self):
        s = Db.fts("notes", ["title", "body"], standalone=True)
        s.create()
        with self.assertRaises(QueryError):
            s.search("独立").where("x", 1)


class TestCacheInvalidation(unittest.TestCase):

    def setUp(self):
        self.idx = _index()

    def test_write_invalidates_fts_cache(self):
        self.assertEqual(self.idx.search("红烧肉").cache(60).count(), 1)
        Db.table("docs").where("id", 3).update(
            {"title": "清蒸鱼教程", "body": "清蒸鱼的做法"})
        self.assertEqual(self.idx.search("红烧肉").cache(60).count(), 0)

    def test_cache_hit_avoids_second_query(self):
        self.idx.search("红烧肉").cache(60).select()
        before = len(Db.get_sql_log())
        self.idx.search("红烧肉").cache(60).select()
        self.assertEqual(len(Db.get_sql_log()), before)


class TestStandalone(unittest.TestCase):

    def test_standalone_index_roundtrip(self):
        Db.close()
        Db.set_config({"database": ":memory:", "sql_log_enabled": False})
        idx = Db.fts("notes", ["title", "body"], standalone=True)
        idx.create()
        Db.execute("INSERT INTO notes_fts(title, body) VALUES (?, ?)",
                   ("独立索引", "不依赖主表"))
        rows = idx.search("独立索引").select()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["title"], "独立索引")
        self.assertEqual(idx.info()["mode"], "standalone")

    def test_standalone_requires_columns(self):
        Db.close()
        Db.set_config({"database": ":memory:"})
        idx = Db.fts("notes", standalone=True)
        with self.assertRaises(InvalidArgumentException):
            idx.create()


class TestAdvancedInvalid(unittest.TestCase):

    def test_prefix_becomes_prefix_option(self):
        idx = _index(prefix=(2, 3))
        self.assertEqual(list(idx.prefix), [2, 3])
        self.assertEqual(idx.search("红烧肉").count(), 1)

    def test_invalid_index_name(self):
        with self.assertRaises(InvalidArgumentException):
            Db.fts("bad name;")

    def test_invalid_tokenize(self):
        with self.assertRaises(InvalidArgumentException):
            Db.fts("docs", tokenize="trigram'; DROP TABLE docs; --")

    def test_invalid_prefix(self):
        with self.assertRaises(InvalidArgumentException):
            Db.fts("docs", prefix=(0,))

    def test_missing_content_table(self):
        Db.close()
        Db.set_config({"database": ":memory:"})
        idx = Db.fts("nope", columns=["a"])
        with self.assertRaises(QueryError):
            idx.create()

    def test_missing_index_column(self):
        _seed()
        idx = Db.fts("docs", columns=["title", "nope"])
        with self.assertRaises(QueryError):
            idx.create()

    def test_unsupported_driver_rejected(self):
        from tinkpyorm import Connection, SQLiteDriver, register_driver
        from tinkpyorm.config import Config
        from tinkpyorm.drivers import _DRIVERS

        class _NoFtsDriver(SQLiteDriver):
            name = "no_fts"
            supports_fts = False

        register_driver("no_fts")(_NoFtsDriver)
        self.addCleanup(_DRIVERS.pop, "no_fts", None)
        conn = Connection.from_config(Config(database=":memory:", type="no_fts"))
        with self.assertRaises(UnsupportedOperation):
            FtsIndex(conn, "docs")


class TestModelIntegration(unittest.TestCase):

    def test_model_search_returns_instances(self):
        _index()

        class Doc(Model):
            __table__ = "docs"
            __pk__ = "id"
            __fts__ = {"columns": {"title": 5.0, "body": 1.0}}

        rows = Doc.search("红烧肉").select()
        self.assertEqual(len(rows), 1)
        self.assertIsInstance(rows[0], Doc)
        self.assertEqual(rows[0].title, "红烧肉教程")

    def test_model_without_fts_raises(self):
        class Plain(Model):
            __table__ = "plain"

        with self.assertRaises(QueryError):
            Plain.search("x")

    def test_model_fts_index_lifecycle(self):
        _seed()

        class Doc2(Model):
            __table__ = "docs"
            __pk__ = "id"
            __fts__ = {"columns": ["title", "body"]}

        Doc2.fts_index().create(rebuild=True)
        self.assertEqual(Doc2.search("全文检索").count(), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
