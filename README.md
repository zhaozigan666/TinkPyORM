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

**本文件**：[简介](#简介) · [特性](#特性) · [零依赖](#零依赖) · [安装](#安装) · [快速开始](#快速开始) · [更新记录](#更新记录) · [致谢与灵感来源](#致谢与灵感来源) · [许可证](#许可证)

**技术文档**：共 25 页，见 [文档导航](#文档导航)，或直接打开 [`wiki/Home.md`](wiki/Home.md)

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
- **JSON 字段查询** —— `json()` 结果自动格式化为 Python `dict`；`where_json` 家族支持路径条件（比较 / 存在 / 包含 / 长度 / 类型），`field_json` / `order_json` 提取与排序，写入侧自动序列化；SQL 形态由驱动提供，可扩展至 MySQL / MongoDB / Redis
- **JSON 路径写入** —— `update_json` / `update_json_insert` / `update_json_remove` / `update_json_patch` 在 SQL 内改写嵌套字段，无读改写窗口（并发交错不丢更新），与路径查询对称；`update_json_ops` 在一条语句内按序混合多种操作
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

## 文档导航

技术细节按主题拆分在 [`wiki/`](wiki/Home.md) 目录下：

**入门**

- [01-连接配置](wiki/01-连接配置.md) —— 字典 / 路径 / Connection 三种配置方式、驱动与连接参数

**查询**

- [02-查询构造器与条件](wiki/02-查询构造器与条件.md) —— 链式入口、WHERE 条件全景（基础 / 逻辑组合 / 语义化快捷方法）
- [03-聚合分组排序分页](wiki/03-聚合分组排序分页.md) —— count·sum·max、group + having、order、limit 与分页
- [04-JOIN与UNION](wiki/04-JOIN与UNION.md) —— join / inner / left / right、union 与 unionAll
- [05-原生表达式](wiki/05-原生表达式.md) —— raw() 在 WHERE、UPDATE、字段片段中的用法与安全边界
- [16-结果集与分页](wiki/16-结果集与分页.md) —— Collection 行为与 Paginator 分页输出
- [17-camelCase别名](wiki/17-camelCase别名.md) —— 蛇形列名映射为驼峰输出的规则

**写入**

- [06-写入操作](wiki/06-写入操作.md) —— insert / insertAll / save / update / inc / dec / delete 与自动分批
- [07-事务](wiki/07-事务.md) —— 回调模式、上下文模式、嵌套事务与 savepoint
- [11-JSON查询与格式化](wiki/11-JSON查询与格式化.md) —— 对象自动序列化、结果自动解码为 dict、路径条件查询、路径选取与排序
- [12-JSON路径写入](wiki/12-JSON路径写入.md) —— 路径级局部更新、操作列表入口、并发正确性、安全性与多库扩展

**模型**

- [08-模型](wiki/08-模型.md) —— 定义、CRUD、序列化、隐藏字段、批量赋值
- [09-获取器与修改器](wiki/09-获取器与修改器.md) —— 读取时转换与写入时处理
- [10-类型转换](wiki/10-类型转换.md) —— 字段类型自动转换与自定义转换器
- [13-软删除与查询范围](wiki/13-软删除与查询范围.md) —— withTrashed / onlyTrashed、恢复与物理删除、全局 Scope
- [14-模型事件](wiki/14-模型事件.md) —— before / after 各阶段事件的注册与触发
- [15-关联](wiki/15-关联.md) —— 定义关联、懒加载、预载入（解决 N+1）、关联写入、级联保存

**运行与调优**

- [18-调试与SQL日志](wiki/18-调试与SQL日志.md) —— fetchSql、getLastSql、SQL 日志上限与查询分析
- [19-查询缓存](wiki/19-查询缓存.md) —— TTL、自动失效、自定义键与后端替换
- [20-流式读取与多线程](wiki/20-流式读取与多线程.md) —— chunk / cursor 流式读取、连接级 RLock 与多线程要点
- [21-性能实践建议](wiki/21-性能实践建议.md) —— 事务批量写入、索引与实测基准指引

**参考**

- [22-API速查表](wiki/22-API速查表.md) —— Query / Model / Db 方法一览
- [23-与think-orm对照表](wiki/23-与think-orm对照表.md) —— API 与语义迁移对照，标出差异
- [24-注意事项与已知限制](wiki/24-注意事项与已知限制.md) —— 使用前必读的行为约定与限制清单
- [25-开发与测试](wiki/25-开发与测试.md) —— 运行测试、项目结构与开发约定

**设计文档与基准**

- [`docs/json-query.md`](docs/json-query.md) —— JSON 能力的驱动层设计、多库扩展映射与代码骨架
- [`docs/config-and-driver-refactor.md`](docs/config-and-driver-refactor.md) —— 配置层与驱动抽象层的重构设计
- [`PERFORMANCE.md`](PERFORMANCE.md) —— 与原生 sqlite3 的 15 场景实测基准

---

## 更新记录

### v0.7.1

**修复：`type` 校验与驱动注册表脱节，自定义驱动无法配置**（破坏"新增数据库只需
实现驱动并注册"的扩展承诺）：

1. **根因** —— `config.normalize_type()` 只对照静态白名单 `KNOWN_TYPES`
   （由内置别名表 `TYPE_ALIASES` 派生）判定合法性，与 `drivers._DRIVERS`
   活注册表**完全脱节**。于是 `register_driver("oracle", OracleDriver)` 之后
   `{"type": "oracle"}` 仍被 `Config` 拒绝，扩展路径在配置层即断裂。此前测试
   只能用 `object.__new__(Config)` 绕过校验构造配置，是这一缺陷的直接症状。
2. **修复** —— 校验改为"先查内置保留名（快路径、零导入），未命中再查活驱动
   注册表"。`drivers/base.py` 反向依赖 `config`（`from ..config import Config`），
   故注册表查询必须**惰性导入**，否则形成循环导入；驱动层抛错时退化为内置集合，
   不连带阻断配置构造。注册表查询不缓存——注册动作之后立即生效。
3. **新增 `available_type_names()`** —— 返回当前可配置的类型名（内置保留名 ∪
   已注册驱动名），并从包顶层导出，便于排查"为何我的 type 被拒"。
4. **错误信息可执行化** —— 未注册类型改为列出**活注册表**清单并给出
   `register_driver('名字', YourDriver)` 的注册指引。
5. **边界保持不变** —— 别名解析（`mariadb` → `mysql`）与"预留名"语义不变：
   `mysql` / `postgresql` / `mssql` / `mongodb` / `redis` 配置阶段放行，到建立
   连接时才因缺少驱动实现抛 `DriverNotAvailable`。
6. **测试** —— `test_drivers.py` 由 9 项扩至 16 项：新增 `TestCustomDriverConfig`
   覆盖自定义驱动的配置构造、`from_dict`、`Db.set_config` 端到端真实查询、
   别名解析、预留名边界、未注册拒绝、注册表非缓存；同时移除既有用例中的
   `object.__new__(Config)` 绕过写法，改为走真实构造路径。合计 335 项单元测试
   + 174 项 README 示例全量通过。
7. **文档重组** —— README 由 1847 行精简至 473 行，只保留门面信息（简介 / 特性 /
   零依赖 / 安装 / 快速开始 / 文档导航 / 更新记录 / 致谢 / 许可证）；技术章节按
   主题拆分到 [`wiki/`](wiki/Home.md) 共 25 页，每页带「上一页 / 下一页 / 返回索引」
   导航。拆分经**多重集校验**：67 个代码块前后完全一致（零丢失、零改写），
   正文仅有的差异是目录改写与一处刻意修正的相对链接。设计文档仍在 `docs/`，
   性能数据仍在 `PERFORMANCE.md`。
### v0.7.0

**JSON 路径写入的操作列表入口**（一条语句内混合 `set` / `insert` / `remove` / `patch`）：

1. **`Query.update_json_ops(field, ops, ifnull=None)`** —— 接受操作列表，按序折叠
   进**同一条** UPDATE：`[('set','$.a',1), ('remove','$.b'), ('patch', {...})]`
   编译为 `json_patch(json_remove(json_set(col,'$.a',?),'$.b'), json(?))`。
2. **五种操作元素写法** —— `(模式, 路径, 值)`（`set` / `insert`）、
   `(模式, 路径)`（`remove`，路径传 `list` 可一次删多个）、`(模式, 补丁文档)`
   （`patch`，补丁自带键路径）、`{路径: 值}`（多路径简写）、`JsonUpdate` 实例
   （自带列名，可在一条语句内改多个 JSON 列）。模式名大小写不敏感。
3. **顺序语义确定** —— 列表顺序即应用顺序：`[set, remove]` 与 `[remove, set]`
   作用于同一路径时结果相反。相邻同类操作合并进同一次函数调用，既避免
   "同列多个 SET 子句只有最后一个生效"的静默丢更新，也不改变语义
   （SQLite 对同一次调用内的重复路径同样是后者胜）。
4. **构造期整体校验** —— 全部元素先解析校验，任一非法即抛
   `InvalidArgumentException`，不执行写入，也不在查询构造器上留下半成品规格。
5. **Model 层对称入口** —— `Model.update_json_ops(field, ops, where, ifnull)`，
   `where` 写法与 `Model.update` 一致（闭包 / dict / 元组 / 主键值）。
6. **表达式求值基准（易踩坑，已文档化）** —— `raw()` 表达式引用的是**列本身**，
   同一语句内的多次写入看到的都是同一个原始列值。因此同一列表内写两次
   `json_extract(col,'$.n') + 1` 只净增 1（嵌套成
   `json_set(json_set(...), ...)` 也改变不了，外层读的仍是原列）；要链式
   引用上一步结果必须拆成多条语句。常量写入不受此限，顺序语义严格成立。
7. **测试与文档** —— `test_json.py` 由 176 项扩至 241 项，合计 328 项单元测试
   + 174 项 README 示例全量通过；README 新增「一条语句内混合多种操作」小节。

### v0.6.0

**JSON 路径写入（局部更新）**——补齐与路径查询对称的写入侧能力，形成
"写入序列化 → 路径查询 → 路径写入 → 结果解码"的完整闭环：

1. **驱动层新增写入扩展点** —— `Driver` 增加 `json_set` / `json_insert` /
   `json_remove` / `json_patch` / `json_bind`；`SQLiteDriver` 给出完整参考实现。
   `json_bind` 解决两个必须包装的场景：容器值若不包 `json(?)` 会被存成
   **转义字符串**；布尔若直接绑定会被 sqlite3 适配成整数 1/0，丢失 JSON 类型
   （现按 JSON 文本 `'true'` / `'false'` 绑定，`json_type` 报 `true` / `false`）。
2. **Query 新增四个终端方法** —— `update_json`（`json_set`，存在覆盖、不存在
   新增）/ `update_json_insert`（`json_insert`，不覆盖已有值）/
   `update_json_remove`（`json_remove`，支持多路径）/
   `update_json_patch`（`json_patch`，RFC 7396）。均支持 `where` 条件组合、
   `fetch_sql`、缓存自动失效、事务回滚。
3. **多路径原子写入** —— `update_json(field, {'$.a': 1, '$.b': 2})` 编译为
   **一次** `json_set` 调用。这是正确性要求而非优化：SQL 中同一列出现多个
   SET 子句时只有最后一个生效，拆开会导致静默丢更新。
4. **空列兜底 `ifnull`** —— `json_set(NULL, ...)` 返回 NULL（更新静默无效），
   传 `ifnull={}` / `[]` 令驱动编译为 `COALESCE(col, '{}')`。兜底文档是常量
   字面量，不引入绑定参数、无注入面。
5. **Model 层入口** —— `Model.update_json` 族，`where` 写法与 `Model.update`
   一致（闭包 / dict / 元组 / 主键值）。抽出 `_apply_where` 供两者复用。
6. **安全** —— 列名、路径、`ifnull` 三重校验；值一律绑定（含容器与布尔，
   以 JSON 文本绑定后由 `json(?)` 解析）；同字段同时出现在普通更新数据与路径
   写入中直接抛 `QueryError`。
7. **顺带清理** —— `builder.py` 中 `RawWhere` 重复定义（后定义覆盖前者）已删除；
   `_UNSET` 哨兵统一下沉到 `utils.UNSET` 供 Model 层复用；`Driver.json_encode`
   支持 `tuple`（底层驱动无法绑定元组，原行为是晦涩的绑定错误）。
8. **测试与文档** —— `test_json.py` 由 78 项扩至 176 项，合计 263 项单元测试
   + 169 项 README 示例全量通过；README 新增「路径级局部更新」；新增
   `docs/json-query.md` 写入侧章节；`PERFORMANCE.md` 新增第十二节（实测差异
   与并发丢更新对照）。

### v0.5.0

**JSON 字段查询与自动格式化**（写入自动序列化 → 路径条件查询 → 结果自动解码闭环）：

1. **驱动层 JSON 能力契约** —— `Driver` 新增 `supports_json` 与 `json_extract` /
   `json_exists` / `json_contains` / `json_length` / `json_type` / `json_decode` /
   `json_encode` 七个扩展点；`SQLiteDriver` 给出完整参考实现（`json_extract` /
   `json_each` / `json_type`）；`NoSQLDriver` 覆写编解码为**恒等透传**，
   使 MongoDB / Redis 复用同一条"结果格式化为 dict"的代码路径。
   路径经 `normalize_json_path()` 白名单校验后内联，杜绝路径注入。
2. **写入自动序列化** —— `insert` / `insert_all` / `update` 遇 `dict` / `list` 值
   自动编码为 JSON 文本（走驱动 `json_encode`，保持方言无关），
   不再需要手动 `json.dumps`。
3. **结果自动格式化** —— `json()` 支持三档：无参自动嗅探、`json([...])` 指定字段
   （零嗅探开销）、`json(False)` 关闭；覆盖 `find / select / value / column /
   chunk / cursor / paginate` 与模型属性；模型声明 `__json__` 后查询自动接入，
   `_deserialize` 对已解码的 dict / list 幂等返回。
4. **JSON 路径条件** —— 新增 `where_json` / `where_json_or` / `where_json_null` /
   `where_json_not_null` / `where_json_exists` / `where_json_not_exists` /
   `where_json_contains` / `where_json_not_contains` / `where_json_length` /
   `where_json_type`，以及 `field_json` / `order_json`。新增 `JsonWhere` 条件对象，
   由 Builder 在编译期向驱动取方言表达式，Query 层不含任何方言知识。
5. **安全与边界** —— 路径、列名、别名三重白名单校验（`InvalidArgumentException`）；
   `dict` / `list` 直接比较显式报错并引导到正确 API；不支持的驱动抛
   `UnsupportedOperation` 而非静默生成错误 SQL。
6. **测试与文档** —— 新增 `test_json.py`（78 项），合计 292 项测试全量通过；
   README 新增「JSON 字段查询」章节，新增 `docs/json-query.md`（设计说明 +
   MySQL / PostgreSQL / MongoDB / Redis 扩展映射）。

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

---
