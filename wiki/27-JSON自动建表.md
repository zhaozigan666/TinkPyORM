# JSON 自动建表模式

> [TinkPyORM](https://github.com/zhaozigan666/TinkPyORM) 技术文档 · [文档索引](Home.md) · 上一页：[26-Collection文档集合](26-Collection文档集合.md)

`Db.set_config` 的 `json` 配置键（v0.9.0，默认关闭）让 `Db.table()` / `Db.name()` 具备 **schemaless 写入**能力：表不存在自动创建、插入 dict 数据时缺失字段自动补列。与 [`DocumentCollection`](26-Collection文档集合.md)（独立文档集合入口）互补——本模式作用于**普通链式查询**。

## 快速上手

```python
from tinkpyorm import Db

Db.set_config({"database": "app.db", "json": True})

# 表不存在 → 自动 CREATE TABLE（id 自增主键），字段不存在 → 自动 ALTER TABLE ADD COLUMN
uid = Db.name("events").insert({"type": "click", "payload": {"x": 1}, "tags": ["ui"]})

# 再次插入出现新字段 → 自动加列，旧行该列为 NULL
Db.name("events").insert({"type": "scroll", "depth": 82.5})

# 正常链式查询；dict/list 存为 JSON 文本，json() 解码回读
Db.name("events").where("type", "=", "click").json(["payload"]).select()
```

## 行为细则

| 行为 | 说明 |
|---|---|
| 自动建表 | 表不存在时 `CREATE TABLE IF NOT EXISTS <表> (id INTEGER PRIMARY KEY AUTOINCREMENT)`；读路径（select/find/count/chunk）遇到缺表同样先建表，返回空集而非报错 |
| 自动加列 | 仅写入路径（`insert` / `insert_all`）；每次写入对照 `PRAGMA table_info` 补缺失列（不缓存列清单，多实例/多进程并发补列安全） |
| 类型推断 | `bool`/`int` → INTEGER，`float` → REAL，其余（str / dict / list / None）→ TEXT；dict/list 由既有写入路径自动 JSON 编码 |
| 主键 | 自动建表主键为 `id`；数据里带 `"id"` 键则显式指定，未带则自增 |
| 前缀 | `Db.name()` 自动带上 `prefix` 配置的前缀（如 `app_logs`） |
| `insert_all` | 放宽"各行字段一致"约束：以各行字段**并集**为准，缺失字段补 `None` |
| 解码回读 | JSON 文本列用 `.json()` 三档（自动嗅探 / 指定字段 / 关闭）解码，见 [JSON 查询与格式化](11-JSON查询与格式化.md) |
| 缓存 | 自动写入后照常失效同表查询缓存 |

## 边界与限制

- **主表参与，关联表不参与**：join 查询只对主表自动建表，关联表（`options['join']`）不会创建，缺表仍报错。
- **update 不补列**：无行可更新时建列无意义，`update` 引用不存在的列仍报错。
- **fetch_sql 不产生 DDL**：`fetch_sql()` 模式只返回 SQL 文本，不执行任何建表/加列。
- **字段名须为裸标识符**：`^[A-Za-z_][A-Za-z0-9_]*$`，否则抛 `InvalidArgumentException`（防注入，与库的全量白名单校验风格一致）。
- **仅关系型驱动**：依赖 `PRAGMA table_info` 与 `ALTER TABLE ADD COLUMN`（当前 SQLite 完整支持）；NoSQL/自定义驱动开启此开关无效果（写入会因缺表报错，不会静默半生效）。
- **已有表零侵入**：对已存在的表只追加缺失列，不动任何现有列与数据。
- **类型是"建议"**：SQLite 动态类型，首次见到的值决定声明类型；同一列混存不同类型时比较语义遵循 SQLite 规则（与 DocumentCollection 的 `promote` 无类型列同理）。

## 与 DocumentCollection 的取舍

| | `json: True` + `Db.table()` | `Db.collection()` |
|---|---|---|
| 存储 | 每字段一列（可读性好，可直接 SQL 级操作） | 单表 `_id` + `data` JSON 列 |
| 新字段 | `ALTER TABLE ADD COLUMN`（DDL，量极大时有 schema 膨胀风险） | 无 DDL，任意嵌套 |
| 适合 | 字段集有限且缓慢增长的"宽表"场景 | 字段高度动态 / 深嵌套文档 |
| 提升/索引 | 直接对列建索引 | `promote()` + `ensure_index()` |

两者可同库共存；同一张表不要混用两种方式写入。

## 导航

- 上一页：[26-Collection文档集合](26-Collection文档集合.md)
- 返回：[文档索引](Home.md)
- 相关：[06-写入操作](06-写入操作.md) · [11-JSON查询与格式化](11-JSON查询与格式化.md) · [24-注意事项与已知限制](24-注意事项与已知限制.md)
