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
| 组合 | `union` `union_all` `cte(name, query[, columns])` |
| 分组排序 | `group` `having` `having_or` `order` `order_raw` |
| 限制 | `limit` `page` `paginate` |
| 查询 | `select` `select_or_fail` `find` `find_or_empty` `find_or_fail` `value` `column` `count` `sum` `avg` `max` `min` `paginate` `explain` |
| 流式 | `chunk(size)` `cursor(chunk_size)` |
| 缓存 | `cache(秒[, key])` |
| 写入 | `insert` `insert_all(list, batch_size=None)` `upsert(data[, update])` `update` `save` `delete` `inc` `dec` |
| 关联 | `with_` |
| 其他 | `comment` `lock` `fetch_sql` `fail_exception` `allow_empty` `copy` `get_last_sql` |

## FtsIndex / FtsQuery（全文检索，v0.10.0）

入口：`Db.fts(name)`。详见 [28-全文检索](28-全文检索.md)。

| 类别 | 方法 |
|---|---|
| 索引 | `create(rebuild=False)` `drop()` `rebuild()` `optimize()` `integrity_check()` `info()` |
| 检索 | `search(query)` `select()` `first()` `count()` `to_sql()` |
| 链式 | `rank([reverse])` `order(field[, direction])` `limit(n[, offset])` `page(p, n)` `where(*args, **kwargs)` `highlight(col[, open, close])` `snippet(col[, ...])` `cache(秒)` |
| 工具 | `fts_quote(text)` |
| 模型 | `Model.fts_index()` `Model.search(query)`（需声明 `__fts__`） |

## MongoCollection（MongoDB 集合，v0.12.0）

仅 `type="mongodb"` 的连接可用；详见 [30-MongoDB支持](30-MongoDB支持.md)。

| 分组 | 方法 |
|---|---|
| 条件 | `where(field, op, value)` `where_or(...)` `where_raw(filter)` `where_in(field, values)` `where_null(field)` `where_exists(field)` |
| 排序 / 投影 / 分页 | `order(field, direction)` `field(*names)` `exclude(*names)` `limit(length, offset)` `page(page, list_rows)` `after(last_id)` |
| 读取 | `select()` `find(pk)` `find_by_id(oid)` `count()` `distinct(field)` `aggregate(pipeline)` `explain()` |
| 写入 | `insert(doc)` `insert_many(docs)` `insert_many_ordered(docs)` `update(changes, upsert)` `update_many(changes, upsert)` `update_raw(update_doc, many, upsert)` `replace(doc, upsert)` `delete(pk)` `delete_many()` `bulk_write(requests)` |
| 索引 | `ensure_index(fields, unique, name)` `drop_index(name)` `indexes()` |
| 校验 | `schema(mapping, required)` |

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

## RedisStore（Redis 键空间，v0.13.0）

仅 `type="redis"` 的连接可用；`Db.redis("app:")` 的第一个参数是**键前缀**，
所有键方法自动补前缀，`scan_iter` / `keys` 返回去前缀后的逻辑键名。
详见 [31-Redis支持](31-Redis支持.md)。

| 分组 | 方法 |
|---|---|
| 字符串 | `set(key, value, ex/px/exat/pxat/nx/xx/keepttl/get)` `setex` `psetex` `setnx` `get` `getdel` `getex` `mset` `mget` `append` `strlen` |
| JSON | `set_json` `get_json`（整键序列化；键不存在默认抛错，`default=None` 才返回 None） |
| 计数 | `incr` `decr` `incrbyfloat` |
| 键 / 过期 | `exists` `delete` `unlink` `type_of` `rename` `renamenx` `touch` `expire(nx/xx/gt/lt)` `pexpire` `expireat` `persist` `ttl` `pttl` `expiretime` `randomkey` |
| 扫描 | `scan_iter(match, count, type)` `keys` `dbsize` `clear(match)` |
| 哈希 | `hset` `hsetnx` `hget` `hmget` `hgetall` `hdel` `hexists` `hkeys` `hvals` `hlen` `hincrby` `hincrbyfloat` `hrandfield` `hscan_iter` `hexpire` `httl` `hpersist` |
| 列表 | `lpush` `rpush` `lpop` `rpop` `lrange` `llen` `lindex` `lset` `lrem` `ltrim` `lpos` `lmove` `lmpop` `blpop` |
| 集合 | `sadd` `srem` `smembers` `sismember` `smismember` `scard` `spop` `srandmember` `sunion` `sinter` `sdiff` `sintercard` `sscan_iter` |
| 有序集合 | `zadd` `zrange` `zrangebyscore` `zscore` `zmscore` `zrank` `zincrby` `zrem` `zcard` `zcount` `zpopmin` `zpopmax` `zremrangebyscore` `zscan_iter` |
| 批量 / 订阅 | `pipeline(transaction=True)` `publish` `subscribe` |
| 其他 | `ping` `info` `config_get` `server_version` `topology` `key` `logical` `client` `close` |

## Model（实例）

`save` `delete` `force_delete` `restore` `together` `get_attr` `set_attr` `set_attrs` `get_relation` `to_dict` `to_array` `to_json` `hidden` `visible` `has_one` `has_many` `belongs_to` `belongs_to_many`

## Db（门面）

`config` `set_config` `get_connection` `close` `table` `name` `collection` `fts` `mongo` `redis` `raw` `query` `execute` `transaction` `clear_cache` `cache_store` `get_sql_log` `get_last_sql` `sql_log_clear` `sql_log_disable` `sql_log_enable`

## 配置键（set_config 一等键）

| 键 | 默认 | 说明 |
|---|---|---|
| `database` / `type` / `prefix` … | - | 连接基础配置，见 [01-连接配置](01-连接配置.md) |
| `sql_log_enabled` / `sql_log_max` | True / 1000 | SQL 日志开关与上限 |
| `path_expand` | True | 数据库路径展开 `~` 与环境变量 |
| `collection_schema_mode` | False | 文档集合写入期 schema 校验（[26](26-Collection文档集合.md)） |
| `json` | False | 自动建表/建列模式（[27-JSON自动建表](27-JSON自动建表.md)） |
| `cache_backend` / `cache_dir` | memory / - | 查询缓存后端：`memory` / `file`（file 须提供目录，见 [19-查询缓存](19-查询缓存.md)） |
| `version` | 5.7.0 / 4.0.0 / 7.0.0 | 数据库产品版本：MySQL / MongoDB / Redis 各自消费（[29](29-MySQL支持.md) / [30](30-MongoDB支持.md) / [31](31-Redis支持.md)） |
| `database` / `options.db`（Redis） | 0 | **db 序号**（默认 16 个：0–15），不是库名；两种写法等价，与 URL 的 `/N` 冲突时直接报错（[31](31-Redis支持.md)） |
