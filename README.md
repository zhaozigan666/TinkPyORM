# thinkorm-py

<div align="center">

**一个用纯 Python 标准库实现的 SQLite ORM，API 设计参考 ThinkPHP 的 think-orm v4.0**

[![Python](https://img.shields.io/badge/Python-3.8%2B-blue.svg)](https://www.python.org/)
[![Dependencies](https://img.shields.io/badge/dependencies-0%20(zero)-brightgreen.svg)](#零依赖)
[![Backend](https://img.shields.io/badge/database-SQLite-lightgrey.svg)](https://www.sqlite.org/)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](#许可证)

*链式操作 · 参数绑定 · 关联预载入 · 软删除 · 嵌套事务 —— 零第三方依赖*

</div>

---

## 目录

- [简介](#简介)
- [特性](#特性)
- [零依赖](#零依赖)
- [安装](#安装)
- [快速开始](#快速开始)
- [连接配置](#连接配置)
- [查询构造器](#查询构造器)
- [WHERE 条件全景](#where-条件全景)
- [聚合 · 分组 · 排序 · 分页](#聚合--分组--排序--分页)
- [JOIN 与 UNION](#join-与-union)
- [原生表达式](#原生表达式)
- [写入操作](#写入操作)
- [事务](#事务)
- [模型](#模型)
- [获取器与修改器](#获取器与修改器)
- [类型转换](#类型转换)
- [软删除](#软删除)
- [查询范围 Scope](#查询范围-scope)
- [模型事件](#模型事件)
- [关联](#关联)
- [结果集与分页](#结果集与分页)
- [camelCase 别名](#camelcase-别名)
- [调试与 SQL 日志](#调试与-sql-日志)
- [API 速查表](#api-速查表)
- [与 think-orm 对照表](#与-think-orm-对照表)
- [运行测试](#运行测试)
- [项目结构](#项目结构)
- [注意事项与已知限制](#注意事项与已知限制)
- [更新记录](#更新记录)
- [致谢与灵感来源](#致谢与灵感来源)
- [许可证](#许可证)

---

## 简介

`thinkorm-py` 把 **ThinkPHP think-orm v4.0** 那套优雅的链式查询 API 搬到 Python 上来，用 SQLite 作为存储后端。

如果你写过 ThinkPHP，下面这段代码会让你感到熟悉：

```python
# ThinkPHP 写法
Db::name('user')->where('status', 1)->where('age', '>', 18)->order('id', 'desc')->select();

# thinkorm-py 写法
Db.name('user').where('status', 1).where('age', '>', 18).order('id', 'desc').select()
```

**设计目标：**

| 目标 | 说明 |
|---|---|
| **零依赖** | 只用 `sqlite3 / json / datetime / contextlib` 等标准库，`pip install` 都不需要 |
| **API 对齐** | 方法名、链式风格、语义尽量贴近 think-orm，降低跨语言心智负担 |
| **安全第一** | 所有值走 `?` 占位符参数绑定，标识符统一加反引号转义，杜绝 SQL 注入 |
| **Pythonic** | 在对齐 API 的同时保留 Python 习惯：`with_` 而非 `with`（关键字冲突）、列表推导友好、支持上下文管理器 |

> ⚠️ **独立性声明**：本项目是**受 think-orm 设计思想启发的独立实现**，不包含、也不复制任何 ThinkPHP / think-orm 的源代码。所有代码为 Python 原生重写，PHP 与 Python 语言特性差异较大，行为细节以本项目文档为准。

---

## 特性

- **链式查询构造器** —— `table / name / field / where / order / limit / page / join / group / having / union` 全链路
- **丰富的 WHERE 条件** —— 数组条件、闭包、NULL / IN / BETWEEN / LIKE / EXISTS / 字段比较 / 原生 SQL
- **参数绑定与标识符转义** —— 值用 `?` 占位符，表名/字段名用反引号包裹
- **ActiveRecord 模型** —— `create / find / save / delete / destroy`，静态查询 + 实例持久化
- **四种关联** —— `has_one / has_many / belongs_to / belongs_to_many`，支持懒加载、预载入（含嵌套点号）、级联保存
- **软删除** —— 自动过滤，支持 `with_trashed / only_trashed / restore / force_delete`
- **获取器 / 修改器 / 类型转换** —— `get_xxx_attr` / `set_xxx_attr`，JSON / int / float / bool / date / datetime / time 自动互转
- **自动时间戳** —— 可选自动维护 `create_time` / `update_time`
- **查询范围 scope** —— `scope_xxx` 类方法，可像类静态方法一样链式调用
- **模型事件** —— 写入前后 8 个钩子
- **嵌套事务** —— 基于 SAVEPOINT，内层回滚不影响外层
- **结果集与分页** —— `Collection`（`column / where / map / sum / to_json` …）+ `Paginator`
- **camelCase 别名** —— `whereIn` 自动映射到 `where_in`，PHP/JS 开发者上手无阻
- **调试友好** —— SQL 日志、`fetch_sql`、最后一条 SQL 查询

---

## 零依赖

```
Python 3.8+ 标准库：sqlite3, json, datetime, contextlib, collections, re, typing, threading
```

**不需要** SQLAlchemy、不需要 Peewee、不需要任何 `pip install`。

---

## 安装

无需安装，直接把 `thinkorm` 目录复制到你的项目里即可：

```bash
git clone <your-repo-url> thinkorm-py
cp -r thinkorm-py/thinkorm /path/to/your/project/
```

或在本目录直接使用：

```python
import sys; sys.path.insert(0, "/path/to/thinkorm-py")
from thinkorm import Db, Model
```

---

## 快速开始

```python
from thinkorm import Db, Model, Collection

# 1. 配置连接（SQLite 文件路径）
Db.set_config({"database": "app.db"})

# 2. 建表（原生 SQL）
Db.execute("""
    CREATE TABLE IF NOT EXISTS user (
        id      INTEGER PRIMARY KEY AUTOINCREMENT,
        name    TEXT NOT NULL,
        age     INTEGER DEFAULT 0,
        status  INTEGER DEFAULT 1
    )
""")

# 3. 链式查询
users = Db.name("user").where("status", 1).where("age", ">", 18).order("id", "desc").select()
for u in users:
    print(u["name"], u["age"])

# 4. 定义模型
class User(Model):
    __table__ = "user"
    __timestamps__ = True          # 自动维护 create_time / update_time

# 5. 模型 CRUD
user = User.create({"name": "thinkphp", "age": 20})   # 新增
user.age = 21
user.save()                                            # 更新
found = User.find(user.id)                             # 查询
User.destroy(user.id)                                  # 删除
```

---

## 连接配置

`Db.set_config()` 接受三种形式：

```python
# ① 字典配置
Db.set_config({
    "database": "app.db",     # SQLite 文件路径（或 ":memory:"）
    "prefix":   "tp_",        # 表前缀（配合 Db.name() 自动拼接）
    "timeout":  5,            # 传给 sqlite3.connect 的额外关键字参数
})

# ② 直接传路径
Db.set_config("app.db")

# ③ 传 Connection 对象（测试、多连接场景常用）
from thinkorm import Connection
Db.set_config(Connection(":memory:"))
```

**多连接：**

```python
Db.set_config({"database": "main.db"}, name="default")
Db.set_config({"database": "log.db"},  name="log")

Db.get_connection("log").execute("INSERT INTO log(msg) VALUES (?)", ["hello"])
```

**连接工具方法：**

```python
conn = Db.get_connection()
conn.table_exists("user")     # 表是否存在
conn.table_fields("user")     # 字段列表 ['id', 'name', ...]
conn.close()                  # 关闭连接
Db.close()                    # 关闭所有连接
```

---

## 查询构造器

所有链式方法返回 `Query` 自身，可任意串联；调用终结方法（`select / find / value / column / count / …`）时才真正执行 SQL。

```python
Db.table("user")                    # 指定表名（含前缀，原样使用）
Db.name("user")                     # 指定表名（自动加 prefix）
Db.table("user").alias("u")         # 起别名 → FROM `user` `u`
Db.table("user u")                  # 等价简写
```

**字段**

```python
Db.table("user").field("id, name").select()          # 字符串
Db.table("user").field("id", "name").select()        # 多参数
Db.table("user").field(["id", "name"]).select()      # 列表
Db.table("user").field({"uid": "id", "n": "name"})   # 别名映射 → SELECT `id` AS `uid` …
Db.table("user").field_raw("COUNT(*) AS total")      # 原生字段片段
Db.table("user").without_field("password")           # 排除字段（SQLite 无 EXCEPT 语法，内部用 PRAGMA 推导出真实字段列表实现）
```

**去重与注释**

```python
Db.table("user").distinct(True).select()
Db.table("user").comment("首页用户列表").select()     # SQL 注释，便于慢日志定位
```

---

## WHERE 条件全景

### 基础形式

```python
# 字段 + 值（默认 = ）
Db.table("user").where("id", 1)

# 字段 + 操作符 + 值
Db.table("user").where("age", ">", 18)
Db.table("user").where("age", ">=", 18).where("age", "<=", 60)
Db.table("user").where("name", "<>", "admin")        # <> / != 均可
Db.table("user").where("age", None)                  # → IS NULL

# 关键字参数（多条件 AND）
Db.table("user").where(id=1, status=1)

# 字典
Db.table("user").where({"age": 20, "status": 1})

# 数组 IN
Db.table("user").where("age", [20, 25, 30])          # → age IN (?, ?, ?)
```

支持的操作符：`=` `<>` `!=` `>` `>=` `<` `<=` `LIKE` `NOT LIKE` `IN` `NOT IN` `BETWEEN` `NOT BETWEEN` `IS NULL` `IS NOT NULL`

### 逻辑组合

```python
# OR / XOR
Db.table("user").where("age", ">", 30).where_or("age", "<", 21).select()
Db.table("user").where("a", 1).where_xor("b", 2).select()

# 嵌套闭包（自动加括号）
Db.table("user").where(
    lambda q: q.where("age", ">", 20).where_or("name", "alice")
).select()
# → WHERE ( age > ? OR name = ? )
```

### 语义化快捷方法

| 方法 | 生成 SQL | 说明 |
|---|---|---|
| `where_null(f)` | `f IS NULL` | 为空 |
| `where_not_null(f)` | `f IS NOT NULL` | 非空 |
| `where_in(f, [...])` | `f IN (?, ?)` | IN 查询 |
| `where_not_in(f, [...])` | `f NOT IN (?, ?)` | NOT IN |
| `where_like(f, "%a%")` | `f LIKE ?` | 模糊匹配 |
| `where_not_like(f, "%a%")` | `f NOT LIKE ?` | 排除匹配 |
| `where_between(f, [a, b])` | `f BETWEEN ? AND ?` | 区间 |
| `where_not_between(f, [a, b])` | `f NOT BETWEEN ? AND ?` | 区间外 |
| `where_column(f1, op, f2)` | `f1 > f2` | **字段间比较** |
| `where_exists(cb)` | `EXISTS (subquery)` | 子查询存在 |
| `where_not_exists(cb)` | `NOT EXISTS (subquery)` | 子查询不存在 |
| `where_raw(sql, params)` | 原样插入 | 原生条件（params 仍走绑定） |

```python
# 字段间比较
Db.table("user").where_column("age", ">", "balance").select()

# EXISTS 子查询
Db.table("user").where_exists(
    lambda q: q.table("article").where_column("article.user_id", "=", "user.id")
).select()

# 原生条件（参数依旧安全绑定）
Db.table("user").where_raw("age > ? AND status = ?", [18, 1]).select()
```

---

## 聚合 · 分组 · 排序 · 分页

```python
# 聚合
Db.table("user").count()              # 总行数
Db.table("user").count("id")          # 指定字段
Db.table("user").sum("age")           # 求和
Db.table("user").avg("age")           # 平均
Db.table("user").max("age")           # 最大
Db.table("user").min("age")           # 最小

# 分组 + HAVING
Db.table("user").field("status, COUNT(*) AS num") \
                .group("status") \
                .having("COUNT(*)", ">", 0) \
                .select()
Db.table("user").group("status").having_or("num", ">", 5).select()

# 排序
Db.table("user").order("age", "desc").select()                   # 单字段
Db.table("user").order("age desc, id asc").select()              # 多字段（字符串）
Db.table("user").order({"age": "desc", "id": "asc"}).select()    # 多字段（字典）
Db.table("user").order("age", "desc").order("id", "asc").select()  # 多次调用追加
Db.table("user").order_raw("RANDOM()").select()                  # 原生排序片段

# 限制与分页
Db.table("user").limit(10).select()          # LIMIT 10
Db.table("user").limit(10, 20).select()      # LIMIT 20, 10（offset 在前）
Db.table("user").page(2, 15).select()        # 第 2 页，每页 15 条
```

**分页对象：**

```python
p = Db.table("user").paginate(list_rows=15, page=1)

p.total          # 总记录数
p.current_page   # 当前页
p.list_rows      # 每页条数
p.per_page       # 每页条数（property）
p.last_page      # 末页（property）
p.has_more       # 是否还有下一页（property）
p.items          # 当前页数据（Collection / list）
p.data           # 当前页数据（property，等价 items）
p.to_array()     # 转字典（含 total / last_page 等，适合直接给 API 返回）
```

---

## JOIN 与 UNION

```python
# 闭包 ON 条件（推荐，自动 AND 连接）
Db.table("user u") \
  .join("article a", lambda q: q.where_column("a.user_id", "=", "u.id")) \
  .field("u.name", "a.title") \
  .select()

# 也支持 left / right / inner
Db.table("user u").left_join("article a", lambda q: q.where_column("a.user_id", "=", "u.id")).select()
Db.table("user u").right_join("profile p", lambda q: q.where_column("p.user_id", "=", "u.id")).select()
Db.table("user u").inner_join("role r", lambda q: q.where_column("r.id", "=", "u.role_id")).select()

# UNION（去重）/ UNION ALL（不去重）
Db.table("user").field("age").union(
    lambda q: q.table("user").field("age").where("age", 20)
).select()

Db.table("user").field("age").union_all(
    lambda q: q.table("user").field("age").where("age", 20)
).select()
```

---

## 原生表达式

需要写数据库函数、算术运算而**不想被当作字符串值转义**时，用 `raw()` / `Raw`：

```python
from thinkorm import raw, Raw

# WHERE 中的原生值
Db.table("user").where("balance", ">", raw("50")).select()

# UPDATE 中的自身运算
Db.table("user").where("id", 1).update({"balance": raw("balance - 100")})

# 字段片段
Db.table("user").field_raw("COUNT(*) AS total").select()
```

> ⚠️ **注意**：`raw()` 的内容会**原样拼进 SQL，不做任何转义**。严禁把用户输入直接传进 `raw()`，否则会产生 SQL 注入风险。凡是可绑定的值，一律走 `where_raw(sql, [params])`。

---

## 写入操作

```python
# 插入，返回自增主键
uid = Db.table("user").insert({"name": "alice", "age": 20})

# 批量插入，返回影响行数
n = Db.table("user").insert_all([
    {"name": "a"}, {"name": "b"}, {"name": "c"},
])

# 更新，返回影响行数（不带 where 会抛 QueryError，防止误全表更新）
n = Db.table("user").where("id", uid).update({"age": 21})

# 字段自增 / 自减
Db.table("user").where("id", uid).inc("score", 5)
Db.table("user").where("id", uid).dec("balance", 10)

# 删除，返回影响行数（同样强制要求 where）
n = Db.table("user").where("id", uid).delete()

# save：数据含主键值 → 按该主键更新；不含主键值 → 插入
Db.table("user").save({"id": uid, "name": "new"})   # 更新（返回影响行数）
Db.table("user").save({"name": "new"})              # 插入（返回自增 id）
```

> **安全设计**：`update()` 和 `delete()` 在未设置任何 `where` 条件时会抛出 `QueryError`。这借鉴了 think-orm 的防误操作理念，避免一行代码清空整表。

---

## 事务

两种用法，可嵌套：

```python
# ① 回调模式（自动提交/回滚）
def work():
    Db.table("user").insert({"name": "a"})
    Db.table("user").insert({"name": "b"})
Db.transaction(work)

# ② 上下文模式
with Db.transaction():
    Db.table("user").insert({"name": "c"})
    # 抛异常自动回滚
```

**嵌套事务（SAVEPOINT）**：内层回滚不影响外层已执行的语句。

```python
with Db.transaction():
    Db.table("user").insert({"name": "outer"})      # 会提交
    try:
        with Db.transaction():
            Db.table("user").insert({"name": "inner"})
            raise RuntimeError("inner fail")        # 内层回滚
    except RuntimeError:
        pass
    Db.table("user").insert({"name": "after"})      # 会提交

# 结果：outer ✓ / inner ✗ / after ✓
```

也可以直接使用连接对象：

```python
conn = Db.get_connection()
conn.begin()
try:
    conn.execute(...)
    conn.commit()
except Exception:
    conn.rollback()
```

---

## 模型

### 定义模型

```python
from thinkorm import Model

class User(Model):
    __table__ = "user"                  # 表名（默认：类名转 snake_case）
    __pk__ = "id"                       # 主键（默认 "id"）
    __connection__ = None               # 连接名或 Connection 对象（默认全局连接）
    __prefix__ = ""                     # 表前缀
    __timestamps__ = True               # 自动维护 create_time / update_time
    __create_time__ = "create_time"     # 创建时间字段名
    __update_time__ = "update_time"     # 更新时间字段名
    __soft_delete__ = None              # 软删除字段名（None 表示不启用）
    __type__ = {"age": "int",           # 类型转换
                "tags": "json"}
    __json__ = ["tags"]                 # JSON 字段（自动序列化/反序列化）
```

### CRUD

```python
# 新增（返回模型实例，已带自增 id）
user = User.create({"name": "thinkphp", "age": 20, "tags": ["a", "b"]})

# 查询
u1 = User.find(1)                       # 按主键，无结果返回 None
u2 = User.find_or_empty(999)            # 无结果返回空模型（属性为 None）
u3 = User.find_or_fail(999)             # 无结果抛 DataNotFound
u4 = User.where("status", 1).find()     # 带条件查单条
u5 = User.get(1)                        # find 的别名

# 条件查询（模型类上可直接调用 Query 的方法）
users = User.where("age", ">", 18).order("id", "desc").select()   # → Collection
one   = User.where("name", "alice").find()
total = User.where("status", 1).count()
names = User.column("name")             # 取单列
age   = User.where("id", 1).value("age")

# 更新（实例）
u1.age = 21
u1.save()                               # 只提交变更过的字段

# 更新（静态）
User.update({"age": 30}, {"id": 1})     # 带 where，返回影响行数
User.update({"status": 0}, [1, 2, 3])   # 主键列表也支持

# 删除
u1.delete()                             # 实例删除（启用软删除时自动走软删）
User.destroy(1)                         # 按主键
User.destroy([1, 2, 3])                 # 按主键列表
u1.force_delete()                       # 强制物理删除（忽略软删）
```

### 序列化

```python
u = User.find(1)
u.to_dict()                             # → dict
u.to_array()                            # → dict（等价）
u.to_json()                             # → JSON 字符串（中文不转义）
u.to_json(indent=2)                     # 支持 json.dumps 的其他参数

# 隐藏 / 只显示字段
u.hidden("password", "salt").to_dict()
u.visible("id", "name").to_dict()
```

> `to_json()` 内置了 `default=str`，因此 `date` / `datetime` / `time` 等类型转换字段会序列化为 ISO 字符串（如 `"2000-01-02"`），不会抛序列化异常。

### 批量赋值

```python
u = User(name="alice", age=20)          # 构造时传字段
u.set_attrs({"age": 21, "name": "bob"}) # 批量赋值
u.get_attr("name")                      # 读取单个属性
```

---

## 获取器与修改器

命名规则：`get_<字段>_attr` / `set_<字段>_attr`（字段名保持小写下划线）。

```python
class User(Model):
    __table__ = "user"

    def get_name_attr(self, value):
        """读取时生效：name 转大写"""
        return value.upper() if value else value

    def set_name_attr(self, value):
        """写入时生效：去掉首尾空格"""
        return value.strip()

    def get_status_attr(self, value):
        """可返回完全不同的类型"""
        return {0: "禁用", 1: "正常"}.get(value, "未知")


u = User.create({"name": "  alice  "})  # 存库前 strip → "alice"
u.name                                   # 读取时 upper → "ALICE"
u.status                                 # → "正常"
```

> 获取器只影响**读取结果**，不改变数据库中存储的值；修改器在**写入前**生效。

---

## 类型转换

通过 `__type__` 声明，读写时自动互转：

```python
class User(Model):
    __table__ = "user"
    __type__ = {
        "id":       "int",
        "score":    "float",
        "is_vip":   "bool",
        "birthday": "date",
        "login_at": "datetime",
        "tags":     "json",
    }
    __json__ = ["tags"]        # 与 "json"/"array" 类型等价的另一种写法
```

| 类型 | 存储（写） | 读取（读） |
|---|---|---|
| `int` | 原样 | `int` |
| `float` | 原样 | `float` |
| `bool` | `1 / 0` | `bool` |
| `json` / `array` | `json.dumps` | `json.loads` |
| `date` | `YYYY-MM-DD` | `datetime.date` |
| `datetime` | `YYYY-MM-DD HH:MM:SS` | `datetime.datetime` |
| `time` | `HH:MM:SS` | `datetime.time` |

---

## 软删除

```python
class SoftUser(Model):
    __table__ = "user"
    __soft_delete__ = "delete_time"      # 该字段为 NULL 表示未删除

u = SoftUser.create({"name": "a"})
u.delete()                               # 写入 delete_time，记录仍在

SoftUser.find(u.id)                      # → None（默认自动过滤）
SoftUser.select()                        # 只返回未删除的
SoftUser.count()                         # 只统计未删除的

SoftUser.with_trashed().where("id", u.id).find()   # 含已删除
SoftUser.only_trashed().select()                   # 只要已删除

# 恢复
u2 = SoftUser.with_trashed().where("id", u.id).find()
u2.restore()

# 物理删除
u2.force_delete()
```

---

## 查询范围 Scope

在模型中定义 `scope_<名称>` 方法，即可作为**类静态方法**或链式方法使用：

两种定义风格都支持 —— 普通实例方法，或 `@classmethod`：

```python
class User(Model):
    __table__ = "user"

    # 风格一：实例方法（self 会被自动传入 None，只需关心 query）
    def scope_active(self, query):
        query.where("status", 1)

    # 风格二：类方法（cls 已绑定，无需 self）
    @classmethod
    def scope_adult(cls, query):
        query.where("age", ">=", 18)
```

调用方式非常灵活，scope 可以出现在链式的任意位置、任意多个：

```python
User.active().select()                                  # ① 类入口（think-orm 经典写法）
User.where("name", "like", "%a%").active().select()     # ② 普通链式方法之后追加
User.adult().active().select()                          # ③ 多个 scope 串联
User.where("id", ">", 0).active().adult().order("id").select()   # ④ 与查询方法自由混排
User.active().where("name", "alice").count()            # ⑤ scope 之后继续接查询方法
```

> `scope_` 前缀的方法既能当 scope 用，也能正常作为实例方法调用。scope 返回的是当前 `Query` 自身，因此可以无限串联。

---

## 模型事件

重写对应方法即可挂载钩子：

| 方法 | 触发时机 |
|---|---|
| `on_before_write` | 任何写入（新增/更新）前 |
| `on_after_write` | 任何写入后 |
| `on_before_insert` | 新增前 |
| `on_after_insert` | 新增后 |
| `on_before_update` | 更新前 |
| `on_after_update` | 更新后 |
| `on_before_delete` | 删除前 |
| `on_after_delete` | 删除后 |

```python
class User(Model):
    __table__ = "user"

    def on_before_insert(self):
        # 例如：自动生成 slug、校验、写审计日志
        if not self.get_attr("slug"):
            self.set_attr("slug", self.get_attr("name").lower())

    def on_after_delete(self):
        print(f"用户 {self.id} 已删除")
```

---

## 关联

### 定义关联

```python
class User(Model):
    __table__ = "user"

    def profile(self):
        return self.has_one("Profile", "user_id")

    def articles(self):
        return self.has_many("Article", "user_id")

    def roles(self):
        return self.belongs_to_many("Role", "user_role", "user_id", "role_id")

class Article(Model):
    __table__ = "article"

    def user(self):
        return self.belongs_to("User", "user_id")

class Role(Model):
    __table__ = "role"

    def users(self):
        return self.belongs_to_many("User", "user_role", "role_id", "user_id")
```

| 关联类型 | 说明 | 外键默认推导 |
|---|---|---|
| `has_one(target, foreign_key, local_key)` | 一对一 | `<当前表名>_id` / 主键 |
| `has_many(target, foreign_key, local_key)` | 一对多 | `<当前表名>_id` / 主键 |
| `belongs_to(target, foreign_key, local_key)` | 反向一对一/多对一 | `<目标表名>_id` / 目标主键 |
| `belongs_to_many(target, middle, foreign_key, local_key)` | 多对多（经中间表） | 需指定中间表 |

### 懒加载

```python
u = User.find(1)
u.articles                  # 首次访问时查询，返回 Collection
u.articles                  # 再次访问走缓存，不重复查库
a = Article.find(1)
a.user.name                 # belongs_to
u.roles                     # 多对多
```

### 预载入（解决 N+1）

```python
# 单个关联
users = User.with_("articles").select()

# 多个关联
users = User.with_("articles", "profile").select()

# 闭包约束（对预载入的子查询加条件）
users = User.with_({
    "articles": lambda q: q.where("status", 1).order("id", "desc")
}).select()

# 嵌套（点号表示法）
users = User.with_("articles.user").select()
users[0].articles[0].user.name          # 无需再查库

# 组合使用
users = User.where("status", 1).with_("articles").with_("profile").select()
```

### 关联写入

```python
# 单个模型赋值
a = Article(title="hello")
u = User.find(1)
u.articles = [a]                        # 支持 list / tuple / Collection / 单个 Model

# 级联保存（together）：主表保存时一并保存关联
u = User(name="alice")
u.together(["articles"])
u.articles = [Article(title="c1"), Article(title="c2")]
u.save()                                # 主表 INSERT 后，自动回填 user_id 并保存两篇文章
```

### 序列化带关联

```python
u = User.where("name", "alice").with_("roles").find()
u.to_dict()["roles"]                    # 关联数据会一并输出
```

---

## 结果集与分页

`select()` 返回 `Collection`（`list` 的子类），可直接迭代、索引、`len()`。

```python
users = User.where("status", 1).select()

users.first()                     # 第一条
users.last()                      # 最后一条
users.column("name")              # 取单列 → list
users.column("name", "id")        # 指定 key → dict
users.where(status=1)             # 内存中过滤
users.each(lambda u: print(u.name))        # 遍历（返回自身，可继续链）
users.map(lambda u: u.name)                # 映射
users.filter_(lambda u: u.age > 18)        # 过滤（filter_ 避开内置关键字）
users.sum("age") / users.avg("age") / users.max("age") / users.min("age")
users.value("name")                        # 取第一条的某字段
users.hidden("password")                   # 隐藏字段
users.visible("id", "name")                # 只显示
users.to_array()                           # → List[dict]
users.to_json(ensure_ascii=False)          # → JSON 字符串
```

---

## camelCase 别名

为照顾 PHP / JavaScript 开发者习惯，所有 `snake_case` 方法都自动支持 `camelCase` 写法：

```python
User.whereIn("id", [1, 2]).select()          # → where_in
User.whereNotIn("id", [1, 2]).select()       # → where_not_in
User.whereNotNull("email").select()          # → where_not_null
User.whereBetween("age", [20, 30]).select()  # → where_between
User.findOrEmpty(999)                        # → find_or_empty
User.findOrFail(999)                         # → find_or_fail
User.selectOrFail()                          # → select_or_fail
User.withTrashed().select()                  # → with_trashed（需启用软删除）
User.onlyTrashed().select()                  # → only_trashed（需启用软删除）
Db.table("user").fieldRaw("COUNT(*)").select()   # → field_raw
Db.table("user").unionAll(...).select()          # → union_all
```

> 两种写法完全等价，团队内统一即可。**注意**：映射目标是 `snake_case` 的**实际方法名**，例如 `Query` 上只有 `order`（没有 `order_by`），因此 `orderBy` 不可用；`withTrashed` / `onlyTrashed` 要求模型已配置 `__soft_delete__`。

---

## 调试与 SQL 日志

```python
# 只返回 SQL 字符串、不执行（参数已内联，可直接复制到 SQLite 客户端调试）
sql = Db.table("user").where("id", 1).fetch_sql(True).select()
print(sql)
# SELECT * FROM `user` WHERE `id` = 1

# 最后一条执行过的 SQL
Db.table("user").where("id", 1).select()
Db.get_last_sql()

# SQL 日志（[(sql, params), ...]）
Db.get_sql_log()
Db.sql_log_clear()

# 查询分析
Db.table("user").where("age", ">", 18).explain()
```

**渲染完整 SQL（含实际参数值）**，便于复制到 SQLite 客户端调试：

```python
q = Db.table("user").where("id", 1)
q.conn_render()      # 返回带真实参数值的 SQL 字符串（仅调试用，勿直接执行）
```

---

## API 速查表

### Query（链式）

| 类别 | 方法 |
|---|---|
| 表 | `table` `name` `alias` |
| 字段 | `field` `field_raw` `without_field` `distinct` `json` `with_attr` `filter` |
| 条件 | `where` `where_or` `where_xor` `where_null` `where_not_null` `where_in` `where_not_in` `where_like` `where_not_like` `where_between` `where_not_between` `where_column` `where_exists` `where_not_exists` `where_raw` |
| 连接 | `join` `left_join` `right_join` `inner_join` |
| 组合 | `union` `union_all` |
| 分组排序 | `group` `having` `having_or` `order` `order_raw` |
| 限制 | `limit` `page` `paginate` |
| 查询 | `select` `select_or_fail` `find` `find_or_empty` `find_or_fail` `value` `column` `count` `sum` `avg` `max` `min` `paginate` `explain` |
| 写入 | `insert` `insert_all` `update` `save` `delete` `inc` `dec` |
| 关联 | `with_` |
| 其他 | `cache` `comment` `lock` `fetch_sql` `fail_exception` `allow_empty` `copy` `get_last_sql` |

### Model（静态）

`find` `find_or_empty` `find_or_fail` `get` `select` `select_or_fail` `create` `update` `destroy` `value` `column` `count` `sum` `avg` `max` `min` `paginate` `query` `with_trashed` `only_trashed`

### Model（实例）

`save` `delete` `force_delete` `restore` `together` `get_attr` `set_attr` `set_attrs` `get_relation` `to_dict` `to_array` `to_json` `hidden` `visible` `has_one` `has_many` `belongs_to` `belongs_to_many`

### Db（门面）

`set_config` `get_connection` `close` `table` `name` `raw` `query` `execute` `transaction` `get_sql_log` `get_last_sql` `sql_log_clear`

---

## 与 think-orm 对照表

| think-orm (PHP) | thinkorm-py | 备注 |
|---|---|---|
| `Db::name('user')` | `Db.name("user")` | 一致 |
| `Db::table('user')` | `Db.table("user")` | 一致 |
| `->where('id', 1)` | `.where("id", 1)` | 一致 |
| `->whereIn('id', [1,2])` | `.where_in("id", [1,2])` 或 `.whereIn(...)` | snake_case + camelCase 双支持 |
| `->field('id,name')` | `.field("id,name")` | 一致 |
| `->order('id', 'desc')` | `.order("id", "desc")` | 一致 |
| `->limit(10)` / `->page(1,15)` | `.limit(10)` / `.page(1, 15)` | 一致 |
| `->select()` / `->find()` | `.select()` / `.find()` | 一致 |
| `->count()` / `->sum()` | `.count()` / `.sum()` | 一致 |
| `->insert($data)` | `.insert(data)` | 一致，返回自增 id |
| `->insertAll($list)` | `.insert_all(list)` | 一致 |
| `->update($data)` | `.update(data)` | 一致 |
| `->delete()` | `.delete()` | 一致 |
| `->inc('f', 1)` / `->dec()` | `.inc("f", 1)` / `.dec()` | 一致 |
| `Model::create($data)` | `Model.create(data)` | 一致 |
| `Model::destroy($id)` | `Model.destroy(id)` | 一致 |
| `->with('articles')` | `.with_("articles")` | ⚠️ `with` 是 Python 关键字，加下划线 |
| `->withTrashed()` | `.with_trashed()` / `.withTrashed()` | 一致 |
| `Db::raw('...')` | `raw("...")` / `Db.raw("...")` | 一致 |
| `Db::transaction(fn)` | `Db.transaction(fn)` / `with Db.transaction()` | 一致 + 上下文模式 |
| `getXxxAttr()` | `get_xxx_attr()` | 命名风格随语言 |
| `scopeXxx()` | `scope_xxx()` | 命名风格随语言 |
| `->paginate(15)` | `.paginate(15)` | 一致 |
| `->fetchSql()` | `.fetch_sql(True)` | Python 无 getter/setter 合一惯例 |
| `->failException()` | `.fail_exception()` | 一致 |
| `->json(['tags'])` | `.json(["tags"])` 或 `__json__` | 一致 |

---

## 运行测试

```bash
# 单元测试（31 项，覆盖查询构造 / 写入 / 事务 / 模型 / 软删除 / 关联）
python test_thinkorm.py

# 冒烟测试（端到端，覆盖全链路 API）
python smoke_test.py

# README 示例回归测试（112 项，逐条校验本文档中的用法示例）
python test_readme_examples.py
```

预期输出：

```
Ran 31 tests in 1.6s
OK
冒烟测试全部通过 ✔
README 示例：通过 112 项，失败 0 项
```

---

## 项目结构

```
thinkorm-py/
├── thinkorm/
│   ├── __init__.py       # 包导出（版本、公开 API）
│   ├── exceptions.py     # 异常体系（OrmError / DataNotFound / QueryError …）
│   ├── utils.py          # Raw/raw、标识符转义、命名转换、字段解析
│   ├── connection.py     # sqlite3 封装：查询/执行/事务(SAVEPOINT)/SQL 日志
│   ├── builder.py        # SQL 编译器：options → SQL（select/insert/update/delete）
│   ├── query.py          # 查询构造器：全链式方法 + 预载入 + 结果处理
│   ├── collection.py     # Collection 结果集 + Paginator 分页
│   ├── relation.py       # 关联实现：4 种类型 + 批量预载入
│   ├── model.py          # Model 基类 + MetaModel 元类（scope/camelCase/静态代理）
│   └── db.py             # Db 门面：连接管理、入口、事务、日志
├── test_thinkorm.py        # unittest 测试套件（31 项）
├── smoke_test.py           # 端到端冒烟测试
├── test_readme_examples.py # README 示例回归测试（112 项）
└── README.md
```

---

## 注意事项与已知限制

1. **仅支持 SQLite**。多数据库（MySQL / PostgreSQL）需要替换 `Builder` 与 `Connection`，当前未实现。
2. **`with_` 不是 `with`**：`with` 是 Python 保留关键字，预载入方法必须写成 `with_()`。
3. **`raw()` 不做转义**。它按字面量拼进 SQL，只应传入你自己硬编码的表达式，绝不可传入未校验的用户输入。
4. **`without_field` 有额外开销**：SQLite 不支持 `SELECT * EXCEPT(col)`，实现上先查 `PRAGMA table_info()` 推导出完整字段列表再剔除，多一次元数据查询。
5. **`cache()` 是进程内内存缓存**，非 `PSR-16` 那种跨请求持久缓存；进程重启即失效，也不跨进程共享。
6. **`right_join()` 依赖 SQLite 版本**：`RIGHT JOIN` / `FULL JOIN` 需 **SQLite ≥ 3.39**（Python 3.11+ 通常自带 3.39+）。旧版本会报语法错误，此时请改写为 `LEFT JOIN` 或升级 SQLite。
7. **`withTrashed()` / `onlyTrashed()` 需先启用软删除**：若模型未配置 `__soft_delete__`，调用会抛 `QueryError`。
8. **`lock()` 在 SQLite 下无实际效果**（SQLite 是文件级锁，`FOR UPDATE` 不适用），保留方法仅为 API 对齐。
9. **获取器不改变存储值**：它只在读取时转换，查询条件里仍应使用数据库里的原始值。
10. **模型属性访问未加载字段返回 `None`**，而非抛 `AttributeError`，这是刻意为之（避免模板里频繁判空）。
11. **未实现的 think-orm 特性**：虚拟模型、实体模型/分层、视图模型、数据自动验证器、MongoDB 支持、分布式与断点重连。

---

## 更新记录

### v0.1.1

行为修正（均为对齐 think-orm 语义的 bug 修复）：

1. **修改器 `set_xxx_attr` 现在对构造数据生效** —— 此前仅在属性赋值时触发，`create()` / `Model(**kwargs)` 传入的数据会绕过修改器直接入库。现已统一走 `set_attr`，`create({"name": "  alice  "})` 可正确触发 `strip` 等加工。从数据库读取仍走 `_from_data`，不会二次加工（例如密码哈希不会被执行两遍）。
2. **查询范围可在链式任意位置、任意多个串联** —— 此前只能在模型类上作入口调用一次（`User.active()`），`User.where(...).active()` 会抛 `AttributeError`。现 `Query` 在绑定模型时可解析 `scope_xxx`，两种定义风格（实例方法 / `@classmethod`）均支持。
3. **模型 `__prefix__` 生效** —— 此前显式设置 `__table__` 时前缀被忽略，现 `Model.query()` 改用 `name()` 拼前缀（前缀为空时行为不变）。
4. **`to_json()` 支持日期类型** —— 内置 `default=str`，`date` / `datetime` / `time` 类型转换字段序列化为 ISO 字符串，不再抛 `TypeError`。
5. 新增 `union_all()`（对应 think-orm 的 `unionAll`）。

### v0.1.0

首个可用版本：查询构造器、模型、关联、软删除、事务、分页等完整能力。

---

## 致谢与灵感来源

本项目的 API 设计、命名规范与链式操作风格，**灵感完全来源于 ThinkPHP 官方 ORM 组件 —— think-orm v4.0**。

在此向 **ThinkPHP 官方团队（顶想公司 TOPThink）** 及 **think-orm 的所有贡献者** 致以诚挚感谢。正是 think-orm 清晰优雅的查询构造器设计、完善的关联模型机制，以及对开发者体验的长期打磨，为本项目提供了完整的设计蓝本。本项目是一次跨语言的致敬与实践：**用 Python 标准库，把这套优秀的 API 带到 SQLite 场景**。

> 本项目为社区学习性质的独立实现，与 ThinkPHP 官方及顶想公司无隶属、合作或背书关系。所有实现代码均为 Python 原生编写，未复制 think-orm 的任何源代码。

### 官方资源

| 项目 | 链接 |
|---|---|
| **ThinkPHP 官方网站** | https://www.thinkphp.cn |
| **think-orm 官方文档（本项目主要参考）** | https://doc.thinkphp.cn/@think-orm |
| **think-orm v4.0 开发指南（直接参考来源）** | https://doc.thinkphp.cn/@think-orm/v4_0/default.html |
| **think-orm v4.0 新特性盘点** | https://doc.thinkphp.cn/@think-orm/v4_0/new-features.html |
| **think-orm v4.0 升级指导** | https://doc.thinkphp.cn/@think-orm/v4_0/upgrade.html |
| **think-orm GitHub 仓库（Apache-2.0）** | https://github.com/top-think/think-orm |
| **ThinkPHP 框架仓库** | https://github.com/top-think/framework |
| **TOPThink 官方组织** | https://github.com/top-think |
| **顶想云（TOPThink Cloud）** | https://www.topthink.com |

### 参考要点

设计过程中重点参考了 think-orm 的以下机制：

- **链式查询构造器** —— `Db::name()->where()->order()->select()` 的流式 API 与 `options` 状态累积模型
- **查询范围（scope）** —— `scopeXxx` 把常用条件封装成可复用片段
- **模型获取器 / 修改器** —— `getXxxAttr` / `setXxxAttr` 的读写双向拦截
- **预载入关联** —— `with()` 批量查询消除 N+1 问题
- **软删除** —— 基于 `delete_time` 字段的自动过滤与 `withTrashed/onlyTrashed` 反向查询
- **嵌套事务** —— 基于 SAVEPOINT 的分层回滚语义
- **原生表达式** —— `Db::raw()` 在 SQL 拼接与安全绑定之间取得平衡

### 技术栈致谢

- **Python** —— 标准库 `sqlite3` 模块提供了稳定可靠的嵌入式数据库能力
- **SQLite** —— 世界上部署最广泛的数据库引擎，零配置、单文件、事务完整

---

## 许可证

**thinkorm-py** 采用 [MIT License](LICENSE) 发布 —— 你可以自由使用、复制、修改、合并、出版发行、散布、再授权及销售本软件。

参考设计来源 **think-orm** 采用 [Apache-2.0 License](https://github.com/top-think/think-orm/blob/master/LICENSE)，版权归 ThinkPHP 官方团队所有。本项目仅参考其**设计思想与 API 命名**，不包含其源代码，二者许可证相互独立。

---

<div align="center">

**thinkorm-py** —— 用 Python 标准库，复刻 think-orm 的优雅

*Inspired by [ThinkPHP](https://www.thinkphp.cn) · [think-orm](https://github.com/top-think/think-orm)*

</div>
