# Redis 支持（v0.13.0）

TinkPyORM 的 Redis 数据访问层：连接配置 + 命名空间键空间 + 五种数据类型的读写
+ 过期/扫描/流水线/发布订阅 + 版本与拓扑门禁。

> 与 SQL 侧的关系：Redis **不套用** `Query` / `Builder` / `Model`
> （它们是为关系型语义设计的）。Redis 侧只有两层：
> 驱动层 [`tinkpyorm/drivers/redis.py`]（连接 / 版本 / 异常）+ 服务层
> [`tinkpyorm/redis.py`]（`RedisStore`，键空间访问）。
> 设计取舍见仓库 `docs/redis.md`。

---

## 1. 安装与连接

Redis 驱动是**可选依赖**，SQLite 路径仍然零依赖：

```bash
pip install "tinkpyorm[redis]"     # 等价于 pip install redis>=4.5
```

三种等价写法（推荐用连接串，密码里的特殊字符由 `quote` 转义）：

```python
from tinkpyorm import Db

# ① 分散字段
Db.config({"type": "redis", "host": "127.0.0.1", "port": 6379,
           "password": "secret", "database": 0})

# ② 直接传参（与 ① 完全等价）
Db.config(type="redis", host="127.0.0.1", port=6379,
          password="secret", database=0)

# ③ 连接串（支持 redis:// / rediss:// / unix://）
Db.config({"type": "redis",
           "options": {"url": "redis://:secret@127.0.0.1:6379/0"}})
```

### 1.1 `database` 是 db 序号

Redis 的 ``database`` 与 SQL 的"库名"完全不同：它是 **0–15 的整数序号**
（由服务端 ``databases`` 配置决定，默认 16 个）。因此：

- 必须能转成整数，否则**配置期直接报错**（不会静默当成 0）；
- `Config` 的默认值 `":memory:"` 对 Redis 而言等于"未设置"，会回落到 0 或环境变量；
- 集群模式（`cluster_enabled=1`）只有 db 0，`SELECT` 不可用。

### 1.2 配置来源优先级

**显式配置 > 配置中心回调 > 环境变量 > 默认值**

| 层 | 写法 | 说明 |
|---|---|---|
| 显式 | `Db.config(host=..., options={"url": ...})` | 字段与 `options` 同名时以 `Config` 字段为准 |
| 配置中心 | `options={"config_resolver": lambda: {...}}` | 回调返回 dict，**每次连接都会调用**，天然支持热更新；回调异常直接上抛 |
| 环境变量 | `REDIS_URL` / `REDIS_HOST` / `REDIS_PORT` / `REDIS_PASSWORD` / `REDIS_USER` / `REDIS_DB`（`REDIS_DATABASE` 亦可） | 首个非空者胜出 |
| 默认 | `127.0.0.1:6379`、db 0、`socket_connect_timeout=5` | 无密码时不发 AUTH |

```python
# 配置中心示例：凭据不进代码、不进版本库
Db.config({"type": "redis",
           "options": {"config_resolver": lambda: {
               "host": vault.get("redis_host"),
               "password": vault.get("redis_password"),
               "database": 0,
           }}})
```

### 1.3 常用连接选项（`options`）

| 键 | 默认 | 说明 |
|---|---|---|
| `decode_responses` | **True** | 返回 `str` 而非 `bytes`；二进制场景显式设 False |
| `socket_connect_timeout` | 5.0 | 建连超时（秒）。redis-py 默认无超时，配置错误会**无限挂起** |
| `socket_timeout` | 无 | 命令读写超时。设了会让 `BLPOP` 这类阻塞命令提前报错 |
| `health_check_interval` | 0 | 空闲连接探活间隔（秒） |
| `max_connections` | 无上限 | 连接池上限；高并发服务务必设置 |
| `client_name` | - | 便于在 `CLIENT LIST` 里定位来源 |
| `protocol` | 2 | 3 = RESP3（需 redis-py ≥ 5.0、Redis ≥ 6.0） |
| `ssl` / `ssl_cert_reqs` / `ssl_ca_certs` | - | TLS 连接（`rediss://`） |
| `retry_on_timeout` | False | 超时重试 |

> **未知选项不会被透传**：只有上表白名单里的键会交给 redis-py，其余静默忽略
> （与本项目"配置错误要显式报错"的风格一致——拼错选项名不会变成 `TypeError`，
> 但也不会生效；需要透传时请用连接串的查询参数）。

---

## 2. 版本解析与校验

`version` 是一等配置键，与 MySQL / MongoDB 侧同名同机制：

```python
Db.config({"type": "redis", "version": "7.4", ...})
```

### 2.1 解析规则

- 接受 `str` / `int` / `float` / 2–3 元组：`"7.4"`、`7`、`7.4`、`(7, 2)`；
- 缺省段补 0：`"7"` → `7.0.0`；
- 允许发行版后缀：`"8.10.2-alpine"` → `8.10.2`；
- 拒绝空串、非数字段、布尔值。

### 2.2 支持范围

**4.0.x / 5.0.x / 6.0.x / 6.2.x / 7.0.x / 7.2.x / 7.4.x / 8.x**，缺省 **7.0.0**。

Redis 的小版本是**发布线**而非补丁号（6.2 与 7.2 是独立发布线），因此按
`(major, minor)` 精确匹配；只有 8.x 放开所有小版本（8.0 起改为快速发布节奏，
8.2 / 8.4 / 8.10 都是正式版）。越界（3.2、6.1、7.1、9.0）**配置期直接报错**，
不做"就近猜版本"——猜错会让门禁静默放行或静默拦截。

### 2.3 探测优先，声明退化为断言

Redis 的版本可以廉价探测（`INFO server` 的 `redis_version`），所以：

```python
driver = Db.get_connection().driver
driver.detect_version()      # -> Version(8, 10, 2)（真实服务端）
driver.version               # 探测值 > 声明值 > 缺省 7.0.0
driver.topology              # 'standalone' / 'cluster' / 'sentinel'
driver.is_replica            # True / False
```

| 情况 | 行为 |
|---|---|
| 有 `INFO` 权限 | 以**实际探测值**为准 |
| `INFO` 被 ACL 拒绝 | 退化为"声明值 > 缺省值"，不把权限问题误报成连接问题 |
| 声明 > 实际 | **连接期报错**（配置与现实不符） |
| 声明 < 实际 | 允许（把新服务端当旧版本用是安全的） |

---

## 3. 版本差异矩阵

所有差异集中在 `REDIS_FEATURES`（最低版本）与 `REDIS_REMOVED`（移除版本），
业务代码只问 `driver.supports_feature(...)`，不写版本 if：

```python
driver.supports_feature("hash_field_expiry")   # 7.4+
driver.require_feature("lmpop")                # 不足时抛 UnsupportedOperation
driver.features()                              # 全量快照 {名字: {min, supported}}
```

| 能力（功能名） | 最低版本 | 说明 |
|---|---|---|
| `unlink` / `object_freq` / `memory_usage` | 4.0 | 异步删除、LFU 频次、内存占用 |
| `stream` / `zpopmin` | 5.0 | Streams 数据类型 |
| `acl` / `resp3` / `client_tracking` / `tls` | 6.0 | 权限模型、RESP3、客户端缓存、TLS |
| `lpos` / `set_keepttl` | 6.0 | 列表查找、`SET ... KEEPTTL` |
| `getdel` / `getex` | 6.2 | 原子取走 / 取值同时改 TTL |
| `set_get` / `set_exat_pxat` | 6.2 | `SET ... GET`、绝对时间过期 |
| `smismember` / `lmove` / `copy` / `zmscore` / `zadd_gt_lt` / `zrangestore` | 6.2 | 批量判断、跨列表移动、键复制 |
| `function` / `lmpop` / `zmpop` / `sintercard` | 7.0 | Functions、多键弹出、交集基数 |
| `expire_nx_xx_gt_lt` / `sharded_pubsub` | 7.0 | 带条件的过期、集群分片订阅 |
| `waitaof` / `client_no_touch` | 7.2 | AOF 确认、客户端不更新 LRU |
| `hash_field_expiry` | 7.4 | 哈希**字段级** TTL（`HEXPIRE` / `HTTL` / `HPERSIST`） |
| `hash_field_getex` | 8.0 | `HGETEX` / `HGETDEL` / `HSETEX` |
| `vector_set` | 8.0 | Vector Set 数据类型 |

`REDIS_REMOVED` 目前为空：Redis 的公开命令几乎不做硬移除（多以 deprecated
形式长期保留）。保留该表是为了让"某个版本移除某项能力"有可落地的门禁位置，
而不是把判断散进业务代码。

### 3.1 部署形态门禁

**版本够不代表能用**。集群模式只有 db 0、多键命令要求同 slot，这类差异
在 `REDIS_TOPOLOGY_FEATURES` 里单独建模：

| 能力 | 允许形态 | 说明 |
|---|---|---|
| `select_db` | standalone / sentinel | 集群无 `SELECT`，只能 db 0 |
| `multi_key_unrestricted` | standalone / sentinel | 集群下 `MGET`/`SMOVE` 等要求同 slot |

```python
# 集群部署
driver.topology                          # 'cluster'
driver.supports_feature("select_db")     # False
driver.require_feature("select_db")
# UnsupportedOperation: select_db 需要 standalone / sentinel 部署，
#                       当前是 cluster（该形态不支持此能力）
```

拓扑未知（`INFO` 拿不到）时**不拦截**，只按版本判断——避免 ACL 权限不足
导致能力查询被误杀。

---

## 4. 键空间与命名空间

`Db.redis(name)` 的 `name` 是**命名空间前缀**（Redis 没有表/集合，最接近的
概念就是键前缀）：

```python
cache = Db.redis("app:")        # 前缀 app:
cache.set("user:1", "张三")      # 实际键名 app:user:1
cache.key("user:1")             # 'app:user:1'（调试用）
cache.logical("app:user:1")     # 'user:1'
```

- 所有键方法**自动补前缀**，业务代码只写逻辑键名；
- `scan_iter()` / `keys()` 返回的也是**逻辑键名**（已去前缀）；
- 不传 `name` 时使用连接配置里的 `prefix`（与 SQL 侧共用同一约定）；
- 键名支持 `str` / `int` / `float` / `bytes`，空串与布尔会被拒绝。

---

## 5. API 总览

### 5.1 字符串

```python
cache.set("k", "v")                       # 标量：str/int/float/bool/bytes
cache.set("k", "v", ex=60)                # 相对过期（秒）；px 为毫秒
cache.set("k", "v", exat=1700000000)      # 绝对过期（>= 6.2）
cache.set("k", "v", nx=True)              # 仅当不存在（分布式锁原语）
cache.set("k", "v", get=True)             # 返回旧值（>= 6.2）
cache.get("k", default="fallback")
cache.getdel("k")                         # 原子取走（>= 6.2）
cache.getex("k", ex=60)                   # 取值并续期（>= 6.2）
cache.mset({"a": 1, "b": 2})              # 单次往返批量写
cache.mget("a", "b", "c")                 # 缺失位置为 None
cache.incr("cnt", 5) / cache.decr("cnt") / cache.incrbyfloat("f", 1.5)
cache.append("k", "x") / cache.strlen("k")
```

**标量约束是刻意的**：`dict` / `list` 传给 `set()` 会抛
`InvalidArgumentException` 并提示用 `set_json()`。隐式序列化会让 `get()` 的
返回类型取决于写入路径，读代码的人无法判断取回的是字符串还是对象。

```python
cache.set_json("user:1:tags", ["vip", "beta"])   # 整键 JSON
cache.get_json("user:1:tags")                    # ['vip', 'beta']
cache.get_json("missing")                        # 抛 QueryError
cache.get_json("missing", default=None)          # 显式给默认值才返回 None
```

### 5.2 哈希

```python
cache.hset("user:1:profile", {"name": "张三", "age": 25})   # 返回新增字段数
cache.hset("user:1:profile", city="北京")                    # 关键字写法亦可
cache.hget("h", "name", default=None) / cache.hmget("h", "a", "b")
cache.hgetall("h") / cache.hkeys("h") / cache.hvals("h") / cache.hlen("h")
cache.hdel("h", "a", "b") / cache.hexists("h", "a")
cache.hincrby("h", "n", 3) / cache.hincrbyfloat("h", "n", 0.5)
cache.hscan_iter("h")                     # 大 hash 的推荐读法（增量）

# 字段级过期（Redis >= 7.4）
cache.hexpire("h", 60, "a")               # -> [1]
cache.httl("h", "a")                      # -> [剩余秒数]，-1 无过期 / -2 字段不存在
cache.hpersist("h", "a")
```

### 5.3 列表 / 集合 / 有序集合

```python
# 列表
cache.rpush("l", "a", "b", "c") / cache.lpush("l", "x")
cache.lrange("l", 0, -1) / cache.lindex("l", 1) / cache.llen("l")
cache.lpop("l") / cache.rpop("l", count=2) / cache.ltrim("l", 0, 99)
cache.lset("l", 1, "B") / cache.lrem("l", 0, "a") / cache.lpos("l", "a")
cache.lmove("src", "dst", "LEFT", "RIGHT")      # >= 6.2
cache.lmpop(["l1", "l2"], "LEFT", count=1)      # >= 7.0

# 集合
cache.sadd("s", "a", "b") / cache.srem("s", "a") / cache.scard("s")
cache.smembers("s") / cache.sismember("s", "a")
cache.smismember("s", "a", "z")                 # >= 6.2 -> [True, False]
cache.sunion("s1", "s2") / cache.sinter(...) / cache.sdiff(...)
cache.sintercard(["s1", "s2"])                  # >= 7.0（只算数量）
cache.sscan_iter("s")                           # 大集合的推荐读法

# 有序集合
cache.zadd("rank", {"张三": 98.5, "李四": 91})
cache.zrange("rank", 0, 9, withscores=True)     # 排行榜前 10
cache.zrange("rank", 0, -1, desc=True)          # 倒序
cache.zrangebyscore("z", 60, 100, count=10)     # 分值区间 + 分页
cache.zscore("z", "张三") / cache.zrank("z", "张三", desc=True)
cache.zincrby("z", 1.5, "张三") / cache.zcard("z") / cache.zcount("z", 0, 100)
cache.zpopmin("z") / cache.zpopmax("z") / cache.zrem("z", "张三")
```

### 5.4 键管理 / 扫描

```python
cache.exists("a", "b")            # 计数（不是布尔）
cache.delete("a", "b") / cache.unlink("a")     # unlink 后台回收，大键优先
cache.type_of("h")                # 'string' / 'hash' / 'list' / 'set' / 'zset' / 'none'
cache.rename("a", "b") / cache.renamenx("a", "b") / cache.touch("a")
cache.expire("k", 60) / cache.pexpire("k", 1000) / cache.expireat("k", ts)
cache.ttl("k")                    # 秒；-1 无过期 / -2 键不存在
cache.pttl("k") / cache.persist("k") / cache.expiretime("k")
cache.scan_iter(match="session:*", count=500)   # 推荐：增量、不阻塞
cache.keys("*")                   # 仅调试；键数 > 10000 直接拒绝
cache.clear("session:*")          # 按模式批量删（SCAN + UNLINK），返回删除数
```

> **`keys()` 有两道护栏**：db 内键数超过 `SAFE_KEYS_LIMIT`（10000）时直接
> 拒绝并提示改用 `scan_iter`；`clear()` 拒绝 `""` 与 `*` 这类整库模式——
> Redis 没有"库"的概念，一条 `FLUSHDB` 会连同别人的数据一起清掉。

### 5.5 流水线 / 事务

```python
with cache.pipeline() as pipe:          # transaction=True -> MULTI/EXEC
    pipe.set("a", "1").incrby("b", 3).hset("h", {"k": 1})
    results = pipe.execute()            # [True, 3, 1]（顺序与入队一致）

cache.pipeline(transaction=False)       # 不加 MULTI：一次往返、不保证原子
```

流水线**自动补键前缀**，但只覆盖**明确登记**的命令（`_PIPELINE_KEY_FIRST` 等
三张表）。未登记的命令直接拒绝并提示用 `pipe.raw()`：

```
InvalidArgumentException: 流水线未登记命令 'georadius'：为避免把键写到错误的前缀上，
这里不做猜测。请用 pipe.raw() 取原生 pipeline 自行处理键名
```

猜错前缀会静默写到错误的键上，比报错危险得多——所以这里选择"不猜"。

### 5.6 发布订阅

```python
sub = cache.subscribe("events")                  # 频道名同样补前缀
sub = cache.subscribe(patterns="evt:*")          # 模式订阅
try:
    receivers = cache.publish("events", "hello") # -> 收到消息的订阅者数
    for message in sub.listen():                 # {'type','channel','data'}
        ...
finally:
    sub.close()                                  # 必须关闭，否则占用连接

# 或用 messages() 只要三元组
for kind, channel, data in sub.messages():
    ...
```

### 5.7 元信息 / 逃生舱

```python
cache.ping() / cache.dbsize() / cache.info("memory") / cache.config_get("*")
cache.server_version() / cache.topology()
cache.client()              # 底层 redis.Redis（键名需自行处理前缀）
cache.close()
```

---

## 6. 异常归口

所有 `redis.exceptions.*` 在 `RedisStore._call` 这一**单一入口**翻译为项目异常，
调用方不需要 import 第三方库：

| 第三方异常 | 项目异常 | 典型场景 |
|---|---|---|
| `ConnectionError` / `TimeoutError` / `BusyLoadingError` / `MasterDownError` | `ConnectionFailedError` | 连不上、超时、主节点不可用 |
| `AuthenticationError` / `AuthenticationWrongNumberOfArgsError` | `ConnectionFailedError` | 密码错、ACL 用户错 |
| `DataError` | `InvalidArgumentException` | 客户端侧参数类型非法 |
| `ResponseError` / `ReadOnlyError` / `NoScriptError` / `OutOfMemoryError` / `WatchError` | `QueryError` | 服务端拒绝执行（WRONGTYPE、只读副本、OOM） |
| `ClusterDownError` / `ClusterCrossSlotError` | `QueryError` | 集群故障、跨 slot 多键 |

```python
from tinkpyorm import QueryError, ConnectionFailedError

try:
    cache.lpush("some-string-key", "x")     # 对 string 用 list 命令
except QueryError as exc:                    # WRONGTYPE 也归到这里
    ...
```

认证失败归 `ConnectionFailedError` 而不是 `QueryError`：对调用方而言它是
"连不上"（凭据错），重试策略与查询错误完全不同。

---

## 7. 性能实践

1. **批量操作用 `pipeline()`**：N 条命令一次往返。跨网络的收益是数量级的
   （单次 RTT 往往比命令本身贵一个数量级）。
2. **遍历键用 `scan_iter()`，不要用 `keys()`**：`KEYS` 是单线程阻塞命令，
   百万键会卡住整个实例；`SCAN` 是游标式增量遍历。
3. **给键设 TTL**：Redis 是内存库，无 TTL 的键会一直占内存直到淘汰策略介入。
   `set(..., ex=)` 或 `expire()`。
4. **大键要拆**：单个 hash/list 超过几万元素后，`HGETALL`/`LRANGE 0 -1` 会
   明显阻塞，改用 `hscan_iter` / 分页 `lrange`。
5. **连接池**：默认每客户端一个池；高并发服务设 `max_connections` 与
   `health_check_interval`，避免连接数打满服务端 `maxclients`。
6. **集群下用 hash tag**：`user:{1}:name`、`user:{1}:tags` 把相关键锁到同一
   slot，否则 `MGET`/`SUNION` 等会报 `CROSSSLOT`。
7. **写关注不是 Redis 概念**：需要"写成功才算成功"的强一致，请用
   `WAIT numreplicas timeout`（或用 `waitaof`，7.2+），而不是像 MongoDB 那样
   配 `write_concern`。

---

## 8. 与其它驱动的关系

| 能力 | SQLite / MySQL | MongoDB | Redis |
|---|---|---|---|
| `Db.table()` 链式查询 | ✅ | ❌ | ❌ |
| `Model` / 关联 / JOIN | ✅ | ❌ | ❌ |
| `Db.mongo()` 集合层 | ❌ | ✅ | ❌ |
| `Db.redis()` 键空间 | ❌ | ❌ | ✅ |
| 事务 | ✅（SQL） | ❌ | `pipeline(transaction=True)` |
| `Db.collection()` 文档集合 | ✅（仅 SQLite） | ❌ | ❌ |

`Db.redis()` 在非 Redis 连接下**构造期即抛** `UnsupportedOperation`；
`Db.mongo()` / `Db.table()` 在 Redis 连接下同样会被拒绝——三种访问层互斥，
不会出现"看着能用、跑起来语义错"的假支持。

---

## 9. 验证与已知限制

**已实测**（v0.13.0）：

- **114 项离线测试**（`test_redis.py`）：假 redis 客户端记录每次调用，
  覆盖版本解析/能力矩阵/配置优先级/参数校验/异常翻译/键前缀/流水线规则；
- **59 项真实服务端集成测试**（`test_redis_live.py`，需环境变量
  `TINKPYORM_REDIS_TEST_URL`，未设置则整份跳过）：
  实测服务端 **Redis 8.10.2**（standalone，16 db），覆盖五种数据类型、
  哈希字段级过期、流水线原子性、发布订阅、WRONGTYPE 与认证失败翻译；
- Python 3.8–3.15 全部通过。

**限制**：

1. **不支持 Cluster 客户端路由**：驱动使用单节点连接（`redis.Redis`），
   连集群需自行传 `redis.cluster.RedisCluster` 或前置代理；集群形态目前只做
   能力门禁（`select_db` / 多键同 slot）而不做代理转发。
2. **不支持 `Model` 映射**：Redis 没有表与字段概念，`Model` / 关联 / JOIN
   不适用，请用 `RedisStore`。
3. **不封装 RedisJSON / RediSearch / Bloom / TimeSeries / Vector Set**：
   这些是**模块**能力（服务端可能未加载），本层只登记 `vector_set` 的版本门槛，
   实际调用请走 `store.client()` 逃生舱。
4. **`KEYS` 有硬阈值**：db 键数 > 10000 时 `keys()` 拒绝执行（防误阻塞）。
5. **流水线只支持登记命令**：未登记的命令需 `pipe.raw()`（键名前缀自行处理）。
6. **`clear()` 不做跨 slot 保证**：集群下 `UNLINK` 多键需同 slot，按前缀批量
   删除可能报 `CROSSSLOT`；集群场景请逐个删除或使用 hash tag。
