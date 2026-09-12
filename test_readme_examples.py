# -*- coding: utf-8 -*-
"""README 示例回归测试。

逐条执行 README.md 中给出的用法示例，确保文档与实现始终一致。
直接运行：``python test_readme_examples.py``（全部通过时退出码 0）。
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from tinkpyorm import Db, Model, Collection, raw, DataNotFound, QueryError

tmp = tempfile.mkdtemp()
DB = os.path.join(tmp, "app.db")
ok, fail = 0, 0


def ck(label, fn, expect=None):
    global ok, fail
    try:
        r = fn()
        if expect is not None and r != expect:
            print("FAIL %s -> got %r, expect %r" % (label, r, expect))
            fail += 1
        else:
            ok += 1
    except Exception as e:
        print("FAIL %s -> %s: %s" % (label, type(e).__name__, str(e)[:120]))
        fail += 1


# ---------- 快速开始 ----------
Db.set_config({"database": DB, "timeout": 5})
Db.execute("""CREATE TABLE IF NOT EXISTS user (
    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, age INTEGER DEFAULT 0,
    status INTEGER DEFAULT 1, balance REAL DEFAULT 0, password TEXT, tags TEXT,
    create_time TEXT, update_time TEXT, delete_time TEXT, is_vip INTEGER,
    score REAL, birthday TEXT, login_at TEXT, slug TEXT)""")
Db.execute("CREATE TABLE IF NOT EXISTS article (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, title TEXT, content TEXT, status INTEGER DEFAULT 1)")
Db.execute("CREATE TABLE IF NOT EXISTS profile (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, bio TEXT)")
Db.execute("CREATE TABLE IF NOT EXISTS role (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT)")
Db.execute("CREATE TABLE IF NOT EXISTS user_role (user_id INTEGER, role_id INTEGER)")

ck("name() 自动加前缀", lambda: Db.name("user").count(), 0)
ck("table_exists", lambda: Db.get_connection().table_exists("user"), True)
ck("table_fields", lambda: "name" in Db.get_connection().table_fields("user"), True)

Db.name("user").insert_all([
    {"name": "thinkphp", "age": 30, "status": 1, "balance": 100.5},
    {"name": "topthink", "age": 25, "status": 1, "balance": 50},
    {"name": "alice", "age": 20, "status": 0, "balance": 200},
    {"name": "bob", "age": 35, "status": 1, "balance": 10},
])
ck("count", lambda: Db.name("user").count(), 4)
ck("基本 where 链", lambda: [u["name"] for u in Db.name("user").where("status", 1).where("age", ">", 18).order("id", "desc").select()],
   ["bob", "topthink", "thinkphp"])


# ---------- 模型 ----------
class User(Model):
    __table__ = "user"
    __timestamps__ = True
    __type__ = {"age": "int", "score": "float", "is_vip": "bool",
                "birthday": "date", "login_at": "datetime", "tags": "json"}

    def articles(self):
        return self.has_many("Article", "user_id")

    def profile(self):
        return self.has_one("Profile", "user_id")

    def roles(self):
        return self.belongs_to_many("Role", "user_role", "user_id", "role_id")

    def scope_active(self, q):
        q.where("status", 1)

    @classmethod
    def scope_adult(cls, q):
        q.where("age", ">=", 18)

    def get_name_attr(self, v):
        return v.upper() if v else v

    def set_name_attr(self, v):
        return v.strip()


class Article(Model):
    __table__ = "article"

    def user(self):
        return self.belongs_to("User", "user_id")


class Profile(Model):
    __table__ = "profile"


class Role(Model):
    __table__ = "role"


u = User.create({"name": " alice ", "age": 20, "tags": ["a", "b"], "is_vip": 1,
                 "score": 9.5, "birthday": "2000-01-02", "login_at": "2026-01-01 10:00:00"})
ck("create 返回 id", lambda: u.id > 0, True)
ck("修改器 strip", lambda: Db.name("user").where("id", u.id).value("name"), "alice")
ck("获取器 upper", lambda: User.find(u.id).name, "ALICE")
ck("json 读取", lambda: User.find(u.id).tags, ["a", "b"])
ck("bool 读取", lambda: User.find(u.id).is_vip, True)
ck("date 读取", lambda: str(User.find(u.id).birthday), "2000-01-02")
ck("自动时间戳", lambda: User.find(u.id).create_time is not None, True)
ck("to_dict", lambda: "name" in User.find(u.id).to_dict(), True)
ck("to_json 含 date", lambda: User.find(u.id).to_json()[:1], "{")
ck("to_json indent", lambda: "\n" in User.find(u.id).to_json(indent=2), True)
ck("hidden", lambda: "name" not in User.find(u.id).hidden("name").to_dict(), True)
ck("visible", lambda: sorted(User.find(u.id).visible("id", "name").to_dict().keys()), ["id", "name"])
ck("Model(**kw)", lambda: User(name="x", age=1).to_dict()["name"], "X")
ck("set_attrs", lambda: User().set_attrs({"name": "y"}).get_attr("name"), "Y")

# scope 链式
User.create({"name": "kid", "age": 10, "status": 1})
User.create({"name": "off", "age": 30, "status": 0})
ck("scope 类入口", lambda: User.active().count() >= 4, True)
ck("scope where 后追加", lambda: User.where("name", "like", "%a%").active().count(), 1)
ck("scope 串联", lambda: User.active().adult().count(), 4)
ck("scope 混排", lambda: User.where("id", ">", 0).active().adult().where("name", "<>", "kid").count(), 4)
ck("scope 后接 where", lambda: User.active().where("name", "alice").count(), 1)

ck("User.update dict where", lambda: User.update({"age": 31}, {"id": u.id}), 1)
ck("User.update pk list", lambda: User.update({"status": 1}, [u.id]), 1)

# 关联
a1 = Article.create({"user_id": u.id, "title": "t1"})
a2 = Article.create({"user_id": u.id, "title": "t2"})
ck("懒加载 has_many", lambda: len(User.find(u.id).articles), 2)
ck("懒加载 belongs_to", lambda: Article.find(a1.id).user.name, "ALICE")
r1 = Role.create({"name": "admin"})
r2 = Role.create({"name": "ed"})
Db.name("user_role").insert_all([{"user_id": u.id, "role_id": r1.id}, {"user_id": u.id, "role_id": r2.id}])
ck("懒加载 btm", lambda: len(User.find(u.id).roles), 2)
ck("with_ 字符串", lambda: len(User.where("id", u.id).with_("articles").find().articles), 2)
ck("with_ 多关联", lambda: len(User.where("id", u.id).with_("articles", "roles").find().roles), 2)
ck("with_ 闭包", lambda: len(User.where("id", u.id).with_({"articles": lambda q: q.where("title", "t1")}).find().articles), 1)
ck("with_ 嵌套", lambda: User.where("id", u.id).with_("articles.user").find().articles[0].user.name, "ALICE")
ck("to_dict 含关联", lambda: "roles" in User.where("id", u.id).with_("roles").find().to_dict(), True)

nu = User()
nu.name = "tg"
nu.together(["articles"])
nu.articles = [Article(title="c1"), Article(title="c2")]
nu.save()
ck("together 级联保存", lambda: Article.where("user_id", nu.id).count(), 2)

# Collection
ck("coll first/last", lambda: (User.select().first() is not None, User.select().last() is not None), (True, True))
ck("coll column", lambda: isinstance(User.select().column("name"), list), True)
ck("coll column key", lambda: isinstance(User.select().column("name", "id"), dict), True)
ck("coll where", lambda: len(User.select().where(status=1)) >= 4, True)
ck("coll map", lambda: isinstance(User.select().map(lambda x: x.name), Collection), True)
ck("coll filter_", lambda: len(User.select().filter_(lambda x: (x.age or 0) > 18)) >= 3, True)
ck("coll sum", lambda: isinstance(User.select().sum("age"), (int, float)), True)
ck("coll value", lambda: User.select().value("name") is not None, True)
ck("coll to_array", lambda: len(User.select().to_array()) >= 6, True)
ck("coll to_json 含 date", lambda: User.select().to_json()[:1], "[")

# Paginator
p = User.paginate(2, 1)
ck("paginator total", lambda: p.total >= 6, True)
ck("paginator props", lambda: (p.list_rows, p.per_page, p.last_page), (2, 2, (p.total + 1) // 2))
ck("paginator data", lambda: len(p.data), 2)
ck("paginator to_array", lambda: sorted(p.to_array().keys()), ["current_page", "data", "last_page", "per_page", "total"])
ck("paginator has_more", lambda: isinstance(p.has_more, bool), True)

# 写入
uid = Db.name("user").insert({"name": "ins", "age": 1})
ck("insert 自增 id", lambda: uid > 0, True)
ck("update 行数", lambda: Db.name("user").where("id", uid).update({"age": 2}), 1)
Db.name("user").where("id", uid).inc("age", 3)
ck("inc", lambda: Db.name("user").where("id", uid).value("age"), 5)
Db.name("user").where("id", uid).dec("age", 1)
ck("dec", lambda: Db.name("user").where("id", uid).value("age"), 4)
ck("insert_all", lambda: Db.name("user").insert_all([{"name": "a1"}, {"name": "a2"}]), 2)
ck("raw 运算", lambda: Db.name("user").where("id", uid).update({"balance": raw("balance - 1")}), 1)
ck("raw where", lambda: len(Db.name("user").where("balance", ">", raw("50")).select()) >= 2, True)
ck("delete", lambda: Db.name("user").where("id", uid).delete(), 1)
ck("query.save 按主键更新", lambda: Db.name("user").save({"id": uid, "name": "zz"}), 0)
try:
    Db.name("user").update({"age": 1})
    print("FAIL update 无 where 未拦截")
    fail += 1
except QueryError:
    ok += 1

# 事务
def work():
    Db.name("user").insert({"name": "tx1"})
    Db.name("user").insert({"name": "tx2"})


ck("事务回调", lambda: (Db.transaction(work), Db.name("user").where("name", "tx1").count())[1], 1)
try:
    with Db.transaction():
        Db.name("user").insert({"name": "tx3"})
        raise RuntimeError("boom")
except RuntimeError:
    pass
ck("事务回滚", lambda: Db.name("user").where("name", "tx3").count(), 0)
try:
    with Db.transaction():
        Db.name("user").insert({"name": "out"})
        try:
            with Db.transaction():
                Db.name("user").insert({"name": "inner"})
                raise RuntimeError("x")
        except RuntimeError:
            pass
        Db.name("user").insert({"name": "after"})
except RuntimeError:
    print("FAIL 外层不应失败")
    fail += 1
ck("SAVEPOINT 外层", lambda: Db.name("user").where("name", "out").count(), 1)
ck("SAVEPOINT 内层回滚", lambda: Db.name("user").where("name", "inner").count(), 0)
ck("SAVEPOINT 后续", lambda: Db.name("user").where("name", "after").count(), 1)


# 软删除
class SoftUser(Model):
    __table__ = "user"
    __soft_delete__ = "delete_time"


s = SoftUser.create({"name": "sd"})
ck("软删 delete", lambda: s.delete(), 1)
ck("软删后 find None", lambda: SoftUser.find(s.id), None)
ck("with_trashed", lambda: SoftUser.with_trashed().where("id", s.id).count(), 1)
ck("only_trashed", lambda: SoftUser.only_trashed().count(), 1)
ck("restore", lambda: (SoftUser.with_trashed().where("id", s.id).find().restore(), SoftUser.find(s.id) is not None)[1], True)
ck("force_delete", lambda: SoftUser.with_trashed().where("id", s.id).find().force_delete(), 1)

# 调试
ck("fetch_sql", lambda: "SELECT" in str(Db.name("user").where("id", 1).fetch_sql(True).select()), True)
Db.name("user").where("id", 1).select()
ck("get_last_sql", lambda: "SELECT" in Db.get_last_sql(), True)
ck("sql_log", lambda: len(Db.get_sql_log()) > 0, True)
Db.sql_log_clear()
ck("sql_log_clear", lambda: len(Db.get_sql_log()), 0)
ck("explain", lambda: isinstance(Db.name("user").explain(), list), True)

# 查询构造器细节
ck("field 别名映射", lambda: "uid" in Db.name("user").field({"uid": "id"}).limit(1).select()[0], True)
ck("field_raw", lambda: "total" in Db.name("user").field_raw("COUNT(*) AS total").select()[0], True)
ck("without_field", lambda: "password" not in Db.name("user").without_field("password").limit(1).select()[0], True)
ck("distinct", lambda: len(Db.name("user").field("status").distinct(True).select()) >= 1, True)
ck("alias", lambda: len(Db.name("user").alias("u").field("u.name").limit(1).select()), 1)
ck("order 字符串多字段", lambda: len(Db.name("user").order("age desc, id asc").select()) >= 6, True)
ck("order 字典多字段", lambda: len(Db.name("user").order({"age": "desc", "id": "asc"}).select()) >= 6, True)
ck("order 多次追加", lambda: "ORDER BY" in Db.name("user").order("id", "desc").order("name", "asc").fetch_sql(True).select(), True)
ck("order_raw", lambda: len(Db.name("user").order_raw("RANDOM()").select()) >= 6, True)
ck("page", lambda: len(Db.name("user").page(2, 2).select()), 2)
ck("limit offset", lambda: len(Db.name("user").limit(3).select()), 3)
ck("group+having", lambda: len(Db.name("user").field("status, COUNT(*) AS num").group("status").having("COUNT(*)", ">", 0).select()) >= 1, True)
ck("join 闭包", lambda: len(Db.name("user u").join("article a", lambda q: q.where_column("a.user_id", "=", "u.id")).field("u.name", "a.title").select()) >= 2, True)
ck("left_join", lambda: len(Db.name("user u").left_join("article a", lambda q: q.where_column("a.user_id", "=", "u.id")).field("u.name").select()) >= 2, True)
ck("right_join", lambda: len(Db.name("user u").right_join("article a", lambda q: q.where_column("a.user_id", "=", "u.id")).field("u.name").select()) >= 0, True)
ck("union", lambda: len(Db.name("user").field("age").union(lambda q: q.name("user").field("age").where("age", 20)).select()) >= 1, True)
ck("union_all >= union", lambda: len(Db.name("user").field("age").union_all(lambda q: q.name("user").field("age").where("age", 20)).select()) >= len(Db.name("user").field("age").union(lambda q: q.name("user").field("age").where("age", 20)).select()), True)
ck("where_column", lambda: len(Db.name("user").where_column("age", ">", "balance").select()) >= 1, True)
ck("where_exists", lambda: len(Db.name("user").where_exists(lambda q: q.name("article").where_column("article.user_id", "=", "user.id")).select()) >= 1, True)
ck("where_raw 绑定", lambda: len(Db.name("user").where_raw("age > ? AND status = ?", [18, 1]).select()) >= 1, True)
ck("where 闭包嵌套", lambda: len(Db.name("user").where(lambda q: q.where("age", ">", 20).where_or("name", "alice")).select()) >= 3, True)
ck("where 数组 IN", lambda: len(Db.name("user").where("age", [20, 25, 30]).select()) >= 3, True)
ck("where None -> IS NULL", lambda: isinstance(Db.name("user").where("password", None).count(), int), True)
ck("where_like", lambda: Db.name("user").where_like("name", "top%").count(), 1)
ck("where_between", lambda: Db.name("user").where_between("age", [20, 30]).count() >= 3, True)
ck("where_not_in", lambda: Db.name("user").where_not_in("id", [1, 2]).count() >= 1, True)
try:
    User.find_or_fail(999999)
    print("FAIL find_or_fail 未抛异常")
    fail += 1
except DataNotFound:
    ok += 1
ck("find_or_empty", lambda: User.find_or_empty(999999).id, None)

# camelCase 别名
ck("whereIn", lambda: Db.name("user").whereIn("id", [1, 2]).count() >= 1, True)
ck("whereNotIn", lambda: isinstance(Db.name("user").whereNotIn("id", [1, 2]).count(), int), True)
ck("whereNotNull", lambda: Db.name("user").whereNotNull("name").count() >= 6, True)
ck("whereBetween", lambda: Db.name("user").whereBetween("age", [20, 30]).count() >= 3, True)
ck("fieldRaw", lambda: "total" in Db.name("user").fieldRaw("COUNT(*) AS total").select()[0], True)
ck("findOrEmpty", lambda: User.findOrEmpty(999999).id, None)
ck("selectOrFail", lambda: len(User.selectOrFail()) >= 6, True)
ck("withTrashed(软删模型)", lambda: len(SoftUser.withTrashed().select()) >= 1, True)

# ---------- 查询缓存 / 流式读取 / 多线程（v0.4.0） ----------
import threading

Db.execute("CREATE TABLE IF NOT EXISTS cache_demo (id INTEGER PRIMARY KEY AUTOINCREMENT, url TEXT)")
Db.execute("DELETE FROM cache_demo")
Db.name("cache_demo").insert({"url": "https://example.com/a"})
Db.clear_cache()

ck("cache(秒) 首次查询",
   lambda: Db.name("cache_demo").where({"url": "https://example.com/a"}).cache(10).find()["url"],
   "https://example.com/a")
Db.sql_log_clear()
Db.name("cache_demo").where({"url": "https://example.com/a"}).cache(10).find()
ck("cache(秒) 命中不产生 SQL", lambda: len(Db.get_sql_log()), 0)
Db.name("cache_demo").where({"url": "https://example.com/a"}).update({"url": "https://example.com/b"})
Db.sql_log_clear()
ck("写操作后缓存失效",
   lambda: Db.name("cache_demo").where({"url": "https://example.com/b"}).cache(10).find()["url"],
   "https://example.com/b")
ck("cache(0) 不缓存", lambda: len(Db.name("cache_demo").cache(0).select()) >= 1, True)
ck("自定义缓存键", lambda: Db.name("cache_demo").cache(60, "demo:key").count() >= 1, True)
ck("旧签名 cache(key, expire) 兼容",
   lambda: Db.name("cache_demo").cache("demo:legacy", 60).count() >= 1, True)
ck("缓存统计 backend", lambda: Db.cache_store().stats()["backend"], "memory")
ck("clear_cache 返回条数", lambda: isinstance(Db.clear_cache(), int), True)
_row = Db.name("cache_demo").cache(30).select()[0]
_row["url"] = "mutated"
ck("缓存结果隔离", lambda: Db.name("cache_demo").cache(30).select()[0]["url"] != "mutated", True)

# 流式读取
ck("chunk 分块大小", lambda: [len(b) for b in Db.name("user").order("id").chunk(2)][:2], [2, 2])
ck("chunk 覆盖全部行", lambda: sum(len(b) for b in Db.name("user").chunk(3)), Db.name("user").count())
ck("cursor 逐行读取", lambda: len(list(Db.name("user").order("id").cursor(chunk_size=2))), Db.name("user").count())
ck("cursor 产出 dict", lambda: isinstance(next(iter(Db.name("user").cursor())), dict), True)

# 多线程
def _worker(n):
    for i in range(5):
        with Db.transaction():
            Db.name("user").insert({"name": "thr%d-%d" % (n, i), "age": 1})

_threads = [threading.Thread(target=_worker, args=(k,)) for k in range(3)]
for _t in _threads:
    _t.start()
for _t in _threads:
    _t.join()
ck("多线程事务写入", lambda: Db.name("user").where_like("name", "thr%").count(), 15)
ck("连接默认开启线程保护", lambda: Db.get_connection()._thread_safe, True)

# ---------- JSON 字段查询 ----------
Db.execute("CREATE TABLE IF NOT EXISTS jdoc ("
           "id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, extra TEXT)")
Db.name("jdoc").insert_all([
    {"name": "张三", "extra": {"age": 18, "city": "北京",
                               "tags": ["vip", "new"], "note": None}},
    {"name": "李四", "extra": {"age": 25, "city": "上海", "score": 92.0,
                               "tags": ["new"]}},
])

ck("JSON 写入自动序列化",
   lambda: isinstance(Db.query("SELECT extra FROM jdoc WHERE name = ?", ["张三"])[0]["extra"], str),
   True)
ck("json() 结果自动格式化为 dict",
   lambda: isinstance(Db.name("jdoc").json().where("name", "张三").find()["extra"], dict),
   True)
ck("json([...]) 指定字段",
   lambda: Db.name("jdoc").json(["extra"]).where("name", "李四").find()["extra"]["city"],
   "上海")
ck("json(False) 关闭解码",
   lambda: isinstance(Db.name("jdoc").json(False).where("name", "张三").find()["extra"], str),
   True)
ck("json() 覆盖 value/column 路径",
   lambda: Db.name("jdoc").json(["extra"]).where("name", "李四").value("extra")["score"],
   92.0)
ck("json() 覆盖 chunk 路径",
   lambda: all(isinstance(r["extra"], dict) for r in next(iter(Db.name("jdoc").json().chunk(2)))),
   True)
ck("where_json 数值比较",
   lambda: [r["name"] for r in Db.name("jdoc").where_json("extra", "$.age", ">", 18).select()],
   ["李四"])
ck("where_json 简写等值",
   lambda: [r["name"] for r in Db.name("jdoc").where_json("extra", "$.city", "北京").select()],
   ["张三"])
ck("where_json 路径省略 $ 前缀",
   lambda: [r["name"] for r in Db.name("jdoc").where_json("extra", "age", 25).select()],
   ["李四"])
ck("where_json_contains",
   lambda: [r["name"] for r in Db.name("jdoc").where_json_contains("extra", "$.tags", "vip").select()],
   ["张三"])
ck("where_json_exists（值 null 也算存在）",
   lambda: len(Db.name("jdoc").where_json_exists("extra", "$.note").select()), 1)
ck("where_json_not_exists",
   lambda: [r["name"] for r in Db.name("jdoc").where_json_not_exists("extra", "$.note").select()],
   ["李四"])
ck("where_json_length",
   lambda: [r["name"] for r in Db.name("jdoc").where_json_length("extra", "$.tags", ">=", 2).select()],
   ["张三"])
ck("where_json_type",
   lambda: len(Db.name("jdoc").where_json_type("extra", "$.tags", "array").select()), 2)
ck("where_json_type 值为 null",
   lambda: [r["name"] for r in Db.name("jdoc").where_json_type("extra", "$.note", "null").select()],
   ["张三"])
ck("field_json 自动别名",
   lambda: [r["city"] for r in Db.name("jdoc").field_json("extra", "$.city").select()],
   ["北京", "上海"])
ck("field_json 与 field 顺序无关",
   lambda: set(Db.name("jdoc").field_json("extra", "$.city").field("name").select()[0].keys()),
   {"name", "city"})
ck("field_json 结果自动解码",
   lambda: Db.name("jdoc").field_json("extra", "$.tags").select()[0]["tags"], ["vip", "new"])
ck("order_json 排序",
   lambda: [r["name"] for r in Db.name("jdoc").order_json("extra", "$.age", "desc").select()],
   ["李四", "张三"])
ck("update 自动序列化",
   lambda: Db.name("jdoc").where("name", "张三").update({"extra": {"age": 19, "city": "北京"}})
   and Db.name("jdoc").json().where("name", "张三").find()["extra"]["age"], 19)


class JDoc(Model):
    __table__ = "jdoc"
    __json__ = ["extra"]


ck("模型 __json__ 自动接入",
   lambda: isinstance(JDoc.where("name", "张三").find().extra, dict), True)
ck("模型 to_dict 返回 dict",
   lambda: isinstance(JDoc.where("name", "李四").find().to_dict()["extra"], dict), True)
ck("模型配合 where_json 条件",
   lambda: len(JDoc.where_json("extra", "$.city", "北京").select()), 1)

print("\nREADME 示例：通过 %d 项，失败 %d 项" % (ok, fail))
sys.exit(1 if fail else 0)
