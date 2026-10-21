# MongoDB 数据访问层设计

> 对应实现：`tinkpyorm/drivers/mongodb.py` + `tinkpyorm/mongo.py`（v0.12.0）。
> 本文记录**为什么这样设计**；用法见 [wiki/30-MongoDB支持](../wiki/30-MongoDB支持.md)。

## 1. 目标与边界

需求：为一个已有 SQLite ORM 项目补充 MongoDB 数据访问能力，覆盖连接配置、
集合增删改查、输入校验、异常归口与性能实践，且**保持与既有代码结构、
命名、导出与返回结构一致**。

项目自身的既有约束（见 `docs/config-and-driver-refactor.md` §6.2）明确写着：
MongoDB / Redis **不应**套用 `Query` / `Builder` / `Model` 抽象。这条约束
仍然成立，本设计遵守它。

## 2. 分层：为什么是「驱动层 + 集合层」而不是复用 Query

| 备选 | 结论 | 理由 |
|---|---|---|
| 把 MongoDB 塞进 `Builder`，生成伪 SQL | 否 | MongoDB 没有表、没有 JOIN、没有通用 SQL 方言；强行翻译会产生"能构造但语义错误"的假支持（JOIN 怎么办？`LIKE` 怎么办？） |
| 让 `MongoCollection` 继承 `Query` | 否 | `Query` 的核心是"累积 options → 编译 SQL → 参数绑定"；MongoDB 的查询是**文档**而非 SQL，继承只会带来一堆空实现 |
| 独立两层：驱动层管连接与方言，集合层管数据访问 | **是** | 与项目既有的「驱动层 / 服务层」切分一致；`DocumentCollection`（SQLite 上的文档存储）也是同样的两层结构 |

**驱动层**（`drivers/mongodb.py`）只做四件事，职责边界明确：

1. 连接建立与生命周期（`MongoClient` 懒连接、ping、close）；
2. 配置解析（来源优先级、连接串拼装、缺失项报错）；
3. 版本探测与能力门禁（`MONGO_FEATURES` / `MONGO_REMOVED`）；
4. 异常翻译（`pymongo` / `bson` → 项目异常体系）。

**集合层**（`mongo.py` 的 `MongoCollection`）负责数据访问语义：条件构造、
排序投影分页、增删改查、索引、schema 校验。

## 3. 操作符：为什么不用 `$` 字典风格

`docs/collection.md` 记录过一个明确决策：文档集合的操作符**不用 MongoDB 的
`$` 字典风格**，沿用库内既有的 think-orm 链式词汇。理由是**库内一致性**：
同一个 `where("age", ">=", 18)` 在 SQLite 表、SQLite 文档集合、MongoDB 集合
上写法相同，切换存储后端时业务代码的运算符不变。

因此 `MongoCollection.where()` 沿用同一套词汇，由驱动层翻译成 MongoDB 算子：

| think-orm 运算符 | MongoDB 表达式 |
|---|---|
| `=` / `!=` | `{field: value}` / `{"$ne": value}` |
| `>` `>=` `<` `<=` | `$gt` / `$gte` / `$lt` / `$lte` |
| `in` / `not in` | `$in` / `$nin` |
| `between` | `{"$gte": lo, "$lte": hi}` |
| `like` / `not like` | 正则（`%`→`.*`、`_`→`.`，大小写不敏感） |
| `null` / `not null` | `{field: None}` / `{"$ne": None}` |
| `exists` / `not exists` | `$exists: True` / `False` |

同时提供三个**逃生舱**以覆盖原生语法需求：`where_raw()`（原生过滤文档）、
`update_raw()`（`$inc` / `$unset` 等原生更新算子）、`aggregate()`（聚合管道）。
这与 `Query` 既有 `where_raw` / `field_raw` / `order_raw` 的设计一致。

### 同一字段的多条件为什么要合并

`where("age", ">=", 18).where("age", "<=", 30)` 若编译成
`{"$and": [{"age": {"$gte":18}}, {"age": {"$lte":30}}]}`，MongoDB 仍能正确
命中复合索引，但可读性差且计划缓存命中率低。实现上优先合并进同一个算子字典
`{"age": {"$gte": 18, "$lte": 30}}`，**无法合并时**（同字段同算子不同值）才
退回 `$and`。

### OR 条件的分组

OR 条件单独成组（`{"$or": [...]}`）再与 AND 组用 `$and` 连接。若把多个 OR
条件平铺进同一个 `$and`，语义会从「A 或 B」变成「(A 或 B) 且 (C 或 D)」——
这是实现过程中发现并修正的一个真实缺陷。

## 4. 配置来源优先级

需求要求"支持从环境变量或配置中心读取，配置缺失时给出明确报错"。实现为
四级优先级：

**显式配置 > 配置中心 > 环境变量 > 默认值**

| 决策 | 理由 |
|---|---|
| 显式配置最高 | 调用方代码是最明确��意图 |
| 配置中心高于环境变量 | 配置中心是运行期可变的（如 Nacos 热更新），环境变量是部署期固定的；运行期来源应能覆盖部署期默认值 |
| 环境变量高于默认值 | 容器化部署的常规做法 |
| 主机/端口有默认值，数据库名没有 | 主机默认 `127.0.0.1:27017` 与 MySQL 驱动一致；数据库名默认是 `test`，几乎总是误配，必须显式指定 |

「配置中心」用 `options["config_resolver"]`（可调用对象，签名 `resolver() -> dict`）
表达——项目核心保持零依赖，不引入任何配置中心 SDK；接入方自己传回调即可。
回调在**每次连接时**被调用，因此天然支持热更新。

## 5. 版本：为什么与 MySQL 侧不同

MySQL 侧必须显式声明 `version`：SQL 方言差异无法从服务端廉价得知，猜错版本
会让版本门禁静默放行或静默拦截。

MongoDB 不同——服务端版本可以通过 `hello` 命令（返回 `maxWireVersion`）
**廉价探测**，且该命令无需额外权限。因此：

- 能力门禁以**实际探测值**为准，而不是配置上的声明；
- 显式声明的 `version` 退化为一条**断言**：声明版本高于实际服务端时，
  连接阶段立即报错（配置与现实不符是必须暴露的错误）；
- 声明版本低于实际服务端时**不报错**，仍以探测值为准。

支持范围 4.0.x / 4.2.x / 4.4.x / 5.0.x / 6.0.x / 7.0.x / 8.0.x。4.x 只放行
偶数发布线（4.0 / 4.2 / 4.4）——奇数小版本是开发版，不保证生产可用。

能力矩阵 `MONGO_FEATURES`（最低版本）与 `MONGO_REMOVED`（移除版本）集中在
驱动层，上层通过 `supports_feature()` / `require_feature()` 询问，不在业务
代码里散落版本号判断。

## 6. 异常归口

需求要求"数据库异常统一转换为项目既有的错误格式"。项目既有的错误格式是
`exceptions.py` 的异常体系（`OrmError` 为根）。

翻译表：

| 第三方异常 | 项目异常 |
|---|---|
| `DuplicateKeyError` | `DuplicateKeyError`（新增） |
| `InvalidId` / `InvalidDocument` / `InvalidOperation` / `ConfigurationError` | `InvalidArgumentException` |
| `ServerSelectionTimeoutError` / `ConnectionFailure` / `NetworkTimeout` / `AutoReconnect` | `ConnectionFailedError`（新增） |
| `OperationFailure`（认证类 13/18） | `ConnectionFailedError` |
| `OperationFailure` / `WriteError` / `BulkWriteError` / `ExecutionTimeout` | `QueryError` |

两个设计细节：

1. **按类名而非 `isinstance` 匹配**：翻译逻辑在"pymongo 未安装"的环境里也要
   能被单元测试覆盖（`test_mongo.py` 用同名假异常类驱动），因此用类名字符串
   匹配，已属于 `OrmError` 的异常原样返回。
2. **认证失败归入连接错误**：真实服务端验证时发现，`OperationFailure`（码 13
   "requires authentication"）本质是**连接层**问题——调用方需要重连或告警，
   而不是重试查询，因此单独识别并归入 `ConnectionFailedError`。

### 集合层必须走统一入口

所有 pymongo 调用都经由 `MongoCollection._exec()`（内部捕获并翻译）。早期
实现直接在方法里调 `self._raw().insert_one(...)`，结果在真实服务端上
**`pymongo.errors.OperationFailure` 直接泄漏给调用方**——离线测试用假客户端
无法发现这类问题（假客户端不抛异常）。修复后改为统一入口，并补了回归测试
（`test_collection_calls_translate_third_party_errors`）。

## 7. 输入校验

校验分两类，异常类型不同：

| 类别 | 异常 | 例子 |
|---|---|---|
| **入参非法**（调用方传错了） | `InvalidArgumentException` | 集合名 / 字段名非法、文档不是 dict、必填项缺失、类型不符、分页参数越界、排序方向非法 |
| **查询构造非法**（语义上不成立） | `QueryError` | 运算符不在词汇表内、`in` 的值不是列表、`between` 的值不是二元组 |

沿用 `DocumentCollection` 的 schema 语义：只校验**已声明且出现**的字段，
不要求必填、不拒绝未声明字段（schemaless 精神）；`required` 是 v0.12.0
新增的独立参数。类型规则同样沿用：`float` 宽容接受 `int`，`bool` 与
`int`/`float` 互不兼容（`bool` 是 `int` 的子类，不特殊处理会误判）。

## 8. 性能设计

### 默认 limit 上限

未显式 `limit()` 时套用默认上限 1000（`options["max_limit"]` 可调）。
理由：MongoDB 的 `find` 不带 limit 会**把整个游标拉进内存**——这正是
MongoDB 侧的经典 OOM 源。宁可截断也不失控。

### 投影

`field()` / `exclude()` 直接映射到 MongoDB 投影，减少网络传输与内存占用。
但要提醒：MongoDB 的投影**不减少服务端扫描量**（除非命中覆盖索引），
它省的是传输与内存。

### 索引

`ensure_index()` 封装 `create_index`，支持单字段 / 复合 / 降序。
`explain()` 暴露查询计划，用于自查是否走了索引——这是 MongoDB 侧最实用的
性能自检手段（`"IXSCAN" in str(plan)` vs `"COLLSCAN"`）。

复合索引字段顺序的建议（等值在前、范围/排序在后）与关系型数据库一致，
因为 MongoDB 的复合索引同样遵循「等值前缀 + 范围字段」的有效前缀规则。

### 批量写

`insert_many()` 用 pymongo 的 `insert_many`（单次网络往返），默认
`ordered=False`（遇错继续，导入场景常用）；需要遇错即停时用
`insert_many_ordered()`。`bulk_write()` 支持混合操作。

**不要循环单条 `insert()`**——每条一次往返，量大时差异是数量级的。

### N+1

MongoDB 没有 JOIN，关联要么用 `$lookup`（服务端一次完成），要么批量取回后
在内存里关联。`aggregate()` 与 `where_in()` 分别支持这两条路径。

### 分页方案取舍

| 方案 | API | 代价 | 适用 |
|---|---|---|---|
| 偏移量 | `page(n, rows)` | 底层 `skip`，代价随页码**线性增长**；并发写入时可能重复/遗漏 | 数据量小、页数浅 |
| 游标（keyset） | `after(last_id)` | 代价与页码无关（走 `_id` 索引），稳定不重不漏 | 深翻页、无限滚动 |

两者都提供，因为取舍取决于数据规模与是否需要随机跳页。游标分页要求排序
字段单调递增（`_id` 默认满足）。

## 8.5 真实服务端验证发现的缺陷（离线测试测不出）

拿到真实凭据后（MongoDB 8.3.13 / 单节点 / 跨网络 ~75ms RTT / 5 万条数据）跑了
32 项集成测试，**暴露出 6 个离线测试无法发现的缺陷**。记录在此，因为它们都是
"假客户端不会模拟"的路径：

| # | 缺陷 | 为什么离线测不出 | 修复 |
|---|---|---|---|
| 1 | 版本探测只返回 `8.0.0`，真实是 `8.3.13` | 假客户端只提供 `maxWireVersion` | 改为 `buildInfo` 优先（精确），`maxWireVersion` 兜底（下界） |
| 2 | `Db.mongo(write_concern={...})` 直接抛 `TypeError` —— 功能完全不可用 | 假集合的 `get_collection` 不校验参数类型 | 转成 `pymongo.WriteConcern` 实例；键名先校验（离线可测） |
| 3 | 集合的**构造**异常绕过翻译（`self._raw()` 在 try 之外求值） | 假集合从不抛异常 | `_exec` 内先 `_call(self._raw)` 再调方法 |
| 4 | `insert_many` 撞唯一键被归为 `QueryError` 而非 `DuplicateKeyError` | 假 `insert_many` 不实现唯一约束 | 从 `BulkWriteError.details.writeErrors` 识别错误码 11000 |
| 5 | 单节点部署被报告"支持事务" | 假客户端没有拓扑概念 | 新增拓扑探测与 `MONGO_TOPOLOGY_FEATURES` 拓扑门禁 |
| 6 | 服务端不可达时 `connect()` 静默成功（pymongo 惰性连接） | 假客户端总是"连得上" | 版本探测失败即抛 `ConnectionFailedError`，并回滚连接状态 |

每一条都补了**离线回归测试**（用同名假异常类 / 可配置拓扑的假客户端），
确保不依赖真实服务端也能守住。

### 教训

1. **假客户端只能验证"我构造了什么"，不能验证"服务端怎么回应"**。异常类型、
   参数类型校验、拓扑差异都必须在真实环境验证。
2. **端到端测试要读服务端的 `executionStats`，不要只信客户端计时**。该服务端
   单次往返 ~75ms，客户端计时里索引带来的 21ms 差异几乎不可见；而
   `docsExamined`（50,000 → 6,192）才是硬证据。
3. **能力判断不能只看版本**。MongoDB 的事务依赖副本集；版本 8.3.13 的单节点
   部署照样不支持。凡是"部署形态相关"的能力都要单独建模。

## 9. 已知限制

1. **集成测试需自备凭据**：`test_mongo_live.py`（32 项）读环境变量
   `TINKPYORM_MONGO_TEST_URI`，未设置时整份跳过。已在 MongoDB 8.3.13
   （单节点）以 5 万条数据实测通过，覆盖 CRUD、异常归口、写关注、索引命中、
   游标分页与 `$lookup`。
2. **不支持多文档事务**：MongoDB 事务（4.0+，且需副本集）需显式 session，
   与项目 `Db.transaction()` 的 SQL 语义不兼容，故 `supports_transactions = False`。
3. **不支持 ORM 模型层**：`Model` / 关联 / JOIN 均为关系型概念。
4. **`Db.collection()` 始终是 SQLite 文档存储**，MongoDB 侧用 `Db.mongo()`。
5. **upsert 语义不统一**：关系型走 `Query.upsert()`，MongoDB 侧走
   `update(..., upsert=True)`，故 `supports_upsert = False`。
6. **全文检索语义不同**：MongoDB 用 text / wildcard 索引，`supports_fts = False`。

## 10. 与既有代码的对应关系

| 既有概念 | MongoDB 侧对应 |
|---|---|
| `Db.set_config` / `Db.config` | 同一入口，`type="mongodb"` |
| `Config`（`tinkpyorm/config.py`） | 同一值对象；驱动专属键落 `options` |
| 驱动注册表（`drivers/__init__.py`） | `_LAZY_DRIVERS["mongodb"]` 惰性导入，缺依赖时给安装提示 |
| `DocumentCollection`（`document.py`） | `MongoCollection`（`mongo.py`），同为「集合服务层」 |
| `JSON_OPS`（`builder.py`） | `MONGO_OPS`（`mongo.py`）——**直接复用** `JSON_OPS`，保证两套文档 API 词汇一致 |
| `Query.where_raw` / `field_raw` / `order_raw` | `where_raw` / `update_raw` / `aggregate` |
| `exceptions.py` 异常体系 | 新增 `DuplicateKeyError` / `ConnectionFailedError` |
| `versioning.py`（v0.11.0） | 复用 `parse_version`，新增 `product="mongodb"` 分支与范围校验 |
| `Driver.supports_*` 能力标志 | 新增 `supports_mongo` |
| `wiki/29-MySQL支持.md` | `wiki/30-MongoDB支持.md`（同结构：配置 / 版本 / 能力矩阵 / 限制） |
