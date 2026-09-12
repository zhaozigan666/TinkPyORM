# JSON 查询设计与多数据库扩展说明

> 对应版本：v0.5.0（`supports_json` 能力契约 + `JsonWhere` 条件对象）
> 相关代码：`tinkpyorm/drivers/base.py`、`tinkpyorm/drivers/sqlite.py`、
> `tinkpyorm/builder.py`、`tinkpyorm/query.py`

---

## 1. 目标

为 ORM 增加 JSON 字段的读写能力，并满足三个约束：

1. **写入免手工序列化** —— `dict` / `list` 值直接写库；
2. **读出自动格式化** —— 查询结果中的 JSON 列自动还原为 Python `dict` / `list`；
3. **方言可扩展** —— SQLite 先落地，MySQL / PostgreSQL / MongoDB / Redis
   只预留实现位置，**不写具体连接代码**，未来接入时不改动 Query / Builder / Model。

第 3 条是设计的主要驱动力：JSON 的 SQL 语法在各数据库间差异极大
（`json_extract` / `JSON_EXTRACT` / `#>` / 原生 dict 查询），
一旦让 Query 层感知方言，后续每加一个数据库都要改上层。

---

## 2. 分层与数据流

```
Query.where_json()            # 只做参数归一化与安全校验，不含方言
      │  产出 JsonWhere(column, path, op, value, func)
      ▼
Builder._build_json_condition()   # 运算符分派 + 参数绑定顺序
      │  调用 driver.json_extract / json_contains / ...
      ▼
Driver（方言层）              # 只返回 SQL 表达式片段
      │  SQLiteDriver: json_extract(`c`, '$.a')
      ▼
Connection.query()            # 执行 + 日志 + 线程锁
      ▼
Query._process_row()          # 调用 driver.json_decode() 自动解码
      ▼
dict / Model
```

关键点：**方言知识只存在于驱动**。`JsonWhere` 只是"条件描述"，
真正的 SQL 表达式在编译期由驱动给出。

---

## 3. 驱动扩展点契约

`Driver` 基类中的 JSON 能力（`tinkpyorm/drivers/base.py`）：

| 成员 | 类型 | 职责 |
|---|---|---|
| `supports_json` | 类属性 | 能力开关；`False` 时调用 JSON API 抛 `UnsupportedOperation` |
| `json_extract(col, path)` | 方法 | 路径取值表达式 |
| `json_exists(col, path)` | 方法 | 路径存在性表达式（值为 `null` 也算存在） |
| `json_contains(col, path, ph)` | 方法 | 数组包含标量的表达式，`ph` 为 Builder 给的占位符 |
| `json_length(col, path)` | 方法 | 元素个数表达式 |
| `json_type(col, path)` | 方法 | 类型名表达式 |
| `json_decode(value)` | 方法 | 数据库值 → Python 对象（Python 层，与方言无关） |
| `json_encode(value)` | 方法 | Python 对象 → 可绑定参数 |

公共工具（方言无关，所有驱动共用）：

- `normalize_json_path(path)` —— 把 `a.b` / `[0].id` / `$.a` 统一为 `$` 开头，
  非法路径抛 `InvalidArgumentException`；
- `json_path_literal(path)` —— 渲染为转义后的 SQL 字面量。

**返回 SQL 片段的约定**：驱动只返回表达式文本，`?` 占位符由 Builder 传入，
驱动**不得内联用户数据**（路径是唯一例外，因为它是标识符式语法，经白名单校验后内联）。

---

## 4. 各数据库实现映射

### 4.1 SQLite（已实现，参考实现）

| 能力 | 实现 |
|---|---|
| 路径取值 | `json_extract(col, '$.a')` |
| 路径存在 | `json_type(col, '$.a') IS NOT NULL` |
| 数组包含 | `EXISTS (SELECT 1 FROM json_each(col, '$.a') WHERE value = ?)` |
| 元素个数 | `(SELECT count(*) FROM json_each(col, '$.a'))` |
| 类型判断 | `json_type(col, '$.a')` |

选择 `json_each` 而非 `json_array_length` 是为了让"长度"对**对象**同样可用；
代价是路径不存在时返回 `0`，与空数组不可区分（已写入文档）。

### 4.2 MySQL（预留，未实现）

`SQLDriver` 的方言差异集中在 `_wrap_identifier` 与 JSON 家族。接入时新增
`tinkpyorm/drivers/mysql.py`：

```python
@register_driver("mysql")
class MySQLDriver(SQLDriver):
    name = "mysql"
    paramstyle = "format"          # pytMySQL 占位符为 %s
    supports_json = True

    def _wrap_identifier(self, name):        # MySQL 用反引号，与 SQLite 相同
        return f"`{name}`"

    def json_extract(self, col, path):
        return f"JSON_EXTRACT({col}, {json_path_literal(path)})"

    def json_exists(self, col, path):
        return f"JSON_CONTAINS_PATH({col}, 'one', {json_path_literal(path)})"

    def json_contains(self, col, path, ph):
        # 注意参数顺序：MySQL 是 (文档, 待查值, 路径)
        return (f"JSON_CONTAINS({col}, JSON_QUOTE({ph}), "
                f"{json_path_literal(path)})")

    def json_length(self, col, path):
        return f"JSON_LENGTH({col}, {json_path_literal(path)})"

    def json_type(self, col, path):
        return f"JSON_TYPE(JSON_EXTRACT({col}, {json_path_literal(path)}))"

    def json_decode(self, value):            # 驱动返回 JSON 文本，同 SQLite
        return super().json_decode(value)
```

要点：

- `paramstyle = "format"` 后 `Driver.placeholder()` 自动返回 `%s`，
  Builder 无需改动；
- `JSON_QUOTE(?)` 是 MySQL 的"把绑定值解释为 JSON"惯用法，
  用于 `contains` 的标量比较；
- 类型名与 SQLite 一致（`integer` / `text` / `array` / `object` / `null`），
  `where_json_type` 无需翻译。

### 4.3 PostgreSQL（预留，未实现）

```python
    def json_extract(self, col, path):
        return f"({col} #> '{pg_path(path)}')"      # 路径数组形式 '{a,b}'

    def json_contains(self, col, path, ph):
        return f"({col} @> {ph}::jsonb)"

    def json_length(self, col, path):
        return f"jsonb_array_length({col} #> '{pg_path(path)}')"

    def json_type(self, col, path):
        return f"jsonb_typeof({col} #> '{pg_path(path)}')"
```

注意 Postgres 的路径语法是 `'{a,b}'` 而非 `'$.a.b'`，
需在驱动内把 `normalize_json_path()` 的结果再转一次；
`json_type` 的返回值（`number` / `string` / `boolean`）与 SQLite 命名不同，
若要保持上层一致，驱动内需做一层归一化映射。

### 4.4 MongoDB / Redis（预留，走 NoSQL 路径）

两者没有 SQL 层，且**原生返回的就是 Python 对象**，因此：

- 继承 `NoSQLDriver`（已内置 `supports_json = True` 与恒等编解码）；
- `json_decode` / `json_encode` 保持恒等 —— 不解析字符串，避免把普通字段误判为 JSON；
- 调用 `where_json*` 会抛 `UnsupportedOperation`（提示改用 `raw_connection`），
  因为 SQL 条件构造器对 NoSQL 无语义；
- 但**结果格式化路径完全复用**：`Query.json()` / `_process_row()` /
  模型 `__json__` 无需任何分支即可工作。

这正是把 `json_decode` 放在驱动层而非 Query 层的原因。

---

## 5. 语义定义

### 5.1 `is null` 与 `exists`

JSON 中"键不存在"与"键值为 `null`"是两种状态，而 `json_extract` 对两者都返回
SQL `NULL`。因此拆成两组语义：

| 条件 | 生成的 SQL | 命中情况 |
|---|---|---|
| `where_json_null(f, p)` | `json_extract(f, p) IS NULL` | 值非 null **或** 路径不存在 |
| `where_json_exists(f, p)` | `json_type(f, p) IS NOT NULL` | 路径存在（含值为 JSON `null`） |
| `where_json_type(f, p, 'null')` | `json_type(f, p) = 'null'` | 路径存在且值为 JSON `null` |

`where_json(f, p, None)` 会自动转为 `is null` 语义（`!=` 则转为 `is not null`）。

### 5.2 运算符白名单

`builders.JSON_OPS`：

```
=  !=  <>  >  <  >=  <=
like  not like  in  not in  between  not between
is null  is not null  null  not null
exists  not exists  contains  not contains
```

### 5.3 值转换

| Python 值 | 转换 | 原因 |
|---|---|---|
| `True` / `False` | `1` / `0` | JSON 布尔在 SQLite / MySQL 中存为整数 |
| `dict` / `list` | 抛 `QueryError` | JSON 文本比较受键序与空白影响，不可靠 |
| 其它 | 原样绑定 | — |

---

## 6. 安全模型

三层独立校验，任一层不通过即中止：

1. **路径**（`normalize_json_path`）—— 白名单正则
   `^\$(?:\.(?:ident|"…")|\[\d+\]|\[#\])*$`，拒绝引号、分号、空格等；
   通过后渲染为 SQL 字面量并转义单引号。
2. **列名 / 别名**（`Query._json_ident` / `_json_alias`）—— 必须匹配
   `^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?$`。
   这一步是必要的：`quote_identifier()` 出于兼容表达式（`COUNT(*)`）的考虑，
   对非标识符会**原样返回**，若在 JSON API 上不校验，`field_json(col, p, 'x; DROP TABLE t')`
   就会形成注入。
3. **值** —— 一律 `?` 参数绑定，由 Builder 统一处理。

`test_json.py::TestSafety` 与 `TestPathAndSafety` 固化了这三层的行为。

---

## 7. 已知限制

| 限制 | 说明 | 替代方案 |
|---|---|---|
| 复合值不支持相等比较 | 键顺序 / 空白差异导致文本比对不可靠 | `where_json_contains` 或 `where_raw` |
| `json_length` 无法区分"路径不存在"与"空数组" | 两者均为 0 | 组合 `where_json_exists` |
| 自动嗅探可能误判 | `json()` 会把 `'[1,2]'` 之类字符串解析为列表 | 用 `json([...])` 显式声明字段 |
| 不支持 JSON 路径索引优化 | SQLite 需 `json_extract` 表达式索引才能走索引 | 建表达式索引，或用生成列 |
| NoSQL 不支持 `where_json*` | SQL 条件构造器对 Redis / Mongo 无语义 | `raw_connection` 走原生查询 |

---

## 8. 测试覆盖

`test_json.py`（78 项）：

| 分组 | 项数 | 覆盖内容 |
|---|---|---|
| `TestWriteEncoding` | 5 | insert / insert_all / update 自动序列化，普通值不受影响 |
| `TestAutoFormat` | 13 | 各终端方法与 `json()` 三种模式；返回值隔离（防缓存污染） |
| `TestModelJson` | 6 | `__json__` / `__type__` 自动接入、`to_dict`、反序列化幂等 |
| `TestWhereJson` | 20 | 全部运算符、语义化方法、嵌套路径、与普通条件混用 |
| `TestJsonFields` | 12 | `field_json` 合并语义、自动别名、SQL 形态、`order_json` |
| `TestPathAndSafety` / `TestSafety` | 11 | 路径规范化、注入防护、非法运算符与复合值 |
| `TestDriverContract` | 5 | NoSQL 恒等、SQLite 表达式形态、能力开关报错 |

---

## 9. 后续可做

1. **`where_json_where` 子集匹配** —— 用 `json_each` 逐键比对实现对象子集查询；
2. **JSON 表达式索引助手** —— 生成 `CREATE INDEX ... ON t(json_extract(c,'$.k'))` 的 DDL 辅助方法；
3. **`json_patch` / `json_set` 写入助手** —— 局部更新 JSON 字段而非整列覆盖；
4. **MySQL / PostgreSQL 驱动落地** —— 按 §4.2 / §4.3 的骨架实现并补齐 `_LAZY_DRIVERS` 注册。
