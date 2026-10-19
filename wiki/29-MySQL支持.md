# 29-MySQL 支持（5.7 / 8.x / 9.x）

> [TinkPyORM](https://github.com/zhaozigan666/TinkPyORM) 技术文档 · [文档索引](Home.md) · 上一页： [28-全文检索](28-全文检索.md)

MySQL 驱动自 **v0.11.0** 起提供，按配置的版本号启用/禁用对应语法。本文说明：
版本怎么解析、各版本差在哪、同一条 API 在不同版本下生成什么 SQL、以及哪些能力会被拒绝。

## 配置与版本

```python
# 方式一：字典
Db.config({"type": "mysql", "version": "8.0.36",
           "host": "127.0.0.1", "port": 3306,
           "user": "app", "password": "***", "database": "app"})

# 方式二：直接传参（关键字）
Db.config(type="mysql", version="8.0", host="127.0.0.1",
          user="app", password="***", database="app")

# Db.config 是 Db.set_config 的别名，两者完全等价
```

未给 `version` 时**按 5.7 处理**（`5.7.0`）。`version` 是 v0.11.0 起的一等配置键，
位置配置与关键字参数不可混用（同时传入会抛 `InvalidArgumentException`）。

依赖：`pip install pymysql` 或 `pip install "tinkpyorm[mysql]"`。未安装时
`get_driver("mysql")` 抛 `DriverNotAvailable` 并提示安装命令，SQLite 用户不受影响。

## 版本解析与校验

| 输入 | 归一结果 | 说明 |
|---|---|---|
| `None` / `""` | `5.7.0` | 缺省按 5.7 |
| `"5.7"` / `5.7` / `5.7.0` | `5.7.0` | 缺省段补 0 |
| `"8"` / `8` | `8.0.0` | 单段 |
| `"8.0.36"` / `(8, 0, 36)` / `[8, 0, 36]` | `8.0.36` | 三段 |
| `"8.0.36-0ubuntu0.22.04.1"` | `8.0.36` | 忽略 `-` 后缀 |
| `"5.7.44-log"` | `5.7.44` | 同上 |
| `"5.6.51"` / `"7.0"` / `"8.5"` / `"abc"` | **报错** | 见下方支持范围 |

规则：

1. 接受 `str` / `int` / `float` / 2~3 元组 / `Version`；
2. 布尔值被显式拒绝（`True` 是 `int` 子类，容易误传）；
3. 超过三段的纯数字（`8.0.36.1`）**不**做截断，直接报错——只有 `-`/`+`/`~` 之后的内容才被视为后缀；
4. 版本比较按三元组字典序，`8.0.19 > 8.0.2`。

**支持范围**：`5.7.x`、`8.0.x–8.4.x`、`9.x`。范围外立即报错：

```
InvalidArgumentException: 不支持的 MySQL 版本: 8.5.0。
支持范围: 5.7.x / 8.0.x–8.4.x / 9.x；未给 version 时默认按 5.7.0（5.7）处理
```

选择"报错"而非"就近猜测"：猜错版本会让门禁静默放行不存在的语法（执行期报错）
或静默拦截可用的语法（功能缺失），两种都比启动期失败更难排查。

## 版本差异矩阵

| 能力 | 最低版本 | 5.7 | 8.0.x | 9.x | 备注 |
|---|---|---|---|---|---|
| JSON 函数族（EXTRACT/CONTAINS/SET/…） | 5.7.8 | ✅ | ✅ | ✅ | 5.7 需 5.7.8+ |
| 生成列 / ngram 全文解析器 | 5.7.6 | ✅ | ✅ | ✅ | 本库未封装 DDL |
| `JSON_MERGE_PATCH`（RFC 7396） | 5.7.22 | ⚠️ <5.7.22 禁用 | ✅ | ✅ | `update_json_patch()` 门禁 |
| CTE（`WITH`） | 8.0.1 | ❌ | ✅ | ✅ | `Query.cte()` |
| `SKIP LOCKED` / `NOWAIT` | 8.0.1 | ❌ | ✅ | ✅ | `Query.lock()` |
| 窗口函数 | 8.0.2 | ❌ | ✅ | ✅ | 走 `field_raw()`，能力可查询 |
| `JSON_TABLE` | 8.0.4 | ❌ | ✅ | ✅ | 规划中，`supports_feature` 可查 |
| 函数索引 | 8.0.13 | ❌ | ✅ | ✅ | DDL 层能力 |
| `DEFAULT (expr)` | 8.0.13 | ❌ | ✅ | ✅ | DDL 层能力 |
| `CHECK` 约束真正生效 | 8.0.16 | ⚠️ 解析但忽略 | ✅ | ✅ | 5.7 不校验 |
| 多值索引 | 8.0.17 | ❌ | ✅ | ✅ | DDL 层能力 |
| upsert 行别名 `AS new` | 8.0.19 | ❌（降级） | ✅ | ✅ | 见"降级" |
| 不可见索引 | 8.0.23 | ❌ | ✅ | ✅ | DDL 层能力 |
| `INTERSECT` / `EXCEPT` | 8.0.31 | ❌ | ✅ | ✅ | 规划中 |
| `mysql_native_password` | 5.0 | ✅ | ✅ | ❌ 9.0 移除 | 连接认证，非 SQL |

同一 8.0.x 行内的小版本差异是真实存在的（例如 8.0.19 才有行别名），
因此矩阵给的是**精确到补丁号的最低版本**，不是"8.0 全有/全无"。

## 三类行为：静默可用 / 降级 / 报错

| 情形 | 策略 | 例子 |
|---|---|---|
| 全版本都有等价语法 | 静默可用 | 基础 CRUD、`LIMIT`、JSON 函数（5.7.8+） |
| 新语法能改写成旧写法 | **降级** | upsert：8.0.19+ 用行别名，旧版本用 `VALUES(col)` |
| 没有等价写法 | **报错** | 5.7 的 `WITH`、`SKIP LOCKED`、`<5.7.22` 的 `JSON_MERGE_PATCH` |

报错统一为 `UnsupportedOperation`，文案包含"所需版本 + 当前版本 + 修改建议"，
例如：

```
UnsupportedOperation: cte 需要 MySQL >= 8.0.1，当前配置版本 5.7.44；
请在 Db.config(version=...) 中声明实际版本
```

### 降级的实际输出

`upsert({"id": 1, "name": "x"}, update=["name"])`：

```sql
-- 5.7.44（降级，功能等价）
INSERT INTO `user` (`id`, `name`) VALUES (%s, %s)
ON DUPLICATE KEY UPDATE `name` = VALUES(`name`)

-- 8.0.19+（行别名，VALUES() 自 8.0.20 起弃用）
INSERT INTO `user` (`id`, `name`) VALUES (%s, %s) AS new
ON DUPLICATE KEY UPDATE `name` = new.`name`
```

### 门禁的实际输出

| 版本 | `Db.table("u").cte("paid", sub)` |
|---|---|
| 5.7.44 | `UnsupportedOperation: cte 需要 MySQL >= 8.0.1` |
| 8.0.36 | ``WITH `paid` AS (SELECT …) SELECT …`` |

| 版本 | `lock("skip locked")` |
|---|---|
| 5.7.44 | `UnsupportedOperation: skip_locked 需要 MySQL >= 8.0.1` |
| 8.0.36 | `… FOR UPDATE SKIP LOCKED` |

## JSON 方言归一

SQLite 与 MySQL 的 `JSON_TYPE` 词汇不同（MySQL 返回大写，且把布尔叫 `BOOLEAN`），
本库在 SQL 层用 `CASE` 归一，保证 `where_json_type()` 的入参跨库一致：

| Python 侧取值 | SQLite | MySQL（归一后） |
|---|---|---|
| `text` | `text` | `STRING` → `text` |
| `real` | `real` | `DOUBLE` / `DECIMAL` → `real` |
| `true` / `false` | `true` / `false` | `BOOLEAN` → `IF(expr,'true','false')` |
| `integer` / `array` / `object` / `null` | 同名 | 同名（转小写） |

`where_json_contains()` 的候选值在 MySQL 必须是**合法 JSON**，驱动会自动
`CAST(%s AS JSON)` 并绑定 `json.dumps(value)`——裸字符串 `vip` 直接绑定会触发
MySQL 错误 3141。

## 连接、字符集与事务

| 项 | 行为 |
|---|---|
| 字符集 | 显式 `utf8mb4`（5.7 默认 latin1、8.0 默认 utf8mb4，统一显式指定避免乱码） |
| 默认排序规则 | 5.7 `utf8mb4_unicode_ci`；8.0/9.x `utf8mb4_0900_ai_ci` |
| autocommit | `False`；由 `Connection` 显式提交（不在事务中时每条写操作后自动 commit） |
| 事务原语 | 交给 pymysql 的 `begin()/commit()/rollback()`，不把 `BEGIN` 当普通语句执行 |
| DDL | MySQL 的 DDL 会**隐式提交**当前事务，与 SQLite 行为不同；DDL 请放在事务外 |
| 流式读取 | `SSCursor` 服务端游标，`chunk()`/`cursor()` 可用 |

## 不支持的能力

| 能力 | 状态 | 说明 |
|---|---|---|
| `Db.collection()` 文档集合 | `UnsupportedOperation` | 依赖 SQLite 的 `table_xinfo`/`sqlite_master`/生成列 |
| `Db.fts()` 全文检索 | `UnsupportedOperation` | MySQL 是 `FULLTEXT` + `MATCH…AGAINST`，语义与 FTS5 不同，另作规划 |
| `RETURNING` | 不存在 | MySQL 无此语法（MariaDB 才有），不要按 MariaDB 文档使用 |
| `Query.upsert()` | 仅 MySQL | SQLite 的 `ON CONFLICT` 需要显式冲突目标，无法一一对应 |

## 测试与验证边界

`test_mysql.py` 共 **47 项**，全部为**离线**测试：只验证版本解析、能力矩阵、
SQL 文本与门禁行为，不建立网络连接。这意味着：

- 已保证：生成的 SQL 语法形态、占位符、参数顺序、门禁时机；
- **未保证**：真实服务器上的执行结果（需要可用的 MySQL 实例）。

自测方式：

```python
Db.config({"type": "mysql", "version": "8.0.36", "host": "...",
           "user": "...", "password": "...", "database": "..."})
Db.get_connection().ping()            # True 表示连通
Db.table("user").limit(1).select()
```

## API 速览

```python
Db.config(type="mysql", version="8.0.36", host=..., user=..., password=..., database=...)

driver = Db.get_connection().driver
driver.version                      # Version(8, 0, 36)
driver.supports_feature("cte")      # True / False
driver.require_feature("cte")       # 不足时抛 UnsupportedOperation
driver.features()                   # {能力: {min, removed_in, supported}}
driver.dialect_info()               # 版本 / 字符集 / 排序规则 / 能力快照

Db.table("u").cte("paid", sub).select()          # >= 8.0.1
Db.table("u").lock("skip locked").select()       # >= 8.0.1
Db.table("u").upsert({"id": 1, "name": "x"}, ["name"])   # 全版本（SQL 形态随版本降级）
Db.table("u").where("id", 1).update_json_patch("extra", {...})  # >= 5.7.22
```
