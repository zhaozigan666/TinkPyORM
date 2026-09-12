# JSON 读写设计与多数据库扩展说明

> 对应版本：v0.6.0（`supports_json` 能力契约 + `JsonWhere` 条件对象 +
> `JsonUpdate` 写入规格 + 路径级局部更新）
> 相关代码：`tinkpyorm/drivers/base.py`、`tinkpyorm/drivers/sqlite.py`、
> `tinkpyorm/builder.py`、`tinkpyorm/query.py`

---

## 1. 目标

为 ORM 增加 JSON 字段的读写能力，并满足四个约束：

1. **写入免手工序列化** —— `dict` / `list` 值直接写库；
2. **读出自动格式化** —— 查询结果中的 JSON 列自动还原为 Python `dict` / `list`；
3. **路径级局部更新** —— 不读出整列即可改写嵌套字段，且并发交错不丢更新；
4. **方言可扩展** —— SQLite 先落地，MySQL / PostgreSQL / MongoDB / Redis
   只预留实现位置，**不写具体连接代码**，未来接入时不改动 Query / Builder / Model。

第 4 条是设计的主要驱动力：JSON 的 SQL 语法在各数据库间差异极大
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

写入侧与之对称，`JsonUpdate` 是"操作描述"，由 Builder 合并后向驱动取表达式：

```
Query.update_json()           # 参数归一化 + 安全校验，不含方言
      │  产出 JsonUpdate(column, path, value, mode, ifnull)
      ▼
Builder._build_json_update()  # 按列分组 → 相邻同类合并 → 异类按序嵌套
      │  调用 driver.json_set / json_insert / json_remove / json_patch
      ▼
Driver（方言层）              # 只返回 SQL 表达式片段
      │  SQLiteDriver: json_set(`c`, '$.a', ?)
      ▼
Connection.execute()          # 执行 + 日志 + 线程锁 + 缓存失效
```

写入侧多了一层**合并**逻辑，原因是 SQL 语义限制：同一列出现多个 SET 子句时
只有最后一个生效（实测见 §5.4），因此同一列的多个路径操作必须压进一次函数调用。

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

写入侧扩展点（v0.6.0）：

| 成员 | 类型 | 职责 |
|---|---|---|
| `json_set(col, pairs, ifnull)` | 方法 | 路径写入（存在覆盖、不存在新增）；可含多组 `(路径, 值片段)` |
| `json_insert(col, pairs, ifnull)` | 方法 | 路径写入，仅当路径不存在时生效 |
| `json_remove(col, paths, ifnull)` | 方法 | 删除路径，可多个 |
| `json_patch(col, value_sql, ifnull)` | 方法 | RFC 7396 合并补丁 |
| `json_bind(ph, value)` | 方法 | 把占位符转成"以 JSON 值绑定"的片段（SQLite 为 `json(?)`） |

`ifnull` 是空列兜底参数：`json_set(NULL, ...)` 返回 NULL（更新静默无效），
传入空 `dict` / 空 `list` 后驱动编译为 `COALESCE(col, '{}')` / `COALESCE(col, '[]')`。
兜底文档是**驱动常量字面量**，不引入绑定参数。

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
| 路径写入 | `json_set(col, '$.a', ?)` / `json_insert(...)` |
| 路径删除 | `json_remove(col, '$.a', '$.b')` |
| 文档合并 | `json_patch(col, ?)` |
| JSON 值绑定 | `json(?)`（容器与布尔需要，否则被当作文本） |

选择 `json_each` 而非 `json_array_length` 是为了让"长度"对**对象**同样可用；
代价是路径不存在时返回 `0`，与空数组不可区分（已写入文档）。

`json_bind` 必须包装两类值，否则会静默出错：

| 值 | 不包装的后果 | 包装后的效果 |
|---|---|---|
| `dict` / `list` / `tuple` | 存成转义字符串 `{"n":"{\"k\":1}"}` | 存成嵌套对象 `{"n":{"k":1}}` |
| `bool` | sqlite3 适配为整数 `1`，`json_type` 报 `integer` | 存成 JSON `true`，`json_type` 报 `true` |

标量（数字 / 文本 / `None`）不包装：TEXT 参数被 `json_set` 当 JSON 字符串、
SQL `NULL` 被当 JSON `null`，语义已经正确。

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

    # ---- 写入侧（v0.6.0） ---- #
    def json_set(self, col, pairs, ifnull=None):
        base = self._json_base(col, ifnull)          # COALESCE 兜底，可复用
        args = ", ".join(f"{p}, {v}" for p, v in pairs)
        return f"JSON_SET({base}, {args})"

    def json_insert(self, col, pairs, ifnull=None):
        base = self._json_base(col, ifnull)
        args = ", ".join(f"{p}, {v}" for p, v in pairs)
        return f"JSON_INSERT({base}, {args})"

    def json_remove(self, col, paths, ifnull=None):
        base = self._json_base(col, ifnull)
        return f"JSON_REMOVE({base}, {', '.join(paths)})"

    def json_patch(self, col, value_sql, ifnull=None):
        return f"JSON_MERGE_PATCH({self._json_base(col, ifnull)}, {value_sql})"

    def json_bind(self, ph, value):
        # MySQL 的 JSON 列可直接接收 JSON 文本参数，无需 SQLite 的 json() 包装；
        # 若列类型为 TEXT，语义与 SQLite 相同（存文本），此时按需改用 CAST
        return ph
```

要点：

- `paramstyle = "format"` 后 `Driver.placeholder()` 自动返回 `%s`，
  Builder 无需改动；
- `JSON_QUOTE(?)` 是 MySQL 的"把绑定值解释为 JSON"惯用法，
  用于 `contains` 的标量比较；
- 类型名与 SQLite 一致（`integer` / `text` / `array` / `object` / `null`），
  `where_json_type` 无需翻译；
- **写入侧需注意 NULL 语义**：MySQL 的 `JSON_SET(NULL, ...)` 同样返回 NULL，
  `_json_base` 的 COALESCE 兜底逻辑应一并移植（可提到基类复用）。

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

    # ---- 写入侧（v0.6.0） ---- #
    def json_set(self, col, pairs, ifnull=None):
        # Postgres 的 jsonb_set 一次只改一个路径，需按序嵌套；
        # create_missing 传 true 才能新增不存在的路径
        expr = self._json_base(col, ifnull)          # COALESCE(col, '{}'::jsonb) 等
        for path, value_sql in pairs:
            expr = f"jsonb_set({expr}, '{pg_path(path)}', {value_sql}, true)"
        return expr

    def json_insert(self, col, pairs, ifnull=None):
        # Postgres 无 json_insert；jsonb_set 的 create_missing 为 true 时
        # 会覆盖已有路径，需用 `col ? path` 判存在后条件拼接（或用 CASE）
        raise UnsupportedOperation("PostgreSQL 需用 jsonb_set + 存在性判断模拟")

    def json_remove(self, col, paths, ifnull=None):
        expr = self._json_base(col, ifnull)
        for path in paths:                           # `-` 运算符一次删一个路径
            expr = f"({expr} - '{pg_path(path)}')"
        return expr

    def json_patch(self, col, value_sql, ifnull=None):
        # `||` 合并 jsonb：注意其语义为"浅合并 + 后者覆盖"，
        # 与 RFC 7396 的"null 删除键"并不等价，需要驱动内做语义适配
        return f"({self._json_base(col, ifnull)} || {value_sql}::jsonb)"

    def json_bind(self, ph, value):
        return f"{ph}::jsonb"
```

注意 Postgres 的路径语法是 `'{a,b}'` 而非 `'$.a.b'`，
需在驱动内把 `normalize_json_path()` 的结果再转一次；
`json_type` 的返回值（`number` / `string` / `boolean`）与 SQLite 命名不同，
若要保持上层一致，驱动内需做一层归一化映射。

写入侧的额外注意点：`jsonb_set` 一次只能改一个路径，故驱动内需**自行嵌套**；
这不会造成同列多 SET 子句问题（Builder 已按列合并为一次调用），
但会带来与 SQLite 不同的表达式嵌套深度。

### 4.4 MongoDB / Redis（预留，走 NoSQL 路径）

两者没有 SQL 层，且**原生返回的就是 Python 对象**，因此：

- 继承 `NoSQLDriver`（已内置 `supports_json = True` 与恒等编解码）；
- `json_decode` / `json_encode` 保持恒等 —— 不解析字符串，避免把普通字段误判为 JSON；
- 调用 `where_json*` 会抛 `UnsupportedOperation`（提示改用 `raw_connection`），
  因为 SQL 条件构造器对 NoSQL 无语义；
- 同理，`update_json*` 族也抛 `UnsupportedOperation`（写入应映射为原生的
  `$set` / `$unset` 更新算子，而非 SQL 函数）；
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

### 5.4 写入侧语义（v0.6.0）

**绑定值转换**（与查询侧不同，写入侧需保留类型保真）：

| Python 值 | SQL 片段 | 绑定值 | 落库结果 |
|---|---|---|---|
| `dict` / `list` / `tuple` | `json(?)` | JSON 文本 | 嵌套对象 / 数组 |
| `True` / `False` | `json(?)` | `'true'` / `'false'` | JSON `true` / `false` |
| 数字 / 文本 | `?` | 原样 | JSON 数字 / 字符串 |
| `None` | `?` | `None` | JSON `null` |
| `raw(...)` | 内联表达式 | 无绑定 | 由表达式决定 |

**`null` 的两处相反含义**（易错点）：

| 写法 | 含义 |
|---|---|
| `update_json(c, '$.k', None)` | 把键 `k` 置为 JSON `null`（键仍存在） |
| `update_json_patch(c, {'k': None})` | 删除键 `k`（RFC 7396） |

**同列多 SET 子句会静默丢更新**（SQL 语义，非本库特有）：同一列出现多个
`SET c = ...` 时只有最后一个生效。因此 Builder 必须把同一列的多个路径操作
合并进一次函数调用：

| 用户写法 | 生成 SQL |
|---|---|
| `update_json(c, {'$.a': 1, '$.b': 2})` | `json_set(c, '$.a', ?, '$.b', ?)` |
| `update_json_remove(c, ['$.a', '$.b'])` | `json_remove(c, '$.a', '$.b')` |
| 普通赋值 + 路径写入（**同列**） | 拒绝：抛 `QueryError` |

异类操作（如 `set` 与 `remove` 同时出现）折叠为按序左嵌套
`json_remove(json_set(c, ...), ...)`；左嵌套保证占位符在 SQL 中的出现顺序
与参数追加顺序严格一致。公共 Query API 下每个终端方法只产生一种操作，
此路径为 Builder 直接调用者保留（`test_json.py` 有对应用例）。

**空列（SQL NULL）**：`json_set(NULL, ...)` 返回 NULL，更新静默无效。
默认保持 SQL 语义，需显式传 `ifnull={}`（对象）或 `ifnull=[]`（数组）才兜底。
不在默认行为里自动替换为 `{}`，因为空列的"根类型"（对象还是数组）无法推断。

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

写入侧沿用同一套闸口，另有两处专属约束：

4. **操作模式白名单** —— `JsonUpdate.mode` 必须是
   `set` / `insert` / `remove` / `patch`，非法值抛 `QueryError`；
5. **`ifnull` 只接受空容器** —— 空 `dict` / 空 `list` 以外一律
   `InvalidArgumentException`，因为它最终会被编译成 SQL 字面量。
   只放行"空"容器，是为了让可取值集合收敛为两个常量（`'{}'` / `'[]'`），
   从机制上排除把任意内容拼进 SQL 的可能。

`test_json.py::TestSafety`、`TestPathAndSafety` 与
`TestUpdateJsonSafety` 固化了这些行为。

---

## 7. 已知限制

| 限制 | 说明 | 替代方案 |
|---|---|---|
| 复合值不支持相等比较 | 键顺序 / 空白差异导致文本比对不可靠 | `where_json_contains` 或 `where_raw` |
| `json_length` 无法区分"路径不存在"与"空数组" | 两者均为 0 | 组合 `where_json_exists` |
| 自动嗅探可能误判 | `json()` 会把 `'[1,2]'` 之类字符串解析为列表 | 用 `json([...])` 显式声明字段 |
| 不支持 JSON 路径索引优化 | SQLite 需 `json_extract` 表达式索引才能走索引 | 建表达式索引，或用生成列 |
| NoSQL 不支持 `where_json*` | SQL 条件构造器对 Redis / Mongo 无语义 | `raw_connection` 走原生查询 |
| NoSQL 不支持 `update_json*` | 同上，写入应映射为 `$set` / `$unset` | `raw_connection` 走原生更新 |
| 路径写入仍重写整列 | JSON 列以文本存储，`json_set` 在存储层重写整列文本，写入放大与读改写相同 | 无——收益在往返次数与并发正确性，不在写入量 |
| 空列默认静默无效 | `json_set(NULL, ...)` 返回 NULL | 传 `ifnull={}` / `[]` |
| 路径写入是终端方法 | 与 `update()` 一致，立即执行，不可继续链式拼接 | 多个操作分别调用（各自一条语句） |

---

## 8. 测试覆盖

`test_json.py`（176 项）。查询与序列化部分（78 项）：

| 分组 | 项数 | 覆盖内容 |
|---|---|---|
| `TestWriteEncoding` | 5 | insert / insert_all / update 自动序列化，普通值不受影响 |
| `TestAutoFormat` | 13 | 各终端方法与 `json()` 三种模式；返回值隔离（防缓存污染） |
| `TestModelJson` | 6 | `__json__` / `__type__` 自动接入、`to_dict`、反序列化幂等 |
| `TestWhereJson` | 26 | 全部运算符、语义化方法、嵌套路径、与普通条件混用 |
| `TestJsonFields` | 13 | `field_json` 合并语义、自动别名、SQL 形态、`order_json` |
| `TestPathAndSafety` / `TestSafety` | 10 | 路径规范化、注入防护、非法运算符与复合值 |
| `TestDriverContract` | 5 | NoSQL 恒等、SQLite 表达式形态、能力开关报错 |

路径写入部分（98 项）：

| 分组 | 项数 | 覆盖内容 |
|---|---|---|
| `TestUpdateJsonSet` | 25 | 单/多路径、嵌套结构、根路径、类型保真（bool/int/text/None）、`where_json` 组合 |
| `TestUpdateJsonInsert` | 5 | 不覆盖已有值、不存在则新增、值为 null 的路径算"已存在" |
| `TestUpdateJsonRemove` | 8 | 单/多路径、嵌套路径与数组元素、路径不存在静默 |
| `TestUpdateJsonPatch` | 7 | 递归合并、数组整体替换、`null` 删键、嵌套布尔保真 |
| `TestUpdateJsonIfnull` | 8 | 默认静默无效、`{}` / `[]` 兜底、常量字面量无绑定参数、非法值拒绝 |
| `TestUpdateJsonSqlShape` | 8 | 各方法 SQL 形态、多路径合并为单 SET、参数与占位符顺序、`raw()` 内联、同列冲突拒绝 |
| `TestUpdateJsonSafety` | 11 | 列名/路径/ifnull 三重校验、值不被内联、无条件更新拒绝 |
| `TestUpdateJsonIntegration` | 8 | 缓存失效、事务回滚与提交、`fetch_sql` 不落库、**交错写入不丢更新**与读改写反例、`raw()` 算术自增 |
| `TestModelUpdateJson` | 8 | 模型层四种方法的 `where` 写法（dict / 主键 / 闭包）与缺失 where 拒绝 |
| `TestJsonWriteDriverContract` | 10 | 基类写入扩展点抛错、`json_bind` 包装规则、SQLite 表达式形态、模式白名单、异类操作左嵌套 |

---

## 9. 后续可做

1. **`where_json_where` 子集匹配** —— 用 `json_each` 逐键比对实现对象子集查询；
2. **JSON 表达式索引助手** —— 生成 `CREATE INDEX ... ON t(json_extract(c,'$.k'))` 的 DDL 辅助方法；
3. **多操作原子写入** —— 当前每个终端方法只产生一种操作；若需要"同列
   `set` + `remove` 一条语句完成"，可加一个接受操作列表的入口
   （Builder 的左嵌套逻辑已就绪，见 §5.4）；
4. **`is null` / `exists` 的写入侧对应** —— 例如"仅当路径不存在时删除"；
5. **MySQL / PostgreSQL 驱动落地** —— 按 §4.2 / §4.3 的骨架实现并补齐
   `_LAZY_DRIVERS` 注册。

> v0.5.0 列在"后续可做"里的 `json_set` / `json_patch` 写入助手已在
> v0.6.0 落地（§5.4）。
