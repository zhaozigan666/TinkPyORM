# Redis 数据访问层设计说明（v0.13.0）

本文记录 Redis 支持的**设计取舍**，面向维护者；使用手册见
[`wiki/31-Redis支持.md`](../wiki/31-Redis支持.md)。

---

## 1. 分层：为什么只有两层

项目已有的两种访问层：

| 层 | 形态 | 依据 |
|---|---|---|
| `Query` / `Builder` / `Model` | SQL 关系型 | 表、列、JOIN、事务 |
| `MongoCollection` | 文档集合 | 集合、文档、过滤文档、索引 |

Redis 既没有表也没有文档集合，它的数据模型是**键 → 值（五种类型）**。
硬套 `Query` 会得到一个只有 `where` 语义却无法表达 `LPUSH` 的假构造器，
所以这里只做两层：

```
drivers/redis.py   RedisDriver   连接 / 配置解析 / 版本与拓扑门禁 / 异常翻译
redis.py           RedisStore    键空间访问 / 输入校验 / 命名空间 / 流水线 / 订阅
```

这与 `docs/config-and-driver-refactor.md` §6.2 对 NoSQL 的结论一致
（MongoDB 不套 Query，Redis 同理）。

---

## 2. 命名空间 = 键前缀

Redis 是**单一扁平键空间**（16 个 db 只是彼此隔离的平面空间）。要在应用里
划出"我的数据"，最自然的抽象是前缀：

```python
cache = Db.redis("app:")
cache.set("user:1", "张三")     # 实际 app:user:1
```

**为什么叫 `name` 而不是 `prefix`**：`Db.redis("user:")` 读起来与
`Db.mongo("users")` / `Db.table("user")` 同构——第一个参数都是"我要操作哪一块
数据"。参数名保持 `name`，语义上用前缀实现。

**返回逻辑键名**：`scan_iter()` / `keys()` 返回去掉前缀的名字。否则业务代码要
在每个遍历处 `removeprefix`，而前缀本就是这层的实现细节。

**与 `Db._prefix` 的关系**：连接配置里的 `prefix`（SQL 侧的表前缀）会与方法参数
拼接，便于 SQL 与 Redis 共用同一命名空间约定。

---

## 3. 序列化策略：显式 JSON，不做隐式编码

反例（很多 Redis 封装会这样做）：

```python
cache.set("k", {"a": 1})     # 自动 json.dumps
cache.get("k")               # -> {"a": 1}？还是 '{"a": 1}'？
```

问题在于 `get()` 的**返回类型取决于写入路径**：同一次 `get`，可能是字符串、
可能是 dict，读代码的人无法只凭签名判断。再叠加"用户本来就存了一段 JSON 文本"
的情况，就无法区分"对象"与"字符串"。

本层的选择：

- `set()` / `hset()` / `lpush()` 只接受**标量**（str/bytes/int/float/bool）；
- 容器一律拒绝，错误信息直接给替代写法（`set_json`）；
- `set_json()` / `get_json()` 是显式 API，`get_json` 在键不存在时**默认抛错**
  （`default=UNSET` 哨兵），要"缺省返回 None"必须显式写 `default=None`——
  避免把"没有这个键"和"值就是 null"混为一谈。

代价是多一个方法名；收益是**任何一行 `get()` 的返回类型都是确定的**。

---

## 4. 版本模型：与 MongoDB 一致，而不是与 MySQL 一致

| | MySQL | MongoDB | Redis |
|---|---|---|---|
| 版本能否廉价探测 | ❌ | ✅ `hello` | ✅ `INFO server` |
| 门禁依据 | **必须显式声明** | 探测值 | 探测值 |
| 声明值的角色 | 唯一依据 | 断言（高了报错） | 断言（高了报错） |

Redis 的 `INFO` 一次往返就能拿到精确版本（实测 `8.10.2`），没有理由要求用户
手写版本——写错反而让门禁失真。因此：

- 探测成功 → 以探测值为准；
- `INFO` 被 ACL 拒绝 → 退化为声明值 > 缺省值（**不把权限问题误报成连接问题**）；
- 声明值 > 探测值 → 连接期直接报错，这是"配置与现实不符"，必须显式暴露。

### 4.1 发布线而非补丁号

Redis 的 6.2 / 7.2 / 7.4 是**独立发布线**，不是 6.0 的补丁。所以校验按
`(major, minor)` 精确匹配，只对 8.x 放开所有小版本（8.0 起进入快速发布节奏，
8.2/8.4/8.10 都是正式版）。这与 MySQL 的"5.7 / 8.0–8.4 / 9.x"、MongoDB 的
"4.0/4.2/4.4/5.0/6.0/7.0/8.0" 是同一套思路：**放行真实发布线，拒绝不存在或
未成型的版本**。

---

## 5. 版本 + 拓扑双门禁

与 MongoDB 事务必须有副本集同理，Redis 的若干能力依赖部署形态：

| 能力 | 约束 | 为什么不能只看版本 |
|---|---|---|
| `select_db` | 集群只有 db 0 | `SELECT` 在集群模式不存在 |
| `multi_key_unrestricted` | 集群要求同 slot | 跨 slot 的 `MGET`/`SUNION` 直接报错 |

实现上用 `REDIS_TOPOLOGY_FEATURES` 表 + `driver.topology`（由 `INFO` 的
`cluster_enabled` / `redis_mode` 判定）。拓扑未知时**不拦截**——宁可少拦一次，
也不要在 ACL 受限的环境里把可用能力误判为不可用。

---

## 6. 流水线：登记制而不是"猜参数语义"

`RedisPipeline.__getattr__` 把任意方法名转发给底层 pipeline，但**键前缀**必须
知道哪些位置参数是键：

- `set(k, v)` → 第 1 个参数
- `delete(k1, k2)` → 全部参数
- `rename(src, dst)` → 前 2 个参数
- `hset(k, mapping=...)` / `zadd(k, mapping)` → 容器型，显式覆写

三张登记表覆盖了本层暴露的全部命令。**未登记的命令直接拒绝**：

```
InvalidArgumentException: 流水线未登记命令 'x'：为避免把键写到错误的前缀上，
这里不做猜测。请用 pipe.raw() 取原生 pipeline 自行处理键名
```

替代方案是"第一个参数一律视为键"的通用规则——但 `EVAL`、`SCAN`、`OBJECT`、
`XREAD` 等命令会让这条规则静默写错键。**静默写错比报错危险得多**，所以选择
保守策略：新命令要进流水线，先在表里登记一行。

---

## 7. 安全护栏

Redis 的危险命令破坏力极大（`FLUSHDB` 一条命令清空整库），因此：

| 护栏 | 行为 | 理由 |
|---|---|---|
| `keys()` | db 键数 > 10000 时拒绝 | `KEYS` 是 O(N) 阻塞命令，线上会卡住所有客户端 |
| `clear()` | 拒绝 `""` / `"*"` 模式 | 没有"库"的概念，整库删除会误伤同实例其它应用的数据 |
| `set()` 容器值 | 拒绝并提示 `set_json` | 见 §3 |
| `nx` + `xx` | 同时为真直接报错 | Redis 语义上互斥，静默取一个会让调用方误判 |
| 未知连接选项 | 静默忽略（不透传） | 避免拼错选项名变成 `TypeError` |
| `write_concern` | 不提供 | Redis 没有这个概念；强一致用 `WAIT` / `WAITAOF` |

---

## 8. 异常归口：单一入口

所有 redis-py 调用都经 `RedisStore._call`：

```python
def _call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except OrmError:
        raise
    except Exception as exc:
        raise translate_redis_error(exc) from exc
```

包括两个容易被漏掉的位置：

1. **`connect()` 里的 `PING`**：认证失败、网络超时都会从这里抛出。最初只包了
   命令执行，结果 `AuthenticationError` 直接从 `connect()` 泄漏到调用方——
   真实服务端测试（`test_authentication_error`）抓到了这个问题。
2. **流水线的 `execute()`**：`MULTI/EXEC` 的错误在 `execute` 时才返回，
   `__getattr__` 那层完全看不到。

翻译用**类名匹配**而不是 `isinstance`：这样离线测试不需要安装 redis-py 也能
覆盖全部映射（与 MongoDB 侧同一做法）。

---

## 9. 真实服务端验证

在 `s10.z100.vip:31568`（Redis **8.10.2**，standalone，16 db，已加载
bf / vectorset / timeseries / search / ReJSON 模块）上跑 59 项集成测试。

暴露并修复的问题：

| # | 问题 | 离线测试为何测不出 |
|---|---|---|
| 1 | `connect()` 的 `PING` 异常未翻译，`AuthenticationError` 直接泄漏 | 假客户端 `ping` 永远返回 True |
| 2 | `zrangebyscore` 只传 `start` 不传 `num` 触发 `DataError` | 假客户端不校验参数组合 |
| 3 | `sintercard` 的 redis-py 签名是 `(numkeys, keys, limit)`，摊平键列表会被当成 `limit` | 假客户端只记录调用，不校验签名 |
| 4 | `subscribe(patterns="evt:*")` 把字符串逐字符拆开 | 假 pubsub 不校验模式语义 |

**教训（与 MongoDB 侧一致）**：假客户端只能验证"我构造了什么"，验证不了
"第三方客户端怎么解释我的参数"。凡是"传给库函数的位置参数/关键字组合"，
都必须在真实依赖上跑一遍。

---

## 10. 未做的取舍

| 未做 | 原因 | 何时再考虑 |
|---|---|---|
| Cluster 客户端路由 | 需 `RedisCluster` 另一套连接模型，且多键/事务语义差异大 | 有真实集群需求时单独做 |
| RedisJSON / RediSearch 封装 | 属于模块能力，服务端可能未加载；语义与键值层差异大 | 明确只用 Redis 8+ 时 |
| 分布式锁 `lock()` | `SET NX EX` 已可表达；完整实现要处理续期、误删、watchdog | 有多实例协调需求时 |
| 隐式 JSON 序列化 | 见 §3：返回类型不确定 | 不做 |
| 键命名空间"自动过期/租户隔离" | 前缀只是命名约定，不是权限边界 | 需要时用 ACL key pattern |
| `write_concern` 模拟 | Redis 无此概念，假装支持会误导 | 不做，文档指向 `WAIT` |
