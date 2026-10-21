# 30-MongoDB支持

MongoDB 数据访问层（v0.12.0）。核心包仍是**零依赖**，MongoDB 支持是可选依赖
（`pip install tinkpyorm[mongo]`，即 `pymongo`）。

---

## 一、定位与分层

MongoDB 是文档数据库，没有表、没有 JOIN、没有通用 SQL 方言。本项目
**不把 MongoDB 塞进 `Query` / `Builder` / `Model` 抽象**（那样只会产生大量
"能构造但语义错误"的假支持），而是分两层：

| 层 | 位置 | 职责 |
|---|---|---|
| 驱动层 | `tinkpyorm/drivers/mongodb.py` | 连接建立 / 生命周期、版本探测与能力门禁、异常翻译、配置解析 |
| 服务层 | `tinkpyorm/mongo.py`（`MongoCollection`） | 集合级增删改查、输入校验、索引与聚合封装 |

与项目内既有的 `DocumentCollection`（建在 SQLite 上的 schemaless 存储）并列，
二者互不干扰：`Db.collection()` 走 SQLite 文档存储，`Db.mongo()` 走真实
MongoDB 服务端。

**操作符词汇沿用 think-orm 链式风格**，不用 MongoDB 的 `$` 字典风格
（与 `DocumentCollection` 的既定决策一致）；需要原生语法时用
`where_raw()` / `update_raw()` / `aggregate()` 三个逃生舱。

---

## 二、连接配置

### 配置方式

```python
from tinkpyorm import Db

# 方式一：连接串
Db.config({"type": "mongodb", "database": "app",
           "options": {"uri": "mongodb://user:pwd@127.0.0.1:27017/?authSource=admin"}})

# 方式二：分散参数（连接串由驱动拼装）
Db.config(type="mongodb", database="app", host="127.0.0.1", port=27017,
          user="app", password="secret", options={"auth_source": "admin"})
```

### 配置来源优先级

**显式配置 > 配置中心 > 环境变量 > 默认值**

| 来源 | 说明 | 键名 |
|---|---|---|
| 显式配置 | `Config` 字段或 `options` 中的同名键 | `host` `port` `user` `password` `database` `auth_source` `uri` |
| 配置中心 | `options["config_resolver"]`，可调用对象，签名为 `resolver() -> dict` | 同上 + `options` |
| 环境变量 | 首个非空者胜出 | `MONGODB_URI`/`MONGO_URI`、`MONGODB_DATABASE`/`MONGO_DB`、`MONGODB_USER`、`MONGODB_PASSWORD`、`MONGODB_AUTH_SOURCE`、`MONGODB_HOST`、`MONGODB_PORT` |
| 默认值 | `host=127.0.0.1`、`port=27017` | — |

配置中心回调在**每次连接时**被调用，因此可接入 Nacos / Apollo / etcd 等
动态配置源而无需重启进程：

```python
Db.config({
    "type": "mongodb",
    "options": {
        "config_resolver": lambda: {
            "uri": os.environ["MONGO_URI_FROM_CENTER"],
            "database": "app",
        },
    },
})
```

回调返回值必须是 `dict`；返回非 dict 或回调本身不可调用都会抛
`InvalidArgumentException`（配置中心的故障不应被静默吞掉）。

### 配置缺失时的报错

| 缺失项 | 报错 |
|---|---|
| 数据库名 | `MongoDB 缺少数据库名：请设置 config 的 database（或在 options['uri'] 中带上库名），或提供环境变量 MONGODB_DATABASE` |
| 密码但无用户名 | `MongoDB 提供了 password 但缺少 user，请补齐用户名` |

主机与端口有本地默认值（同 MySQL 驱动的 `127.0.0.1:3306`）；**数据库名没有
默认值**——MongoDB 的默认库是 `test`，几乎总是误配。

### 连接选项

`options` 中以下键会转成 pymongo 的 `MongoClient` 参数（内部统一 snake_case，
对外 camelCase）：

| 项目键 | pymongo 参数 | 默认 | 说明 |
|---|---|---|---|
| `server_selection_timeout_ms` | `serverSelectionTimeoutMS` | pymongo 默认 | **服务选择超时**：等不到合适节点即失败，生产环境应显式设置 |
| `connect_timeout_ms` | `connectTimeoutMS` | pymongo 默认 | 建立 TCP 连接超时 |
| `socket_timeout_ms` | `socketTimeoutMS` | 无 | 套接字读写超时；无操作时的长连接建议设为较大值 |
| `max_pool_size` | `maxPoolSize` | 100 | 连接池上限 |
| `min_pool_size` | `minPoolSize` | 0 | 连接池下限 |
| `max_idle_time_ms` | `maxIdleTimeMS` | 无 | 空闲连接回收 |
| `wait_queue_timeout_ms` | `waitQueueTimeoutMS` | 无 | 等待池中连接的排队超时 |
| `retry_writes` | `retryWrites` | True | 可重试写（**需副本集**，单节点会报错） |
| `retry_reads` | `retryReads` | True | 可重试读 |
| `auth_mechanism` | `authMechanism` | pymongo 默认 | 认证机制 |
| `appname` | `appname` | 无 | 连接标识，便于在服务端 `currentOp` 里区分来源 |
| `direct_connection` | `directConnection` | False | 直连单节点（不发现副本集） |
| `replica_set` | `replicaSet` | 无 | 副本集名 |
| `tls` | `tls` | False | TLS 开关 |

`config.connect_timeout`（秒）会同时兜底 `serverSelectionTimeoutMS` 与
`connectTimeoutMS`。

### 写关注（write concern）

```python
Db.config({"type": "mongodb", "database": "app",
           "options": {"uri": "...", "write_concern": {"w": "majority", "j": True}}})
```

| 键 | pymongo 参数 | 取值 | 取舍 |
|---|---|---|---|
| `w` | `w` | `0` / `1` / 副本集名 / `"majority"` | `1`（默认）= 主节点确认，快；`"majority"` = 多数节点确认，慢但抗节点故障 |
| `j` | `j` | `True` / `False` | `True` = 落盘 journaling，慢但掉电不丢 |
| `wtimeout` | `wtimeoutMS` | 毫秒 | 等待确认的超时 |

**建议**：普通读多写少用 `w=1`；资金/订单类关键写入用 `w="majority", j=True`。
写入关注级别越高，单次写延迟越高——这是**耐久性换延迟**的显式取舍。

也可按集合覆盖：

```python
users = Db.mongo("users", write_concern={"w": "majority"})
```

---

## 三、版本与能力门禁

MongoDB 的服务端版本可以被**廉价探测**（`hello` 命令返回 `maxWireVersion`，
无需额外权限），因此与 MySQL 侧不同：**能力门禁以实际探测到的版本为准**，
而不是以配置上的声明为准。

```python
driver = Db.get_connection().driver
driver.detect_version()      # -> Version(8, 3, 13)（主动探测，精确）
driver.version               # -> 探测值 > 声明值 > 默认 4.0.0
driver.topology              # -> 'replica_set' / 'sharded' / 'standalone'
driver.supports_feature("transactions")   # bool（版本 + 拓扑双门禁）
driver.require_feature("transactions")    # 能力不足时抛 UnsupportedOperation
```

**探测分两条路径，精确优先**：

| 路径 | 返回 | 权限要求 |
|---|---|---|
| `buildInfo`（`server_info()`） | **精确版本**（如 `8.3.13`） | 需要认证/权限 |
| `hello` 的 `maxWireVersion` | **版本线下界**（8.x 一律 `8.0.0`） | 无需权限 |

有权限时以 `buildInfo` 为准；无权限时退化为 `maxWireVersion` 下界（对能力
门禁是保守的，不会误放行）。

### 版本之外还有拓扑门禁

**版本够不代表能用**。多文档事务要求副本集或分片集群，单节点（standalone）
部署即使跑 8.x 也不支持。因此能力判定是**版本 + 拓扑**双门禁：

```python
# 实测：MongoDB 8.3.13 + standalone
driver.supports_feature("transactions")     # False（不是版本问题，是拓扑问题）
driver.require_feature("transactions")
# UnsupportedOperation: transactions 需要 replica_set / sharded 部署，
#                       当前是 standalone（单节点不支持该能力）
```

受拓扑限制的能力见 `MONGO_TOPOLOGY_FEATURES`：`transactions`（副本集/分片）、
`sharded_change_stream` / `sharded_lookup`（仅分片）。
拓扑未知时不拦截（避免误杀），只按版本判断。

显式声明的 `version` 退化为一条**断言**——声明版本**高于**实际服务端时，
连接阶段立即报错：

```
InvalidArgumentException: 声明的 MongoDB 版本 8.0.0 高于实际服务端版本 5.0.0；
请修正 Db.config(version=...)
```

声明版本**低于**实际服务端是允许的（不报错），此时仍以探测值为准。

### 支持范围

`4.0.x / 4.2.x / 4.4.x / 5.0.x / 6.0.x / 7.0.x / 8.x`（缺省按 `4.0.0` 处理）。
4.x 只放行偶数发布线（4.0 / 4.2 / 4.4）——奇数小版本是开发版；8.x 含快速发布线（8.1+）。

### 能力矩阵

| 能力 | 最低版本 | 4.0 | 4.2 | 4.4 | 5.0+ | 6.0+ | 7.0+ | 8.0+ |
|---|---|---|---|---|---|---|---|---|
| `change_stream` | 3.6 | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| `retryable_writes` | 3.6 | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| `transactions`（多文档） | 4.0 | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| `update_pipeline`（聚合管道更新） | 4.2 | ❌ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| `merge_stage`（`$merge`） | 4.2 | ❌ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| `wildcard_index` | 4.2 | ❌ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| `union_with`（`$unionWith`） | 4.4 | ❌ | ❌ | ✅ | ✅ | ✅ | ✅ | ✅ |
| `function_accumulator` | 4.4 | ❌ | ❌ | ✅ | ✅ | ✅ | ✅ | ✅ |
| `hidden_index` | 4.4 | ❌ | ❌ | ✅ | ✅ | ✅ | ✅ | ✅ |
| `set_window_fields` | 5.0 | ❌ | ❌ | ❌ | ✅ | ✅ | ✅ | ✅ |
| `time_series` | 5.0 | ❌ | ❌ | ❌ | ✅ | ✅ | ✅ | ✅ |
| `densify_stage` | 5.1 | ❌ | ❌ | ❌ | ✅ | ✅ | ✅ | ✅ |
| `top_n_accumulators` | 5.2 | ❌ | ❌ | ❌ | ✅ | ✅ | ✅ | ✅ |
| `fill_stage` | 5.3 | ❌ | ❌ | ❌ | ✅ | ✅ | ✅ | ✅ |
| `documents_stage` | 6.0 | ❌ | ❌ | ❌ | ❌ | ✅ | ✅ | ✅ |
| `search_meta` | 7.0 | ❌ | ❌ | ❌ | ❌ | ❌ | ✅ | ✅ |
| `rank_fusion` | 8.0 | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ✅ |

### 已被移除的能力

| 能力 | 移除版本 | 说明 |
|---|---|---|
| `mongodb_cr_auth` | 4.0 | MONGODB-CR 认证机制移除 |
| `geo_haystack` | 4.4 | geoHaystack 索引与 `$geoSearch` 移除 |
| `op_query` | 5.1 | OP_QUERY 线协议移除（仅保留 OP_MSG） |

---

## 四、增删改查

```python
users = Db.mongo("users")

# 创建单条（返回 _id）
uid = users.insert({"name": "张三", "age": 25, "tags": ["vip"]})

# 批量写入（单次网络往返）
users.insert_many([{"name": "李四", "age": 30}, {"name": "王五", "age": 22}])

# 查询列表（分页 + 排序 + 字段筛选 + 条件）
users.where("age", ">=", 18).order("age", "desc").limit(10).select()
users.page(2, 20).select()                       # 偏移量分页
users.field("name", "age").select()              # 只返回指定字段
users.exclude("password").select()               # 排除指定字段

# 按 ID 查询
users.find_by_id(uid)
users.find_by_id("507f1f77bcf86cd799439011")     # 24 位十六进制自动转 ObjectId

# 单个更新 / 批量更新
users.where("_id", uid).update({"age": 26})      # $set
users.where("age", "<", 18).update_many({"vip": False})
users.where("_id", uid).update_raw({"$inc": {"balance": 100}})   # 原生算子
users.where("_id", uid).replace({"name": "赵六"})                # 整文档替换

# 删除
users.delete(uid)                                # 按 _id 删一条
users.where("age", "<", 18).delete()             # 按条件删一条
users.where("age", "<", 18).delete_many()        # 按条件删多条

# 其它
users.count()                                    # 匹配条数
users.distinct("city")                           # 字段去重
users.aggregate([{"$match": {"age": {"$gte": 18}}}, {"$group": {"_id": "$city", "n": {"$sum": 1}}}}])
```

链式方法（`where` / `order` / `limit` / `page` / `field`）累积状态，**终端方法**
（`select` / `insert` / `update` / `delete` / `count` / `aggregate`）执行后复位，
同一实例可连续发起多个独立操作。

### 操作符映射

| think-orm 运算符 | MongoDB 表达式 | 备注 |
|---|---|---|
| `=` | `{field: value}` | |
| `!=` / `<>` | `{field: {"$ne": value}}` | |
| `>` `>=` `<` `<=` | `$gt` / `$gte` / `$lt` / `$lte` | |
| `in` / `not in` | `$in` / `$nin` | 值必须是列表 |
| `between` | `{"$gte": lo, "$lte": hi}` | 值必须是 `[lo, hi]` |
| `not between` | `{"$not": {"$gte":.., "$lte":..}}` | |
| `like` / `not like` | 正则 | `%`→`.*`、`_`→`.`，大小写不敏感，正则元字符已转义 |
| `null` / `not null` | `{field: None}` / `{"$ne": None}` | MongoDB 的 `null` 同时匹配"字段缺失" |
| `exists` / `not exists` | `$exists: True` / `False` | |
| `contains` | `{field: value}` | 数组含元素 |

同一字段的多个条件会**合并进同一个算子字典**（`{"$gte":18,"$lte":30}`），
以便命中复合索引；无法合并时退回 `$and`。

### 原生语法逃生舱

```python
users.where_raw({"$expr": {"$gt": ["$balance", 100]}})   # 原生过滤文档
users.update_raw({"$unset": {"tmp": ""}})                 # 原生更新算子
users.aggregate([...])                                    # 聚合管道
```

---

## 五、输入校验

| 校验项 | 规则 | 异常 |
|---|---|---|
| 集合名 | 字母/数字/下划线，不以数字开头 | `InvalidArgumentException` |
| 字段名 | 非空、不以 `$` 开头、不含空字符 | `InvalidArgumentException` |
| 文档类型 | 必须是 `dict` | `InvalidArgumentException` |
| 文档字段 | 键不得以 `$` 开头（MongoDB 保留前缀） | `InvalidArgumentException` |
| 必填字段 | `schema(..., required=[...])` 声明后必须出现 | `InvalidArgumentException` |
| 字段类型 | `schema({"age": int})` 声明后校验；float 宽容接受 int；bool 与 int/float 不兼容 | `InvalidArgumentException` |
| 运算符 | 必须在 `MONGO_OPS` 词汇内 | `QueryError` |
| `in` / `between` 的值 | 必须是列表 / 二元列表 | `QueryError` |
| 排序方向 | `asc` / `desc` / `1` / `-1` | `InvalidArgumentException` |
| 分页参数 | `page >= 1`；`1 <= list_rows <= 1000` | `InvalidArgumentException` |
| `limit` / `offset` | `limit >= 1`；`offset >= 0` | `InvalidArgumentException` |
| 索引字段描述 | 字段名 / `(字段, 方向)` / 列表 | `InvalidArgumentException` |
| 聚合管道 | 非空 `list[dict]`，每阶段为非空 dict | `InvalidArgumentException` |

Schema 校验示例：

```python
users.schema({"name": str, "age": int, "tags": list}, required=["name"])
users.insert({"age": 20})        # 报错：缺少必填字段: 'name'
users.insert({"name": "x", "age": "二十"})   # 报错：字段 'age' 的值类型不符
```

未声明的字段放行（贴合 schemaless 精神）。

---

## 六、异常归口

所有 `pymongo` / `bson` 异常在驱动层翻译成项目既有异常体系，调用方
**无需 import 第三方库**即可捕获：

| 第三方异常 | 项目异常 |
|---|---|
| `DuplicateKeyError` | `DuplicateKeyError`（新增） |
| `InvalidId` / `InvalidDocument` / `InvalidOperation` / `ConfigurationError` | `InvalidArgumentException` |
| `ServerSelectionTimeoutError` / `ConnectionFailure` / `NetworkTimeout` / `AutoReconnect` | `ConnectionFailedError`（新增） |
| `OperationFailure`（**认证类** 13/18） | `ConnectionFailedError` |
| `OperationFailure` / `WriteError` / `WriteConcernError` / `BulkWriteError` / `ExecutionTimeout` | `QueryError` |
| 其它未知异常 | `QueryError` |

全部是 `OrmError` 的子类，因此 `except OrmError` 可一次性兜住。

> 认证失败（`requires authentication`）归入 `ConnectionFailedError` 而非
> `QueryError`——它是连接层问题，调用方通常需要重连或告警，而不是重试查询。

---

## 七、性能实践

### 1. 建索引，避免全表扫描

无索引的 `where` 会退化成 **COLLSCAN**（全表扫描），集合越大越慢。
**上线前先为高频查询字段建索引**：

```python
users.ensure_index("email", unique=True)               # 单字段唯一索引
users.ensure_index([("city", 1), ("age", -1)])          # 复合索引
users.ensure_index(("email", "desc"))                   # 降序
```

复合索引的**字段顺序很重要**：等值过滤字段在前、范围/排序字段在后
（如 `{"city": 1, "age": -1}` 适合 `where("city", x).order("age", "desc")`）。

自查是否命中索引：

```python
plan = users.where("age", ">=", 18).explain()
"IXSCAN" in str(plan)     # 命中索引
"COLLSCAN" in str(plan)   # 全表扫描 —— 该建索引了
```

### 2. 投影与结果数量限制

只取需要的字段，避免拉回大文档（尤其是含长文本/数组的文档）：

```python
users.field("name", "age").select()
users.exclude("internal_notes").select()
```

**始终显式 `limit()`**。未指定 `limit` 时本层会套用默认上限
`1000`（可用 `options["max_limit"]` 调整），防止一次查询拖垮内存。

### 3. 避免 N+1 查询

典型 N+1：先查出一批用户，再对每个用户单独查一次订单。

```python
# 差：N+1 次往返
for u in users.select():
    orders = Db.mongo("orders").where("user_id", u["_id"]).select()

# 好 1：$lookup 在服务端一次 join 完成
users.aggregate([
    {"$lookup": {"from": "orders", "localField": "_id",
                 "foreignField": "user_id", "as": "orders"}},
])

# 好 2：批量取回后在内存里关联（两次往返）
by_user = {o["user_id"]: o for o in Db.mongo("orders")
           .where_in("user_id", [u["_id"] for u in users.select()]).select()}
```

### 4. 批量写入

```python
users.insert_many([...])              # 单次网络往返，unordered（遇错继续）
users.insert_many_ordered([...])       # 单次网络往返，遇错即停
users.bulk_write([InsertOne(...), UpdateOne(...), DeleteOne(...)])   # 混合操作
```

**不要循环单条 `insert()`**——每条一次往返，量大时差异是数量级的。

大批量导入的顺序建议：**先灌数据，再建索引**（建索引期间数据需重排，
边灌边建会持续触发重排，比最后建一次慢得多）。

### 5. 分页方案取舍

| 方案 | API | 代价 | 适用 |
|---|---|---|---|
| 偏移量 | `page(n, rows)` | 底层 `skip`，**代价随页码线性增长**（`skip 100000` 仍要扫描前 10 万条）；并发写入时可能出现重复/遗漏 | 数据量小、页数浅、可随机跳页 |
| 游标（keyset） | `after(last_id)` | 代价与页码无关（走 `_id` 索引），稳定不重不漏 | 深翻页、无限滚动、大数据量 |

```python
# 游标分页：取最后一条的 _id 作为下一页起点
last = None
while True:
    page = users.order("_id").limit(50)
    if last:
        page = users.after(last)
    rows = page.select()
    if not rows:
        break
    last = rows[-1]["_id"]
```

游标分页要求排序字段**单调递增**（`_id` 默认满足）。

### 6. 实测数据（MongoDB 8.3.13，单节点，跨网络 ~75ms RTT，5 万条文档）

以下数字来自真实服务端（`executionStats` 的服务端统计，非客户端计时推算）：

**索引效果**（`city + age` 复合索引，查 `city=北京` 并按 `age` 排序）

| 状态 | 执行计划 | docsExamined | keysExamined | 服务端耗时 |
|---|---|---|---|---|
| 建索引前 | `COLLSCAN` | 50,000（全表） | 0 | 35 ms |
| 建索引后 | `IXSCAN` | 6,192（仅命中） | 6,192 | 14 ms |

结论：**扫描量与"命中数"挂钩，而不是"集合大小"**——这是索引唯一真正的价值。

**批量写入**

| 场景 | 耗时 | 吞吐 |
|---|---|---|
| `insert_many` 5 万条（每批 5000，`w=1`） | 16.1 s | ~3,100 条/秒 |

**深分页**（5 万条，取第 2000 页 / skip 39,980）

| 方案 | keysExamined | 特点 |
|---|---|---|
| `page(2000, 20)`（偏移量） | 随页码线性增长 | 页码越深越慢 |
| `after(last_id)`（游标） | 20（恒定） | 与页码无关 |

客户端计时两者分别为 95 ms / 74 ms——**该服务端 RTT ~75 ms，几乎全是网络
往返**，把服务端的差异掩盖了。这也是为什么上述结论必须看 `executionStats`
而不是看客户端耗时。

**写关注**（`w=1` vs `w=majority`，单节点）

单节点部署下 `majority` 恒等于 `w=1`（多数派就是自己），实测 1.04×——差异
在噪声范围内。**只有副本集/分片集群才需要为 `majority` 付真实代价**；单节点
上加 `majority` 只是心理安慰。

---

## 八、已知限制

1. **真实服务端集成测试需自备凭据**：`test_mongo_live.py`（32 项）跑真实
   pymongo + 真实服务端，覆盖 CRUD、异常归口、写关注、索引命中、游标分页、
   `$lookup`。凭据**不入库**，只从环境变量读：

   ```bash
   TINKPYORM_MONGO_TEST_URI='mongodb://user:pwd@host:27017/?authSource=admin'        python test_mongo_live.py
   ```

   未设置该变量（或未装 pymongo / 服务端不可达）时**整份自动跳过**，可安全
   并入常规流程。已在 MongoDB 8.3.13（单节点）上以 5 万条数据实测通过。
2. **不支持多文档事务**：MongoDB 事务（4.0+，且需副本集/分片集群）需要显式
   session，与本项目既有的 `Db.transaction()`（SQL 语义）不兼容，故
   `supports_transactions = False`；需要事务请直接用 pymongo 的 session。
   `driver.supports_feature("transactions")` 会同时检查**版本与拓扑**，
   单节点部署返回 `False`。
3. **不支持 ORM 模型层**：`Model` / 关联 / JOIN 均为关系型概念，MongoDB 侧
   不可用；文档模型请用 `MongoCollection` + `schema()`。
4. **不提供 `Db.collection()` 的 MongoDB 版本**：`Db.collection()` 始终是
   SQLite 文档存储；MongoDB 侧请用 `Db.mongo()`。
5. **`upsert` 语义不统一**：关系型的 `Query.upsert()` 是 SQL 语法，
   MongoDB 侧通过 `update(..., upsert=True)` 提供，故驱动
   `supports_upsert = False`。
6. **全文检索语义不同**：MongoDB 用 text / wildcard 索引，与 SQLite FTS5
   完全不同，故 `supports_fts = False`，`Db.fts()` 在 MongoDB 配置下不可用。

---

## 九、相关页面

- [01-连接配置](01-连接配置.md) —— 关系型连接配置
- [26-Collection文档集合](26-Collection文档集合.md) —— SQLite 上的 schemaless 文档存储
- [29-MySQL支持](29-MySQL支持.md) —— MySQL 版本矩阵与门禁（对照阅读）
- [24-注意事项与已知限制](24-注意事项与已知限制.md)
