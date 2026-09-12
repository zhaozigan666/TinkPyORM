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

**技术文档**：共 25 页 —— 在线版 [GitHub Wiki](https://github.com/zhaozigan666/TinkPyORM/wiki)，或见 [文档导航](#文档导航) / 仓库内 [`wiki/Home.md`](wiki/Home.md)

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

技术细节按主题拆分在 [`wiki/`](wiki/Home.md) 目录下，并同步发布到
[**GitHub Wiki**](https://github.com/zhaozigan666/TinkPyORM/wiki)（带侧边栏导航，在线阅读更方便）：

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

`type` 校验修复；文档重组并发布到 GitHub Wiki。

- `type` 校验改为对照活驱动注册表，`register_driver()` 注册的类型可正常配置
- 新增并导出 `available_type_names()`
- 未注册类型的报错列出全部可配置类型与 `register_driver()` 用法
- 别名解析与保留名行为不变：`mariadb` → `mysql`；`mysql` / `postgresql` / `mssql` / `mongodb` / `redis` 配置放行、连接时报 `DriverNotAvailable`
- `test_drivers.py` 9 → 16 项，新增 `TestCustomDriverConfig`；合计 335 项单元测试 + 174 项示例通过
- README 精简为门面文档，技术章节拆分为 [`wiki/`](wiki/Home.md) 共 25 页
- 新增 `scripts/sync_wiki.py`，技术文档发布至 [GitHub Wiki](https://github.com/zhaozigan666/TinkPyORM/wiki)

### v0.7.0

一条语句内混合多种 JSON 操作。

- 新增 `Query.update_json_ops(field, ops, ifnull=None)` 与 `Model.update_json_ops(...)`
- 支持五种操作元素：`(模式, 路径, 值)`、`(模式, 路径)`、`(模式, 补丁)`、`{路径: 值}`、`JsonUpdate` 实例；模式名大小写不敏感
- 列表顺序即应用顺序；相邻同类操作合并进同一次函数调用
- 全部元素在构造期校验，非法即抛 `InvalidArgumentException`
- 同一条语句内多次写入引用的是原始列值，链式引用需拆成多条语句
- `test_json.py` 176 → 241 项；合计 328 项单元测试 + 174 项示例通过
- 文档新增「一条语句内混合多种操作」小节

### v0.6.0

JSON 路径级局部更新。

- 驱动层新增 `json_set` / `json_insert` / `json_remove` / `json_patch` / `json_bind`
- 新增 `Query.update_json` / `update_json_insert` / `update_json_remove` / `update_json_patch`，及 `Model` 层同名方法
- 多路径写入编译为单次 `json_set` 调用
- 新增 `ifnull={}` / `[]`，列为空时编译为 `COALESCE(col, '{}')`
- 列名、路径、`ifnull` 三重校验；值一律绑定（含容器与布尔）
- 同字段同时出现在普通更新数据与路径写入中抛 `QueryError`
- 删除 `builder.py` 中重复的 `RawWhere` 定义；`_UNSET` 下沉为 `utils.UNSET`；`Driver.json_encode` 支持 `tuple`
- `test_json.py` 78 → 176 项；合计 263 项单元测试 + 169 项示例通过
- 新增 `docs/json-query.md` 写入侧章节；`PERFORMANCE.md` 新增第十二节

### v0.5.0

JSON 字段查询与结果自动格式化。

- 驱动层新增 `supports_json` 与 `json_extract` / `json_exists` / `json_contains` / `json_length` / `json_type` / `json_decode` / `json_encode` 七个扩展点
- `insert` / `insert_all` / `update` 遇 `dict` / `list` 值自动序列化为 JSON
- `json()` 三档：自动嗅探、`json([...])` 指定字段、`json(False)` 关闭；覆盖 `find` / `select` / `value` / `column` / `chunk` / `cursor` / `paginate` 与模型属性
- 模型声明 `__json__` 后查询自动接入
- 新增 `where_json` 家族 10 个条件方法与 `field_json` / `order_json`
- 路径、列名、别名三重白名单校验；`dict` / `list` 直接比较显式报错；不支持的驱动抛 `UnsupportedOperation`
- 新增 `test_json.py`（78 项）；合计 292 项测试通过
- 新增 `docs/json-query.md`；文档新增「JSON 字段查询」章节

### v0.4.0

查询缓存重做、流式读取、连接级线程安全。

- 新增 `tinkpyorm/cache.py`：进程级缓存 + TTL + LRU（默认 500 条）+ `RLock` + 可替换后端
- 查询缓存改为 `cache(秒)` 语义，缓存范围扩展至 `find` / `select` / `value` / `column` / `count` / `sum` / `avg` / `max` / `min` / `paginate`
- 写操作按"库 + 表"前缀清缓存；缓存键改为 MD5 摘要；命中时返回副本
- `Connection` 内置可重入锁，串行化 SQL 执行与事务；`Connection(..., thread_safe=False)` 可关闭
- 新增 `Query.chunk(size)` 与 `Query.cursor(chunk_size)`；驱动层新增 `Driver.select_stream()`，连接层新增 `Connection.query_stream()`
- 新增 `test_cache.py`（24 项）、`test_stream_concurrency.py`（21 项）；合计 87 项单元测试 + 127 项示例通过
- 文档新增「查询缓存」「流式读取」「多线程使用」三节

### v0.3.1

撤销集中式配置，保留驱动抽象层。

- 配置入口收敛回 `Db.set_config`；移除 `Config` / `DatabaseManager` / `manager` / 包级 `configure()` / `connection()`，以及 DSN / 环境变量 / 配置文件四种来源
- 保留 `tinkpyorm.drivers` 驱动抽象层；`config.py` 裁剪为内部值对象（不再导出）；删除 `manager.py`
- 保留未知 `type` 报错、未知键归入驱动 options、SQL 日志上限与开关、`insert_all` 自动分批、`find()` 快路径、`journal_mode` 配置
- 移除的公开 API：`Config`、`DatabaseManager`、`configure()`、`connection()`、`parse_dsn`、`load_file`、`ConfigError`、`ConnectionNotFound`
- `Db.set_config` / `Connection()` 旧写法零改动；33 项既有测试 + 9 项驱动层测试 + 112 项示例通过

### v0.3.0

集中式配置与驱动抽象层。

- 新增 `Config`（dict / DSN / env / file 四种来源）与 `DatabaseManager`（命名注册 + 懒连接）
- 新增包级 `configure()` 与 `connection()`
- 新增 `tinkpyorm.drivers` 驱动抽象层，SQL 方言（占位符、标识符引用、分页）下沉至 `Driver`
- `Query` 延迟解析连接，`Db.table(...)` 不再要求先调用 `configure()`
- 公开 API 零变更；`Db.set_config` 降级为兼容包装
- 33 项既有测试 + 48 项配置层新测 + 9 项驱动层新测 + 112 项示例通过
- 新增 `docs/config-and-driver-refactor.md`

### v0.2.0

健壮性与性能。

- `insert_all` 超过绑定变量上限时自动分批（可用 `batch_size` 控制），同一事务内保证原子性
- SQL 日志默认保留最近 1000 条；新增 `sql_log_enable(max_size=...)` / `sql_log_disable()` 与配置项 `sql_log_max`
- 新增 `journal_mode` 连接配置项，连接建立后自动执行对应 PRAGMA（内存库跳过）
- `find()` 纯表查询走单行快路径，跳过结果集包装

### v0.1.1

行为修正。

- 修改器 `set_xxx_attr` 对 `create()` / `Model(**kwargs)` 构造数据生效
- 查询范围可在链式任意位置、任意多个串联；实例方法与 `@classmethod` 两种定义均支持
- 显式设置 `__table__` 时 `__prefix__` 生效
- `to_json()` 内置 `default=str`，`date` / `datetime` / `time` 序列化为 ISO 字符串
- 新增 `union_all()`（对应 think-orm 的 `unionAll`）

### v0.1.0

首个可用版本：查询构造器、模型、关联、软删除、事务、分页。

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
