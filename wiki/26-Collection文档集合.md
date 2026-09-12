# Collection 文档集合

> [TinkPyORM](https://github.com/zhaozigan666/TinkPyORM) 技术文档 · [文档索引](Home.md) · 上一页：[25-开发与测试](25-开发与测试.md)

`DocumentCollection`（v0.8.0）提供 **schemaless 文档存储**：无需提前建表、无需声明字段，即可像操作文档一样读写 SQLite。入口为 `Db.collection(name)`，底层复用 [JSON 查询与格式化](11-JSON查询与格式化.md) 与 [JSON 路径写入](12-JSON路径写入.md) 的全部基建。

> 命名说明：`DocumentCollection` 是**文档集合**（存储入口）；`Collection` 是查询**结果集**封装（见 [16-结果集与分页](16-结果集与分页.md)），两者是不同概念。

## 快速上手

```python
from tinkpyorm import Db

Db.set_config({"database": "app.db"})
docs = Db.collection("users")           # 首次访问自动建表

uid = docs.insert({"name": "张三", "age": 25,
                   "profile": {"city": "北京"}, "tags": ["vip", "beta"]})

docs.find(uid)                                    # 按 _id 取单条
docs.where("profile.city", "=", "北京").select()   # 点路径条件
docs.where("age", ">=", 18).where("tags", "contains", "vip").select()
docs.where("name", "张三").order("age", "desc").limit(10).select()

docs.where("_id", uid).update_path({"age": 26})              # 路径级局部更新
docs.where("_id", uid).patch({"profile": {"city": "上海"}})   # RFC 7396 合并
docs.where("_id", uid).update({"name": "李四"})               # 整文档替换
docs.where("age", "<", 18).delete()
```

## 存储布局

每个集合一张表，固定两列：

| 列 | 类型 | 说明 |
|---|---|---|
| `_id` | `INTEGER PRIMARY KEY AUTOINCREMENT` | 文档主键；插入时可在文档内显式指定 |
| `data` | `TEXT NOT NULL DEFAULT '{}'` | JSON 文档列，文档全部字段存于此 |

- 建表幂等（`CREATE TABLE IF NOT EXISTS`），**字段增减不需要任何 DDL**
- 文档内出现 `_id` 键时：插入期作为主键写入，读取期以存储主键为准
- 文档内可以有名为 `data` 的普通键（存于 JSON 内，与存储列互不冲突）

## 条件查询

字段名即 JSON 路径：`"name"` / `"profile.city"` / `"tags[0]"`（数组索引用 `[n]` 写法）。操作符与 `Query.where` **同一套词汇**（`tinkpyorm.JSON_OPS`）：

| 写法 | 语义 |
|---|---|
| `where("age", ">=", 18)` | 比较：`= != <> > >= < <=` |
| `where("name", "张三")` | 省略运算符 = 等于 |
| `where("tags", ["vip", "beta"])` | 值为列表 = `IN` |
| `where("deleted", None)` | 值为 null 或路径不存在 |
| `where("deleted", "exists")` | 路径存在（值为 null 亦算存在） |
| `where("tags", "contains", "vip")` | 数组包含某标量元素 |
| `where_length("tags", ">=", 3)` | 数组 / 对象元素个数 |
| `where({"city": "北京", "age": 18})` | 字典形式，AND 连接 |
| `where_or(...)` | OR 连接 |

读取终端：`select()`（返回结果集 Collection，元素为文档 dict）/ `find(pk)` / `value(path)` / `column(path)` / `count()` / `distinct(path)` / `group_counts(path)`（返回 `[{'value': .., 'count': ..}]`，按 count 降序）。

链式方法（`where / order / limit / page`）修改实例内部状态，**终端方法执行后状态自动复位**——同一实例可连续发起多个独立查询。

## 写入

| 方法 | 语义 |
|---|---|
| `insert(doc)` | 插入，返回 `_id`；文档内 `_id` 可显式指定主键 |
| `insert_all(docs)` | 批量插入；`_id` 要么全带要么全不带，混用报错 |
| `update(doc)` | **整文档替换**（未提及字段消失）；`_id` 不可改；需先 `where` |
| `update_path({路径: 值})` | 路径级局部更新，一条 SQL 原子写入，其余字段保留 |
| `patch(partial)` | RFC 7396 合并：对象递归合并、数组整体替换、`null` 删除键 |
| `delete(pk=None)` | 按 `_id` 或当前条件删除 |

`update_path` / `patch` 的并发语义与 [12-JSON路径写入](12-JSON路径写入.md) 一致：SQL 内完成改写，没有"读出 → 改 → 写回"窗口。

## 字段提升与索引

### promote：字段提升为生成列

```python
docs.promote("age")                                # 列名默认 age
docs.promote("profile.city")                       # 嵌套路径 -> 列 profile_city
docs.promote("age", sql_type="INTEGER", column="user_age")
```

提升把字段加为 **VIRTUAL 生成列**：`ALTER TABLE … ADD COLUMN age GENERATED ALWAYS AS (json_extract(data,'$.age')) VIRTUAL`。

- 值由引擎按需计算，**写路径零改动、永不失同步**；VIRTUAL 加列瞬时完成，不回填数据
- 提升后该字段条件与排序**自动改走列**（解析顺序：`_id` → 提升列 → JSON 路径）
- 仅支持对象键路径（不含 `[n]`）；`sql_type` 省略时不声明列类型，保持 JSON 原生类型比较语义
- 列名冲突 / 重复提升 / 保留名（`_id`、`data`）显式报错
- 提升注册表从 `sqlite_master` 的 DDL 文本解析，不引入元数据表，跨实例自洽

### ensure_index：索引

```python
docs.ensure_index("age")                             # 提升列 -> 普通索引
docs.ensure_index("profile.city", name="idx_city")   # 其余字段 -> 表达式索引
docs.ensure_index("uid", unique=True)
```

- 幂等（`CREATE INDEX IF NOT EXISTS`）；索引名默认 `idx_{表名}_{路径}`
- 表达式索引依赖 `json_extract` 为确定性函数（SQLite 允许）；**高频查询路径建议提升或建索引**，否则路径条件为全表扫描
- 唯一索引下，缺失路径的行互不冲突（SQLite 视 NULL 为互异）

## Schema 校验（可选）

```python
docs.schema({"age": int, "name": str, "tags": list})
```

- 开关在 `Db.set_config`：`{"collection_schema_mode": True}`（默认**关闭**）
- 关闭时声明仅作文档；开启后在**写入期**校验已声明路径上已出现的值类型
- 只查已声明路径，**不要求必填、不拒绝未声明字段**（贴合 schemaless 精神）；值为 `None` 放行
- 支持类型：`bool / int / float / str / list / dict`；`float` 宽容接受 `int`；`bool` 与 `int` / `float` 互不兼容
- `update_path` / `patch` 对已声明路径同样校验（嵌套补丁结构不校验）

## 边界与注意事项

1. **数组元素级操作受限**：`where("tags[0]", "=", "vip")` 等固定下标可用；"数组内存在满足条件的元素"（MongoDB `$elemMatch` 类语义）没有等价支持，需用 `contains`（标量成员判断）替代。
2. **类型比较语义**：路径条件按 JSON 原生类型比较（数字对数字、文本对文本）。存入 `"17"`（字符串）不会命中 `where("age", 17)`——需要类型一致性时开启 schema 校验或声明 `sql_type`。
3. **整文档替换的代价**：`update` 重写整个 `data` 列；局部改动用 `update_path` / `patch`。
4. **update / delete 需要条件**：与 `Query` 一致，无条件写操作会报错；全表更新可用 `where("_id", ">", 0)` 显式表达。
5. **MySQL 支持**：当前实现基于 SQLite JSON1 与生成列；驱动抽象层与 JSON 扩展点已预留多库形态，MySQL 未在承诺范围内。
6. **与结果集 Collection 的命名**：导入时注意 `from tinkpyorm import DocumentCollection`（文档集合）与 `Collection`（结果集）是两个类。

## API 速览

```python
Db.collection(name)                    # -> DocumentCollection

# 链式
.where(field, op?, value?) / .where_or(...) / .where_length(path, op, value?)
.order(path, direction?) / .limit(n, offset?) / .page(page, rows)
.schema({字段: 类型})                  # 可选声明

# 读取终端（执行后链式状态复位）
.select() / .find(pk?) / .value(path, default?) / .column(path)
.count() / .distinct(path) / .group_counts(path)

# 写入终端
.insert(doc) -> _id / .insert_all(docs) -> n / .update(doc) -> n
.update_path({路径: 值}) -> n / .patch(partial) -> n / .delete(pk?) -> n

# 索引与提升（DDL，即时生效）
.promote(path, sql_type?, column?) / .ensure_index(path, unique?, name?)
```

对应的底层 Query 能力见 [02-查询构造器与条件](02-查询构造器与条件.md)、[11-JSON查询与格式化](11-JSON查询与格式化.md)、[12-JSON路径写入](12-JSON路径写入.md)；设计动机与实现取舍见 [`docs/collection.md`](https://github.com/zhaozigan666/TinkPyORM/blob/main/docs/collection.md)。
