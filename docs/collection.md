# 文档集合（DocumentCollection）设计

> 对应实现：`tinkpyorm/document.py`（v0.8.0）。本文记录**为什么这样设计**；
> 用法见 [wiki/26-Collection文档集合](../wiki/26-Collection文档集合.md)。

## 1. 目标与需求

用户诉求：**无需提前建立数据库与数据库字段就能直接操作**——MongoDB 式的开发体验，但存储后端保持 SQLite。同时用户明确了三个边界：

- 操作符**不用 MongoDB 的 `$` 字典风格**，沿用库内既有的 think-orm 链式词汇；
- 保留一个**可选的字段提升**机制；
- 字段校验是否开启由 `Db.set_config` 配置控制；
- **不做 MySQL 驱动**（本特性随 SQLite 落地）。

## 2. 存储路线选型

| 路线 | 结论 | 理由 |
|---|---|---|
| EAV（行转列） | 否 | 聚合 / 排序 / 多条件查询的 SQL 复杂且慢，一行文档拆 N 行 |
| 动态建列（首次写入自动 `ALTER TABLE ADD COLUMN`） | 否 | DDL 频繁、字段爆炸后表结构失控、两环境行为不一致 |
| **JSON 文档列**（单表 `_id` + `data`） | 采用 | 零迁移、天然 schemaless、路径级原子更新（v0.6.0 已解决并发正确性） |

选 JSON 文档列的决定性因素是**既有基建的复用率**：v0.5.0 的 `where_json` 家族、v0.6.0 的路径写入、v0.7.0 的操作列表，已经是"文档式操作"的完整原语。`DocumentCollection` 只是一层翻译，不需要新的存储引擎。

## 3. 分层：Query 之上的薄封装

`DocumentCollection` 内部持有一个 `Query`（表名固定为集合表），所有操作翻译后走既有链路：

| DocumentCollection | 翻译到 Query |
|---|---|
| `where("profile.city", "=", x)` | `where_json("data", "$.profile.city", "=", x)` |
| `where("age", ">=", x)`（age 已提升） | `where("age", ">=", x)`（真实列） |
| `order("age", "desc")` | 提升列 → `order`；否则 `order_json` |
| `insert(doc)` | `insert({"data": doc})`（dict 值自动序列化） |
| `update(doc)` | `update({"data": doc})`（整列替换） |
| `update_path({路径: 值})` | `update_json("data", {归一化路径: 值}, ifnull={})` |
| `patch(partial)` | `update_json_patch("data", partial, ifnull={})` |

由此**免费继承**：参数绑定与三重白名单校验、写操作缓存失效、事务与 SAVEPOINT、连接级线程锁、`fetch_sql` 调试。Collection 层自己只做三件事：路径→列/路径解析、文档 dict 与存储行的互转、生成列与索引的 DDL。

**设计取舍**：翻译层大量调用 `Query` 的私有方法（`_where` / `_json_where`）。同包内复用可接受——公开这两个方法反而会扩大 API 面。

## 4. 字段提升：为什么是生成列

初版方案是"物化真实列 + 写入时同步维护列与 JSON"，同步逻辑是主要复杂度与出错点。SQLite 的 **VIRTUAL 生成列**（3.31+）消掉了它：

```sql
ALTER TABLE users ADD COLUMN age GENERATED ALWAYS AS (json_extract(data,'$.age')) VIRTUAL
```

- 值由引擎在读取时计算，**写路径零改动**，永不失同步；
- 加列为元数据操作，瞬时完成、不回填、不锁表；
- 可直接建普通索引。

**代价与边界**（均已文档化）：

1. 生成列对 `PRAGMA table_info` 隐藏（hidden=2/3），列清单必须用 `table_xinfo` 读取——首版实现踩过此坑；
2. 表达式不可变更，换类型只能删列重建（SQLite 3.35+ 支持 DROP COLUMN）；
3. 注册表（路径 → 列名）从 `sqlite_master.sql` 的 DDL 文本解析，不引入元数据表。解析正则要求引号**成对**且容忍可选类型名：驱动 `quote_identifier` 用反引号，SQLite 原样存储；无配对约束时 `age INTEGER GENERATED …` 中的 `INTEGER` 会被误捕获为列名（第二版实现踩过此坑）。

**为什么默认不声明 `sql_type`**：SQLite JSON 函数返回原生类型（JSON 数字 → INTEGER/REAL，字符串 → TEXT），无类型列保持同一语义；声明类型会引入 affinity 隐式转换，属于用户显式选择的行为。

**为什么没有 CAST**：评估期曾计划对路径条件统一包 `CAST(json_extract(...) AS …)` 稳定类型，实现时放弃——SQLite 的 `json_extract` 已返回确定类型，`where_json` 既有语义（含 `where("x", None)` → IS NULL 覆盖"缺失与 null"）与 MongoDB 直觉一致；引入 CAST 反而与底层 `where_json` 的表达式形态分叉，破坏"薄封装"不变量。类型一致性交给可选的 schema 校验。

## 5. 状态机：终端复位

`Query` 是"链式累积 options"的模型，复用后继承了一个隐性行为：终端执行后条件仍在。文档集合的使用者心智是 MongoDB 式的"每次 `where(...)` 链以终端收尾"，同一个 collection 实例会连续发起多个独立查询——叠加旧条件会产出静默错误结果（首版测试抓到此问题）。

因此每个终端方法（读 / 写 / DDL）执行后调用 `_finish()` 复位内部 Query。链式状态只存在于一次 `where…终端` 之间。

## 6. Schema 校验

- 开关放 `Db.set_config` 的 `collection_schema_mode`（一等配置键，进 `COMMON_KEYS`——否则会被 `Config.from_dict` 归入驱动 options 而静默失效）。默认关闭；
- 校验范围刻意收窄：只查**已声明路径上已出现的值**，不要求必填、不拒绝未声明字段——schemaless 的价值就在"字段自由"，校验只兜类型一致性；
- `bool` 与 `int` / `float` 互不兼容（Python `bool` 是 `int` 子类，必须显式排除）；`float` 宽容接受 `int`；
- schema 键以**归一化 JSON 路径**存储（`age` → `$.age`），与 `update_path` 的查找一致。

## 7. 不做 MySQL 的影响

- 范围收敛为 SQLite JSON1 + 生成列，无 5.7/8.0 差异、无方言分叉；
- 生成列在 MySQL 5.7+ 同样存在，`ensure_index` 的表达式索引对应函数索引/多值索引——**架构留白，未承诺实现**；
- Collection 层只调用 Query 层 API，未来 MySQL 驱动落地后理论上自动可用，届时需补的仅是 DDL 方言（`ensure_index` / `promote` 两处）。

## 8. 已知限制

1. 数组元素级查询（`$elemMatch` 类语义）不支持；固定下标 `tags[0]` 可用；
2. `update` / `delete` 必须带条件（继承自 Query 的防误伤语义）；
3. 高频路径不提升、不建索引即为全表扫描（与 MongoDB 无索引时同罪，但有 `ensure_index` 兜底）；
4. `promote` 后的列不可改表达式，换类型需删列重建。

## 9. 测试

`test_collection.py` 59 项，覆盖：幂等建表、CRUD 全路径、点路径条件全操作符、null/exists/contains 语义、数组下标、排序分页、`distinct` / `group_counts`、表达式索引（含 unique + 缺失路径）、生成列提升（嵌套路径列名 / 自定义列名 / 重复提升拒绝 / 保留名拒绝）、schema 校验开关两态、非法输入拒绝、多集合隔离。
