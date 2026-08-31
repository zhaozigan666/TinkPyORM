# -*- coding: utf-8 -*-
"""thinkorm 完整测试套件（标准库 unittest）。"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from thinkorm import (
    Db, Model, Collection, Paginator, Raw, raw,
    DataNotFound, QueryError, RelationNotFound,
)
from thinkorm.connection import Connection

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_test.db")
if os.path.exists(DB_PATH):
    os.remove(DB_PATH)


def make_schema():
    Db.execute("DROP TABLE IF EXISTS user")
    Db.execute("DROP TABLE IF EXISTS article")
    Db.execute("DROP TABLE IF EXISTS role")
    Db.execute("DROP TABLE IF EXISTS user_role")
    Db.execute("DROP TABLE IF EXISTS soft_user")
    Db.execute("""
        CREATE TABLE user (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            email TEXT,
            age INTEGER DEFAULT 0,
            status INTEGER DEFAULT 1,
            balance REAL DEFAULT 0,
            tags TEXT,
            create_time TEXT,
            update_time TEXT
        )
    """)
    Db.execute("""
        CREATE TABLE article (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            title TEXT,
            content TEXT
        )
    """)
    Db.execute("CREATE TABLE role (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT)")
    Db.execute("CREATE TABLE user_role (user_id INTEGER, role_id INTEGER)")
    Db.execute("""
        CREATE TABLE soft_user (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT,
            delete_time TEXT
        )
    """)


class User(Model):
    __table__ = "user"
    __timestamps__ = True
    __type__ = {"tags": "json"}
    __json__ = ["tags"]

    def articles(self):
        return self.has_many("Article", "user_id")

    def roles(self):
        return self.belongs_to_many("Role", "user_role", "user_id", "role_id")

    def get_name_attr(self, value):
        return value.upper() if value else value

    def set_name_attr(self, value):
        return value.strip()

    def scope_active(self, query):
        query.where("status", 1)


class Article(Model):
    __table__ = "article"

    def user(self):
        return self.belongs_to("User", "user_id")


class Role(Model):
    __table__ = "role"

    def users(self):
        return self.belongs_to_many("User", "user_role", "role_id", "user_id")


class SoftUser(Model):
    __table__ = "soft_user"
    __soft_delete__ = "delete_time"


class EventUser(Model):
    __table__ = "user"
    __events__ = []

    def on_before_insert(self):
        EventUser.__events__.append("before_insert")

    def on_after_insert(self):
        EventUser.__events__.append("after_insert")

    def on_before_update(self):
        EventUser.__events__.append("before_update")

    def on_after_update(self):
        EventUser.__events__.append("after_update")

    def on_before_delete(self):
        EventUser.__events__.append("before_delete")

    def on_after_delete(self):
        EventUser.__events__.append("after_delete")


class TestQueryBuilder(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        Db.set_config(Connection(DB_PATH))
        make_schema()
        cls._seed()

    @classmethod
    def _seed(cls):
        Db.table("user").insert_all([
            {"name": "thinkphp", "email": "a@qq.com", "age": 30, "balance": 100.5},
            {"name": "topthink", "email": "b@qq.com", "age": 25, "balance": 50},
            {"name": "alice", "email": "c@qq.com", "age": 20, "balance": 200},
            {"name": "bob", "email": "d@qq.com", "age": 35, "balance": 10},
        ])
        Db.table("article").insert_all([
            {"user_id": 1, "title": "t1", "content": "c1"},
            {"user_id": 1, "title": "t2", "content": "c2"},
            {"user_id": 2, "title": "t3", "content": "c3"},
        ])

    # ----- where 形式 ----- #
    def test_where_forms(self):
        self.assertEqual(Db.table("user").where("id", 1).value("name"), "thinkphp")
        self.assertEqual(Db.table("user").where("id", ">", 2).count(), 2)
        self.assertEqual(Db.table("user").where(id=1, status=1).count(), 1)
        self.assertEqual(Db.table("user").where({"age": 20}).value("name"), "alice")
        self.assertEqual(Db.table("user").where("age", [20, 25, 30]).count(), 3)
        self.assertEqual(Db.table("user").where("age", None).count(), 0)
        # 闭包
        q = Db.table("user").where(lambda s: s.where("age", ">", 20).where_or("name", "alice"))
        self.assertEqual(q.count(), 4)  # 30/25/35 + alice(20)
        # whereOr
        self.assertEqual(Db.table("user").where("age", ">", 30).where_or("age", "<", 21).count(), 2)

    def test_where_special(self):
        self.assertEqual(Db.table("user").where_null("email").count(), 0)
        self.assertEqual(Db.table("user").where_not_null("email").count(), 4)
        self.assertEqual(Db.table("user").where_in("id", [1, 3]).count(), 2)
        self.assertEqual(Db.table("user").where_not_in("id", [1, 3]).count(), 2)
        self.assertEqual(Db.table("user").where_like("name", "top%").count(), 1)
        self.assertEqual(Db.table("user").where_between("age", [20, 30]).count(), 3)
        self.assertEqual(Db.table("user").where_not_between("age", [20, 30]).count(), 1)
        self.assertEqual(Db.table("user").where_column("age", ">", "balance").count(), 1)  # bob 35 > 10
        self.assertEqual(Db.table("user").where_raw("age > ?", [30]).count(), 1)

    def test_where_exists(self):
        n = Db.table("user").where_exists(lambda s: s.table("article").where_column("article.user_id", "=", "user.id")).count()
        self.assertEqual(n, 2)  # id 1、2 有文章

    # ----- 链式与结果 ----- #
    def test_field_order_limit_page(self):
        rows = Db.table("user").field("id,name").order("age", "desc").limit(2).select()
        self.assertEqual([r["name"] for r in rows], ["bob", "thinkphp"])
        rows = Db.table("user").field({"uid": "id", "uname": "name"}).limit(2).select()
        self.assertIn("uid", rows[0])
        self.assertNotIn("id", rows[0])
        rows = Db.table("user").without_field("email", "balance").limit(1).select()
        self.assertNotIn("email", rows[0])
        page = Db.table("user").page(2, 2).select()
        self.assertEqual(len(page), 2)

    def test_aggregate(self):
        self.assertEqual(Db.table("user").count(), 4)
        self.assertEqual(Db.table("user").sum("age"), 110)
        self.assertAlmostEqual(Db.table("user").avg("age"), 27.5)
        self.assertEqual(Db.table("user").max("age"), 35)
        self.assertEqual(Db.table("user").min("age"), 20)

    def test_group_having_union_distinct(self):
        stats = Db.table("user").field("status").group("status").having("COUNT(*)", ">", 0).select()
        self.assertEqual(len(stats), 1)
        u = Db.table("user").field("age").union(lambda s: s.table("user").field("age").where("age", 20)).select()
        self.assertEqual(len(u), 4)  # UNION 去重：30/25/20/35
        ua = Db.table("user").field("age").union_all(lambda s: s.table("user").field("age").where("age", 20)).select()
        self.assertEqual(len(ua), 5)  # UNION ALL 不去重：多一行 20
        d = Db.table("user").field("status").distinct().select()
        self.assertEqual(len(d), 1)

    def test_join(self):
        rows = Db.table("user u").join("article a", lambda s: s.where_column("a.user_id", "=", "u.id")) \
            .field("u.name", "a.title").order("u.id").select()
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]["name"], "thinkphp")
        rows = Db.table("user u").left_join("article a", lambda s: s.where_column("a.user_id", "=", "u.id")) \
            .field("u.name").count()
        self.assertEqual(rows, 5)  # 4 用户，id=1 有两篇文章展开 2 行

    def test_find_variants(self):
        self.assertEqual(Db.table("user").find(1)["name"], "thinkphp")
        self.assertIsNone(Db.table("user").where("id", 9999).find())
        self.assertIsNotNone(Db.table("user").allow_empty().where("id", 9999).find())
        with self.assertRaises(DataNotFound):
            Db.table("user").fail_exception().where("id", 9999).find()

    def test_value_column(self):
        self.assertEqual(Db.table("user").where("id", 1).value("name"), "thinkphp")
        self.assertEqual(Db.table("user").where("id", 9999).value("name", "default"), "default")
        cols = Db.table("user").column("name")
        self.assertEqual(len(cols), 4)
        keyed = Db.table("user").column("name", "id")
        self.assertEqual(keyed[1], "thinkphp")

    def test_raw_and_sql_log(self):
        rows = Db.table("user").where("balance", ">", raw("50")).select()
        self.assertEqual(len(rows), 2)
        Db.table("user").where("id", 1).fetch_sql(True)
        self.assertTrue(Db.get_sql_log())
        Db.sql_log_clear()
        self.assertEqual(len(Db.get_sql_log()), 0)

    def test_cache(self):
        n = Db.table("user").cache("cache_key", 60).count()
        self.assertEqual(n, 4)
        n2 = Db.table("user").cache("cache_key", 60).count()
        self.assertEqual(n2, 4)

    def test_paginate(self):
        p = Db.table("user").paginate(2, 1)
        self.assertIsInstance(p, Paginator)
        self.assertEqual(p.total, 4)
        self.assertEqual(p.last_page, 2)
        self.assertEqual(len(p.items), 2)
        self.assertTrue(p.has_more)
        p2 = Db.table("user").paginate(2, 2)
        self.assertFalse(p2.has_more)


class TestWrite(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        Db.set_config(Connection(DB_PATH))
        make_schema()

    def test_insert_update_delete(self):
        uid = Db.table("user").insert({"name": "ins", "age": 1})
        self.assertGreater(uid, 0)
        n = Db.table("user").where("id", uid).update({"age": 2})
        self.assertEqual(n, 1)
        self.assertEqual(Db.table("user").where("id", uid).value("age"), 2)
        n = Db.table("user").where("id", uid).inc("age", 3)
        self.assertEqual(Db.table("user").where("id", uid).value("age"), 5)
        Db.table("user").where("id", uid).dec("age", 1)
        self.assertEqual(Db.table("user").where("id", uid).value("age"), 4)
        n = Db.table("user").where("id", uid).delete()
        self.assertEqual(n, 1)
        self.assertIsNone(Db.table("user").where("id", uid).find())

    def test_update_requires_where(self):
        with self.assertRaises(QueryError):
            Db.table("user").update({"age": 1})

    def test_insert_all(self):
        n = Db.table("user").insert_all([{"name": "a1"}, {"name": "a2"}, {"name": "a3"}])
        self.assertEqual(n, 3)
        self.assertEqual(Db.table("user").count(), 3)


class TestTransaction(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        Db.set_config(Connection(DB_PATH))
        make_schema()

    def test_transaction_callback(self):
        def work():
            Db.table("user").insert({"name": "tx1"})
            Db.table("user").insert({"name": "tx2"})
        Db.transaction(work)
        self.assertEqual(Db.table("user").count(), 2)

    def test_transaction_rollback(self):
        try:
            with Db.transaction():
                Db.table("user").insert({"name": "tx3"})
                raise RuntimeError("boom")
        except RuntimeError:
            pass
        self.assertEqual(Db.table("user").where("name", "tx3").count(), 0)

    def test_transaction_nested_savepoint(self):
        try:
            with Db.transaction():
                Db.table("user").insert({"name": "outer"})
                try:
                    with Db.transaction():
                        Db.table("user").insert({"name": "inner"})
                        raise RuntimeError("inner fail")
                except RuntimeError:
                    pass
                Db.table("user").insert({"name": "after"})
        except RuntimeError:
            self.fail("外层事务不应失败")
        self.assertEqual(Db.table("user").where("name", "outer").count(), 1)
        self.assertEqual(Db.table("user").where("name", "inner").count(), 0)
        self.assertEqual(Db.table("user").where("name", "after").count(), 1)


class TestModel(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        Db.set_config(Connection(DB_PATH))
        make_schema()

    def test_create_find_update_delete(self):
        u = User.create({"name": "model1", "age": 30, "tags": ["x", "y"]})
        self.assertIsNotNone(u.id)
        self.assertIsNotNone(u.create_time)
        found = User.find(u.id)
        self.assertEqual(found.tags, ["x", "y"])      # json 反序列化
        self.assertEqual(found.name, "MODEL1")         # 获取器
        found.name = "  renamed  "
        found.save()
        self.assertEqual(User.find(u.id).name, "RENAMED")  # 修改器 strip + 获取器 upper
        n = User.destroy(u.id)
        self.assertEqual(n, 1)
        self.assertIsNone(User.find(u.id))

    def test_static_update_destroy(self):
        u = User.create({"name": "s1"})
        User.update({"age": 88}, {"id": u.id})
        self.assertEqual(User.find(u.id).age, 88)
        User.destroy([u.id])
        self.assertIsNone(User.find(u.id))

    def test_scope_and_camel(self):
        User.create({"name": "on", "status": 1})
        User.create({"name": "off", "status": 0})
        self.assertEqual(User.active().count(), 1)
        self.assertEqual(User.whereIn("name", ["on", "off"]).count(), 2)
        self.assertEqual(User.findOrEmpty(99999).id, None)
        with self.assertRaises(DataNotFound):
            User.findOrFail(99999)

    def test_events(self):
        EventUser.__events__.clear()
        u = EventUser.create({"name": "ev"})
        u.name = "ev2"
        u.save()
        u.delete()
        self.assertIn("before_insert", EventUser.__events__)
        self.assertIn("after_insert", EventUser.__events__)
        self.assertIn("before_update", EventUser.__events__)
        self.assertIn("after_update", EventUser.__events__)
        self.assertIn("before_delete", EventUser.__events__)
        self.assertIn("after_delete", EventUser.__events__)


class TestSoftDelete(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        Db.set_config(Connection(DB_PATH))
        make_schema()

    def test_soft_delete(self):
        u = SoftUser.create({"name": "sd"})
        n = u.delete()
        self.assertEqual(n, 1)
        self.assertIsNone(SoftUser.find(u.id))                      # 默认过滤
        self.assertEqual(SoftUser.with_trashed().where("id", u.id).count(), 1)
        self.assertEqual(SoftUser.only_trashed().count(), 1)        # 只剩软删的
        # 恢复
        u2 = SoftUser.with_trashed().where("id", u.id).find()
        self.assertIsNotNone(u2)
        u2.restore()
        self.assertIsNotNone(SoftUser.find(u.id))
        # destroy 批量软删（此时 sd 已恢复，共 3 条可见记录）
        SoftUser.create({"name": "a"})
        SoftUser.create({"name": "b"})
        SoftUser.destroy([x.id for x in SoftUser.select()])
        self.assertEqual(SoftUser.count(), 0)
        self.assertEqual(SoftUser.only_trashed().count(), 3)


class TestRelation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        Db.set_config(Connection(DB_PATH))
        make_schema()
        u1 = User.create({"name": "rel1"})
        u2 = User.create({"name": "rel2"})
        Article.create({"user_id": u1.id, "title": "a1"})
        Article.create({"user_id": u1.id, "title": "a2"})
        Article.create({"user_id": u2.id, "title": "a3"})
        r1 = Role.create({"name": "admin"})
        r2 = Role.create({"name": "editor"})
        Db.table("user_role").insert_all([
            {"user_id": u1.id, "role_id": r1.id},
            {"user_id": u1.id, "role_id": r2.id},
            {"user_id": u2.id, "role_id": r1.id},
        ])

    def test_lazy_has_many(self):
        u = User.where("name", "rel1").find()
        arts = u.articles
        self.assertEqual(len(arts), 2)
        self.assertIsInstance(arts, Collection)

    def test_lazy_belongs_to(self):
        a = Article.where("title", "a3").find()
        self.assertEqual(a.user.name, "REL2")

    def test_lazy_belongs_to_many(self):
        u = User.where("name", "rel1").find()
        roles = u.roles
        self.assertEqual(len(roles), 2)
        self.assertIn("admin", [r.name for r in roles])

    def test_eager_with(self):
        users = User.where_in("name", ["rel1", "rel2"]).with_("articles").select()
        by_name = {u.name: u for u in users}  # 读取时获取器转大写 → REL1/REL2
        self.assertEqual(len(by_name["REL1"].articles), 2)
        self.assertEqual(len(by_name["REL2"].articles), 1)

    def test_eager_with_closure(self):
        users = User.with_({"articles": lambda q: q.where("title", "a1")}).where("name", "rel1").select()
        self.assertEqual(len(users[0].articles), 1)

    def test_eager_nested(self):
        # user -> articles -> user（嵌套）
        users = User.with_("articles.user").where("name", "rel1").select()
        self.assertEqual(users[0].articles[0].user.name, "REL1")

    def test_together_save(self):
        nu = User()
        nu.name = "together"
        nu.together(["articles"])
        nu.articles = [Article(title="c1"), Article(title="c2")]
        nu.save()
        self.assertEqual(Article.where("user_id", nu.id).count(), 2)

    def test_to_dict_with_relation(self):
        u = User.where("name", "rel1").with_("roles").find()
        d = u.to_dict()
        self.assertIn("roles", d)
        self.assertEqual(len(d["roles"]), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
