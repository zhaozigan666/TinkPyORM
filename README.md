# TinkPyORM

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
- [查询缓存](#查询缓存)
- [流式读取](#流式读取)
- [多线程使用](#多线程使用)
- [性能实践建议](#性能实践建议)
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

`TinkPyORM` 把 **ThinkPHP think-orm v4.0** 那套优雅的链式查询 API 搬到 Python 上来，用 SQLite 作为存储后端。

如果你写过 ThinkPHP，下面这段代码会让你感到熟悉：

```python
# ThinkPHP 写法
Db::name('user')->where('status', 1)->where('age', '>', 18)->order('id', 'desc')->select();

# TinkPyORM 写法
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
- **查询缓存** —— `cache(秒)` 进程级 TTL 缓存，写操作自动失效，后端可替换
- **流式读取** —— `chunk()` 分块 / `cursor()` 逐行，大表不爆内存
- **线程安全** —— 连接级可重入锁，多线程共享连接时事务语义正确
- **调试友好** —— SQL 日志、`fetch_sql`、最后一条 SQL 查询

---

## 零依赖

```
Python 3.8+ 标准库：sqlite3, json, datetime, contextlib, collections, re, typing, threading
```

**不需要** SQLAlchemy、不需要 Peewee、不需要任何 `pip install`。

---

## 安装

无需安装，直接把 `tinkpyorm` 目录复制到你的项目里即可：

```bash
git clone <your-repo-url> TinkPyORM
cp -r TinkPyORM/tinkpyorm /path/to/your/project/
```

或在本目录直接使用：

```python
import sys; sys.path.insert(0, "/path/to/TinkPyORM")
from tinkpyorm import Db, Model
```

---

## 快速开始

```python
from tinkpyorm import Db, Model, Collection

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
    # "journal_mode": "WAL",  # 连接建立后执行 PRAGMA journal_mode=WAL
    #                         # （文件库写性能提升数十倍，见"性能实践建议"）
})

# ② 直接传路径
Db.set_config("app.db")

# ③ 传 Connection 对象（测试、多连接场景常用）
from tinkpyorm import Connection
Db.set_config(Connection(":memory:"))
```

**journal_mode（WAL 等）**：可在字典配置中指定 `journal_mode`，连接建立后自动执行
对应 PRAGMA（内存库自动跳过）：

```python
Db.set_config({"database": "app.db", "journal_mode": "WAL"})   # 推荐：读写并发 + 写性能大幅提升
Db.set_config({"database": "app.db", "journal_mode": "OFF"})   # 其他合法值：delete/truncate/persist/memory/wal/off
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
from tinkpyorm import raw, Raw

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

# 大批量插入（自动分批，调用方无需关心）
# SQLite 单语句绑定变量上限为 32766，超过会报 "too many SQL variables"。
# insert_all 会按 32766 // 列数 自动分批（如 6 列表每批 5461 行），
# 分批在同一事务内完成，中途失败整体回滚；也可显式指定批大小。
n = Db.table("user").insert_all(big_list)              # 自动分批
n = Db.table("user").insert_all(big_list, batch_size=500)  # 每批 500 行

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
from tinkpyorm import Model

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

# 日志默认只保留最近 1000 条（环形裁剪），避免长驻进程内存无限增长。
# 生产环境可整体关闭；需要完整日志时设为 None（不限制）。
Db.sql_log_disable()          # 关闭并清空
Db.sql_log_enable()           # 重新开启，上限 1000 条
Db.sql_log_enable(max_size=5000)
Db.set_config({"database": "app.db", "sql_log_max": None})  # 不限制

# 查询分析
Db.table("user").where("age", ">", 18).explain()
```

**渲染完整 SQL（含实际参数值）**，便于复制到 SQLite 客户端调试：

```python
q = Db.table("user").where("id", 1)
q.conn_render()      # 返回带真实参数值的 SQL 字符串（仅调试用，勿直接执行）
```

---

## 查询缓存

链式查询调用 `.cache(秒)` 即可缓存结果，参数是**倒计时秒数**，超时自动失效：

```python
Db.name('data').where({'url': url}).cache(10).find()    # 10 秒内复用同一结果
Db.table('user').where('status', 1).cache(60).select()  # 60 秒
Db.table('stat').cache(300).count()
```

| 方法 | 是否支持缓存 |
|---|---|
| `find()` / `find_or_fail()` / `find_or_empty()` | 支持 |
| `select()` / `select_or_fail()` | 支持 |
| `value()` / `column()` | 支持（内部走 find/select） |
| `count()` / `sum()` / `avg()` / `max()` / `min()` | 支持 |
| `paginate()` | 支持（total 与数据分别缓存） |
| `chunk()` / `cursor()` | 不缓存（流式读取本身即低内存路径） |

特性与规则：

1. **进程级共享** —— 缓存挂在进程单例上，`Db.name(...)` 每次新建 Query 也能命中，
   跨模块、跨函数调用共享同一份缓存。
2. **写操作自动失效** —— `insert / insert_all / update / delete` 会清除**同一库同一表**
   的全部缓存，因此不会出现"写入后仍读到旧值"。
3. **裸 SQL 需手动清理** —— `Db.execute('UPDATE ...')` 绕过查询构造器，不触发失效，
   此时请调用 `Db.clear_cache()`。
4. **TTL 语义** —— `cache(0)` 表示不缓存（每次穿透）；`cache(-1)` 抛
   `InvalidArgumentException`；同一条 SQL 的 TTL 以**首次写入缓存时**为准。
5. **容量上限** —— 默认最多 500 条，超出按 LRU 淘汰（`MemoryCacheStore(max_items=...)`）。
6. **结果隔离** —— 命中缓存时返回的是副本，修改返回值不会污染缓存内容。

自定义缓存键与后端：

```python
# 自定义键（按库+表参与失效）
Db.table('user').where('id', 1).cache(60, 'user:1').find()

# 旧写法仍兼容：cache(key, expire)
Db.table('user').where('id', 1).cache('user:1', 120).find()

# 替换后端（如接入 Redis / 文件缓存）
from tinkpyorm import CacheStore, cache

class MyStore(CacheStore):
    def get(self, key): ...
    def set(self, key, value, ttl=None): ...
    def delete(self, key): ...
    def clear(self, prefix=None): ...

old = cache.set_store(MyStore())
...
cache.set_store(old)          # 恢复原来的后端

Db.clear_cache()              # 清空全部缓存
Db.cache_store().stats()      # {'backend','items','hits','misses','hit_rate'}
```

> 缓存的是**查询返回的原始行**，命中后仍会重新应用 `json` / `with_attr` / `filter` /
> 模型转换，因此行为与未命中时完全一致。

---

## 流式读取

大结果集不要一次 `select()` 全量载入内存，按需选择下面两种方式：

```python
# 1) chunk()：分块，每批独立查询（线程安全），产出 Collection
for batch in Db.table('log').order('id').chunk(500):
    for row in batch:
        ...

# 2) cursor()：真流式，底层 fetchmany，逐行产出 dict（内存恒定）
for row in Db.table('log').order('id').cursor(chunk_size=1000):
    ...
```

| | `chunk(size)` | `cursor(chunk_size)` |
|---|---|---|
| 底层实现 | 多次 `LIMIT` 查询 | 游标 `fetchmany` |
| 连接锁 | 每批持锁（线程安全） | 迭代期间不持锁 |
| 一致性 | 跨批可能变化；需强一致请包事务 | 同一游标内一致 |
| 适用场景 | 批量处理 | 只读导出、全表扫描 |
| 模型绑定 | 每批产出模型集合 | 逐行产出模型实例 |

```python
# 大表迁移：chunk 读 + insert_all 写，全程低内存
with Db.transaction():
    for batch in Db.table('src').chunk(1000):
        Db.table('dst').insert_all([dict(r) for r in batch])
```

---

## 多线程使用

SQLite 驱动默认以 `check_same_thread=False` 建立连接，`Connection` 内部使用
**可重入锁（RLock）** 串行化 SQL 与事务，因此多线程共享同一连接是安全的：

```python
Db.set_config({'database': 'app.db', 'journal_mode': 'WAL'})

def worker(n):
    for i in range(100):
        with Db.transaction():              # 事务期间独占连接（其它线程排队）
            Db.table('log').insert({'msg': f'{n}-{i}'})

threads = [threading.Thread(target=worker, args=(k,)) for k in range(4)]
```

要点：

- **事务原子** —— 一个线程的 `commit/rollback` 不会影响另一个线程的事务状态；
  同线程嵌套事务走 `SAVEPOINT`，可重入不死锁。
- **事务独占连接** —— 事务期间其它线程的 SQL 排队等待，因此事务内避免耗时 IO。
- **并发写建议开启 WAL** —— `journal_mode: 'WAL'` 下读不阻塞写。
- **单线程可关闭锁** —— `Connection('app.db', thread_safe=False)` 省去加锁开销。
- **更高并发** —— 可为每个线程使用独立 `Connection`（WAL 模式下并发读更佳）。

---

## 性能实践建议

TinkPyORM 基于标准库 sqlite3，ORM 层的 Python 开销为**微秒量级**（主键点查约 20 µs）。
实际性能差距几乎全部来自**使用策略**（事务与 PRAGMA），量级为毫秒——相差三个数量级。
以下按收益排序：

**① 循环内写操作务必包事务（收益数百倍）**

SQLite 每次提交都伴随磁盘 fsync（本机约 8 ms/次）。ORM 默认逐条自动提交（与 think-orm
对齐），因此在循环中写数据时，用 `Db.transaction()` 包裹可把耗时从毫秒级降到微秒级：

```python
# 慢：每条 insert 一次 fsync
for u in users:
    Db.table("user").insert(u)

# 快：单事务，快 200–300 倍
Db.transaction(lambda: [Db.table("user").insert(u) for u in users])
# 或 with Db.transaction(): ...
```

**② 开启 WAL（连接配置一行，写入可提升数十倍）**

```python
Db.set_config({"database": "app.db", "journal_mode": "WAL"})
```

WAL 模式允许读与写并发，并把 `synchronous=NORMAL` 下的逐条提交成本从毫秒级降到百微秒级。
桌面应用（如 FreeVault 类本地数据库）与长驻服务均推荐。注意 WAL 会产生额外的
`-wal` / `-shm` 文件（数据库关闭/checkpoint 后合并回主库）。

**③ 批量写入用 `insert_all`（自动分批）**

```python
Db.table("user").insert_all(big_list)            # 自动按 32766//列数 分批，单事务原子
Db.table("user").insert_all(big_list, batch_size=500)
# 配合外层事务仍可再提速：Db.transaction(lambda: Db.table("user").insert_all(big_list))
```

**④ 高频点查 / 海量结果按需选择 API**

| 场景 | 推荐 | 原因 |
|---|---|---|
| 单行点查 | `Db.table().find(id)` 已足够 | 走快路径，ORM 开销约 20 µs |
| 极端高频（>5000 QPS） | 热点改用 `Db.query(sql, params)` | 绕过 Query/Builder 层，再省约 15 µs |
| 大结果集全量加载 | `column('name')` / `field(...)` 取必要列 | Model 实例内存约 4.9×，dict 约 2.1× |
| 超大数据集 | `page()`/`paginate()` 分页 | 避免全量驻留 |

**⑤ 生产环境关闭 SQL 日志**

```python
Db.sql_log_disable()   # 每条 SQL 约 220 字节；默认保留 1000 条，长驻进程请关闭或定期 clear
```

> 完整实测数据见仓库内 `PERFORMANCE.md`（15 个场景、与原生 sqlite3 三档对照、三轮中位数）。

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
| 流式 | `chunk(size)` `cursor(chunk_size)` |
| 缓存 | `cache(秒[, key])` |
| 写入 | `insert` `insert_all(list, batch_size=None)` `update` `save` `delete` `inc` `dec` |
| 关联 | `with_` |
| 其他 | `comment` `lock` `fetch_sql` `fail_exception` `allow_empty` `copy` `get_last_sql` |

### Model（静态）

`find` `find_or_empty` `find_or_fail` `get` `select` `select_or_fail` `create` `update` `destroy` `value` `column` `count` `sum` `avg` `max` `min` `paginate` `query` `with_trashed` `only_trashed`

### Model（实例）

`save` `delete` `force_delete` `restore` `together` `get_attr` `set_attr` `set_attrs` `get_relation` `to_dict` `to_array` `to_json` `hidden` `visible` `has_one` `has_many` `belongs_to` `belongs_to_many`

### Db（门面）

`set_config` `get_connection` `close` `table` `name` `raw` `query` `execute` `transaction` `clear_cache` `cache_store` `get_sql_log` `get_last_sql` `sql_log_clear` `sql_log_disable` `sql_log_enable`

---

## 与 think-orm 对照表

| think-orm (PHP) | TinkPyORM | 备注 |
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
# 单元测试
python test_tinkpyorm.py            # 33 项：查询构造 / 写入 / 事务 / 模型 / 软删除 / 关联
python test_drivers.py              #  9 项：驱动抽象层（注册表 / 方言钩子）
python test_cache.py                # 24 项：查询缓存（TTL / 失效 / LRU / 后端替换）
python test_stream_concurrency.py   # 21 项：流式读取（chunk/cursor）与多线程安全

# 冒烟测试（端到端，覆盖全链路 API）
python smoke_test.py

# README 示例回归测试（127 项，逐条校验本文档中的用法示例）
python test_readme_examples.py
```

预期输出（合计 87 项单元测试 + 127 项示例）：

```
Ran 33 tests in 1.6s
OK
...
README 示例：通过 127 项，失败 0 项
```

---

## 项目结构

```
TinkPyORM/
├── tinkpyorm/
│   ├── __init__.py       # 包导出（版本、公开 API）
│   ├── exceptions.py     # 异常体系（OrmError / DataNotFound / QueryError …）
│   ├── utils.py          # Raw/raw、标识符处理、命名转换、字段解析
│   ├── config.py         # 连接配置值对象（类型校验、options 归并）
│   ├── cache.py          # 查询缓存（进程级 + TTL + LRU + 可替换后端）
│   ├── drivers/          # 驱动抽象层（base / sqlite，可扩展多数据库方言）
│   ├── connection.py     # 连接门面：查询/执行/事务(SAVEPOINT)/日志/线程锁
│   ├── builder.py        # SQL 编译器：options → SQL（select/insert/update/delete）
│   ├── query.py          # 查询构造器：全链式 + 预载入 + 结果处理 + 缓存/流式
│   ├── collection.py     # Collection 结果集 + Paginator 分页
│   ├── relation.py       # 关联实现：4 种类型 + 批量预载入
│   ├── model.py          # Model 基类 + MetaModel 元类（scope/camelCase/静态代理）
│   └── db.py             # Db 门面：连接管理、入口、事务、日志、缓存
├── test_tinkpyorm.py           # 核心测试（33 项）
├── test_drivers.py             # 驱动抽象层测试（9 项）
├── test_cache.py               # 查询缓存测试（24 项）
├── test_stream_concurrency.py  # 流式读取与并发测试（21 项）
├── smoke_test.py               # 端到端冒烟测试
├── test_readme_examples.py     # README 示例回归测试（127 项）
├── benchmark_vs_sqlite3.py     # 与原生 sqlite3 的性能对照基准
├── PERFORMANCE.md              # 性能报告与优化记录
└── README.md
```

---

## 注意事项与已知限制

1. **内置驱动仅 SQLite**。驱动抽象层（`tinkpyorm.drivers`）已就位：新增数据库只需实现一个驱动类并注册，`Connection` / `Builder` / `Query` / `Model` 无需改动；MySQL / PostgreSQL 等驱动尚未提供。
2. **`with_` 不是 `with`**：`with` 是 Python 保留关键字，预载入方法必须写成 `with_()`。
3. **`raw()` 不做转义**。它按字面量拼进 SQL，只应传入你自己硬编码的表达式，绝不可传入未校验的用户输入。
4. **`without_field` 有额外开销**：SQLite 不支持 `SELECT * EXCEPT(col)`，实现上先查 `PRAGMA table_info()` 推导出完整字段列表再剔除，多一次元数据查询。
5. **`cache()` 是进程内内存缓存**（v0.4.0 起为进程级共享 + TTL + LRU 上限），非跨进程持久缓存，进程重启即失效。链式查询的写操作会自动清除同库同表缓存，裸 SQL（`Db.execute`）需手动调用 `Db.clear_cache()`。
6. **`right_join()` 依赖 SQLite 版本**：`RIGHT JOIN` / `FULL JOIN` 需 **SQLite ≥ 3.39**（Python 3.11+ 通常自带 3.39+）。旧版本会报语法错误，此时请改写为 `LEFT JOIN` 或升级 SQLite。
7. **`withTrashed()` / `onlyTrashed()` 需先启用软删除**：若模型未配置 `__soft_delete__`，调用会抛 `QueryError`。
8. **`lock()` 在 SQLite 下无实际效果**（SQLite 是文件级锁，`FOR UPDATE` 不适用），保留方法仅为 API 对齐。
9. **获取器不改变存储值**：它只在读取时转换，查询条件里仍应使用数据库里的原始值。
10. **模型属性访问未加载字段返回 `None`**，而非抛 `AttributeError`，这是刻意为之（避免模板里频繁判空）。
11. **未实现的 think-orm 特性**：虚拟模型、实体模型/分层、视图模型、数据自动验证器、MongoDB 支持、分布式与断点重连。

---

## 更新记录

### v0.4.0

**查询缓存重做 + 流式读取 + 连接级线程安全**（依能力评估结论落地：P0 两项 + P1 一项）：

1. **查询缓存真正可用** —— 此前 `_cache_store` 挂在 Query 实例上，而 `Db.name()` 每次都
   新建 Query，导致缓存**从不命中**。新增 `tinkpyorm/cache.py`：进程级单例 + TTL +
   LRU 容量上限（默认 500 条）+ `RLock` 线程安全 + 可替换后端
   （`CacheStore` / `MemoryCacheStore` / `cache.set_store()`）。
2. **`cache(秒)` 语义** —— 第一个参数即"倒计时秒数"：`cache(10)` 表示 10 秒后失效；
   `cache(0)` 不缓存；仍兼容旧签名 `cache(key, expire)`。缓存范围从仅 `select` 扩展到
   `find / select / value / column / count / sum / avg / max / min / paginate`。
3. **写操作自动失效与结果隔离** —— `insert / insert_all / update / delete` 按"库 + 表"
   前缀清除缓存，裸 SQL 提供 `Db.clear_cache()`；缓存键改为 MD5 摘要（键长恒定），
   命中时返回副本，修改返回值不再污染缓存。
4. **连接级线程安全** —— `Connection` 内置可重入锁，串行化 SQL 执行与事务，修复多线程
   共享同一连接时 `_in_transaction` / `_savepoint_depth` 被交叉覆盖导致的事务语义错乱；
   `Connection(..., thread_safe=False)` 可关闭锁（单线程省开销）。
5. **流式读取** —— 新增 `Query.chunk(size)`（分块、每批持锁、线程安全）与
   `Query.cursor(chunk_size)`（底层 `fetchmany` 真流式、内存恒定）；驱动层新增
   `Driver.select_stream()`（默认整体分块，SQLite 覆写为真流式）与 `Connection.query_stream()`。
6. **测试与文档** —— 新增 `test_cache.py`（24 项）、`test_stream_concurrency.py`（21 项），
   合计 33 + 9 + 24 + 21 项单元测试与 127 项 README 示例全量通过；README 新增
   「查询缓存」「流式读取」「多线程使用」三节，并修正已知限制中的缓存描述。

### v0.3.1

**撤销 v0.3.0 的集中式配置机制，保留驱动抽象层**（依使用反馈：集中式配置对单库
SQLite 场景过于复杂）：

1. **配置入口收敛回 `Db.set_config`** —— 移除 `Config` / `DatabaseManager` / `manager` /
   包级 `configure()` / `connection()` 以及 DSN / 环境变量 / 配置文件四种来源；
   `Db._connections` 恢复为唯一状态源，任意模块调用一次 `Db.set_config({...})` 即全局共享，
   用法与 v0.2.0 完全一致。
2. **保留并独立 `tinkpyorm.drivers` 驱动抽象层** —— `Connection` 委派 `Driver`、
   `Builder`/`Query` 方言下沉（`placeholder()` / `quote_identifier()` / `limit_sql()`）
   全部保留；`config.py` 裁剪为内部值对象（不再导出），删除 `manager.py`。
3. **保留的修复性行为** —— 未知 `type` 立即报错、未知键归入驱动 options 不再透传
   `sqlite3.connect()`、SQL 日志上限与开关、`insert_all` 自动分批、`find()` 快路径、
   `journal_mode` 配置。默认连接未配置时仍回退匿名内存库（v0.2.0 兼容行为）。
4. **兼容性** —— `Db.set_config` / `Connection()` 旧写法零改动；33 项既有测试 +
   9 项驱动层测试 + 112 项 README 示例全量通过。公开 API 移除项：`Config`、
   `DatabaseManager`、`configure()`、`connection()`、`parse_dsn`、`load_file`、
   `ConfigError`、`ConnectionNotFound`。

### v0.3.0

**架构：集中式配置 + 驱动抽象层**（详见 `docs/config-and-driver-refactor.md`）：

1. **`Config` + `DatabaseManager` 集中式配置** —— 修复 `Db.set_config` 时代"双状态不同步"的根因
   （`Db._connections` 与 `connection._default_connection` 两套独立状态未注册时静默回退内存库）。
   新增 `Config`（dict / DSN / env / file 四种来源）、`DatabaseManager`（命名注册 + 懒连接），
   推荐在应用启动时调用一次 `tinkpyorm.configure({...})`，之后任意模块直接 `Db.table(...)` /
   `tinkpyorm.connection()` 共享。
2. **驱动抽象层 `tinkpyorm.drivers`** —— 把 SQL 方言（占位符 `?` / `%s` / `:1`、标识符引用
   `` `x` `` / `"x"` / `[x]`、`LIMIT n OFFSET m`）下沉到 `Driver` 接口，新增数据库类型时只需
   实现一份驱动 + 注册，无需改动 `Connection` / `Builder` / `Query` / `Model`。`Builder`
   中 5 处硬编码 `?` 和 LIMIT 全部走 `driver.placeholder()` 与 `driver.limit_sql()`；
4. **`Query` 延迟解析连接** —— `Db.table(...)` 不再要求 `configure()` 已先调用；Query 句柄
   可先建，配置后置仍生效，消除导入顺序敏感。
5. **公开 API 零变更** —— `Db.set_config` 降级为兼容包装，33 项既有测试 + 48 项配置层新测
   + 9 项驱动层新测 + 112 项 README 示例全量通过。

### v0.2.0

健壮性与性能（基于与原生 sqlite3 的 15 场景基准评估，详见 `PERFORMANCE.md`）：

1. **`insert_all` 自动分批（修复严重缺陷）** —— 超过 SQLite 绑定变量上限（32766，6 列表约
   5461 行）的批量插入此前会抛 `too many SQL variables` 崩溃；现按 `32766 // 列数` 自动分批，
   并在同一事务内完成保证原子性（嵌套事务退化为 SAVEPOINT，语义不变）。可用
   `batch_size` 显式控制每批行数。
2. **SQL 日志加上限与开关（修复内存无限增长）** —— 默认仅保留最近 1000 条（环形裁剪），
   新增 `sql_log_enable(max_size=...)` / `sql_log_disable()` 与配置项 `sql_log_max`
   （`None` 表示不限制，兼容旧行为）。
3. **`journal_mode` 连接配置** —— `Db.set_config({"database": ..., "journal_mode": "WAL"})`
   在连接建立后自动执行对应 PRAGMA（内存库跳过），一行开启 WAL 等写优化。
4. **`find()` 单行快路径** —— 纯表查询（无模型/无 json/attr/filter/with 后处理）直接返回
   首行，跳过结果集包装与二次拷贝，主键点查 ORM 开销更低。

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

**TinkPyORM** 采用 [MIT License](LICENSE) 发布 —— 你可以自由使用、复制、修改、合并、出版发行、散布、再授权及销售本软件。

参考设计来源 **think-orm** 采用 [Apache-2.0 License](https://github.com/top-think/think-orm/blob/master/LICENSE)，版权归 ThinkPHP 官方团队所有。本项目仅参考其**设计思想与 API 命名**，不包含其源代码，二者许可证相互独立。

---

<div align="center">

**TinkPyORM** —— 用 Python 标准库，复刻 think-orm 的优雅

*Inspired by [ThinkPHP](https://www.thinkphp.cn) · [think-orm](https://github.com/top-think/think-orm)*

</div>
