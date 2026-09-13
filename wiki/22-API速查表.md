# API 速查表

> [TinkPyORM](https://github.com/zhaozigan666/TinkPyORM) 技术文档 · [文档索引](Home.md) · 上一页： [21-性能实践建议](21-性能实践建议.md) · 下一页： [23-与think-orm对照表](23-与think-orm对照表.md)


## Query（链式）

| 类别 | 方法 |
|---|---|
| 表 | `table` `name` `alias` |
| 字段 | `field` `field_raw` `without_field` `distinct` `json` `with_attr` `filter` |
| 条件 | `where` `where_or` `where_xor` `where_null` `where_not_null` `where_in` `where_not_in` `where_like` `where_not_like` `where_between` `where_not_between` `where_column` `where_exists` `where_not_exists` `where_raw` |
| JSON | `json([fields])` `where_json` `where_json_or` `where_json_null` `where_json_not_null` `where_json_exists` `where_json_not_exists` `where_json_contains` `where_json_not_contains` `where_json_length` `where_json_type` `field_json` `order_json` |
| JSON 写入 | `update_json` `update_json_insert` `update_json_remove` `update_json_patch` |
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

## DocumentCollection（文档集合，v0.8.0）

入口：`Db.collection(name)`。详见 [26-Collection文档集合](26-Collection文档集合.md)。

| 类别 | 方法 |
|---|---|
| 链式 | `where` `where_or` `where_length` `order` `limit` `page` `schema` |
| 读取 | `select` `find(pk)` `value` `column` `count` `distinct` `group_counts` |
| 写入 | `insert` `insert_all` `update` `update_path` `patch` `delete` |
| DDL | `promote(path, sql_type?, column?)` `ensure_index(path, unique?, name?)` |

条件操作符与 `Query.where` 同一套（`tinkpyorm.JSON_OPS`）；字段名即 JSON 路径。

## Model（静态）

`find` `find_or_empty` `find_or_fail` `get` `select` `select_or_fail` `create` `update` `destroy` `value` `column` `count` `sum` `avg` `max` `min` `paginate` `query` `with_trashed` `only_trashed`

## Model（实例）

`save` `delete` `force_delete` `restore` `together` `get_attr` `set_attr` `set_attrs` `get_relation` `to_dict` `to_array` `to_json` `hidden` `visible` `has_one` `has_many` `belongs_to` `belongs_to_many`

## Db（门面）

`set_config` `get_connection` `close` `table` `name` `collection` `raw` `query` `execute` `transaction` `clear_cache` `cache_store` `get_sql_log` `get_last_sql` `sql_log_clear` `sql_log_disable` `sql_log_enable`

## 配置键（set_config 一等键）

| 键 | 默认 | 说明 |
|---|---|---|
| `database` / `type` / `prefix` … | - | 连接基础配置，见 [01-连接配置](01-连接配置.md) |
| `sql_log_enabled` / `sql_log_max` | True / 1000 | SQL 日志开关与上限 |
| `path_expand` | True | 数据库路径展开 `~` 与环境变量 |
| `collection_schema_mode` | False | 文档集合写入期 schema 校验（[26](26-Collection文档集合.md)） |
| `json` | False | 自动建表/建列模式（[27-JSON自动建表](27-JSON自动建表.md)） |
