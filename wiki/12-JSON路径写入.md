# JSON 路径写入

> [TinkPyORM](https://github.com/zhaozigan666/TinkPyORM) 技术文档 · [文档索引](Home.md) · 上一页： [11-JSON查询与格式化](11-JSON查询与格式化.md) · 下一页： [13-软删除与查询范围](13-软删除与查询范围.md)

> 查询侧（对象自动序列化 / 结果解码为 dict / 路径条件查询 / 路径选取与排序）见 [JSON 查询与格式化](11-JSON查询与格式化.md)。

## 路径级局部更新


写入侧与条件查询对称，同样在 SQL 内改写嵌套字段：

```python
# 单路径：只改 $.age，其余键不动
Db.table('user').where('id', 1).update_json('extra', '$.age', 19)

# 多路径：压进一条语句原子写入（推荐，避免多次往返）
Db.table('user').where('id', 1).update_json(
    'extra', {'$.age': 19, '$.city': '上海'})

# 仅当路径不存在时写入（不覆盖已有值，适合初始化默认字段）
Db.table('user').where('id', 1).update_json_insert('extra', '$.level', 'normal')

# 删除路径（可传多个）
Db.table('user').where('id', 1).update_json_remove('extra', ['$.tmp', '$.cache'])

# RFC 7396 合并补丁：对象递归合并、数组整体替换、null 表示删除键
Db.table('user').where('id', 1).update_json_patch(
    'extra', {'level': 'gold', 'tmp': None})

# 与 JSON 条件组合：给满足条件的记录打标
Db.table('user').where_json('extra', '$.age', '>', 30).update_json(
    'extra', '$.level', 'senior')
```

| 方法 | 语义 | 底层函数 |
|---|---|---|
| `update_json` | 存在则覆盖、不存在则新增 | `json_set` |
| `update_json_insert` | 仅当路径不存在时写入 | `json_insert` |
| `update_json_remove` | 删除路径（可多个） | `json_remove` |
| `update_json_patch` | RFC 7396 合并补丁 | `json_patch` |
| `update_json_ops` | 一条语句内混合多种操作 | 按序折叠成一次调用 |

### 一条语句内混合多种操作

上面每个方法一次只表达一种操作；`update_json_ops()` 接受操作列表，把多种
操作编译进**同一条** UPDATE，按列表顺序依次生效：

```python
Db.table('user').where('id', 1).update_json_ops('extra', [
    ('set',    '$.age', 19),        # 改值
    ('remove', '$.tmp'),            # 删键
    ('insert', '$.level', 'vip'),   # 不存在才写
    ('patch',  {'meta': {'v': 2}}), # RFC 7396 合并
    ('remove', ['$.a', '$.b']),     # 一次删多个
])
# UPDATE `user` SET `extra` = json_insert(json_patch(json_set(
#   json_remove(json_set(`extra`, '$.age', ?), '$.tmp'), '$.level', ?),
#   json(?)), '$.a', ...) WHERE `id` = 1
```

操作元素支持五种写法：

| 写法 | 适用模式 | 说明 |
|---|---|---|
| `(模式, 路径, 值)` | `set` / `insert` | 通用三元组 |
| `(模式, 路径)` | `remove` | 删除无需值；路径传 `list` 可一次删多个 |
| `(模式, 补丁文档)` | `patch` | 补丁自带键路径，无路径元素 |
| `{路径: 值}` | 等价 `set` | 多路径简写 |
| `JsonUpdate(...)` | 任意 | 高级用法，自带列名 → 一条语句改多个 JSON 列 |

顺序是**确定的**：列表顺序即应用顺序。

```python
q.update_json_ops('extra', [('set', '$.a', 1), ('remove', '$.a')])  # $.a 被删
q.update_json_ops('extra', [('remove', '$.a'), ('set', '$.a', 1)])  # $.a == 1
```

相邻的同类操作会被合并进同一次函数调用（`json_set(col, p1, v1, p2, v2)`）。
这不是纯粹的性能优化，而是**正确性要求**：SQL 中同一列出现多个 SET 子句时
只有最后一个生效，拆开会导致静默丢更新。合并不改变语义——SQLite 对同一次
调用内的重复路径同样是后者胜（`json_insert` 则是首个生效）。

> **值表达式的求值基准**：`raw()` 表达式引用的是**列本身**
> （如 `json_extract(\`extra\`, '$.n')`），因此同一条语句内的多次写入看到的
> 都是同一个原始列值——即使 ORM 把它们嵌套成
> `json_set(json_set(col, ...), ...)` 也一样。下面两次自增只净增 1：
>
> ```python
> expr = raw("json_extract(`extra`, '$.n') + 1")
> q.update_json_ops('extra', [('set', '$.n', expr), ('set', '$.n', expr)])  # n += 1
> ```
>
> 要链式引用上一步的结果，必须拆成多条语句（每条独立 UPDATE 才能读到上一次
> 的落库值）：
>
> ```python
> q.update_json_ops('extra', [('set', '$.n', expr)])
> q.update_json_ops('extra', [('set', '$.n', expr)])   # n += 2
> ```
>
> 这是 SQL 表达式的固有性质，不是框架限制。**常量**写入不受影响，
> 顺序语义严格成立。

用 `JsonUpdate` 自带列名，可在一条语句内改多个 JSON 列：

```python
from tinkpyorm import JsonUpdate
Db.table('user').where('id', 1).update_json_ops('extra', [
    JsonUpdate('extra', '$.x', 1, 'set'),
    JsonUpdate('meta',  '$.m', 2, 'set'),
])
```

模型层有同名方法：`User.update_json_ops('extra', [...], where={'id': 1})`。

值为 Python 容器时按 JSON 结构写入（`{'job': 'dev'}` 落库为嵌套对象，
不会被转义成字符串）；`True` / `False` 写入 JSON `true` / `false`，类型保真。

**为什么不"读出 → 改 → 写回"**：那条路存在读改写窗口，两个交错执行的
更新会互相覆盖（先写者的改动丢失）：

```python
# 读改写：a 的改动被 b 的整列写回覆盖
a = Db.table('doc').json(['val']).find(1)['val']
b = Db.table('doc').json(['val']).find(1)['val']
a['a'] = 1; Db.table('doc').where('id', 1).update({'val': a})
b['b'] = 1; Db.table('doc').where('id', 1).update({'val': b})
# 结果：{'b': 1}        ← a 丢了
```

`update_json` 在 SQL 内完成改写，同样时序下两个键都保留 → `{'a': 1, 'b': 1}`。
配合 `raw()` 还能做 SQL 内算术自增（两次自增都保留，不会退化成 1）：

```python
from tinkpyorm import raw
expr = raw("json_extract(`val`, '$.n') + 1")
Db.table('cnt').where('id', 1).update_json('val', '$.n', expr)
```

几点须知：

1. **必须有 where 条件**：无条件路径更新会改写全表，与 `update()` 一样
   直接拒绝（`QueryError`）。
2. **列为 NULL 时默认静默无效**：`json_set(NULL, ...)` 返回 NULL。传
   `ifnull={}`（或 `[]`）可让空列先视为空文档：
   `update_json('extra', '$.a', 1, ifnull={})`。
3. **`null` 的两种相反含义**：`update_json(..., None)` 是"置为 JSON null"，
   而 `update_json_patch(..., {'k': None})` 是"删除键 k"。
4. **写入量不变**：JSON 列以文本存储，`json_set` 同样重写整列文本，写入
   放大与读改写一致。收益在少一次往返、省 Python 侧解析、并发正确性。
5. 与 `update()` 一样是**终端方法**（立即执行并返回影响行数），不能像
   `where_json()` 那样继续链式拼接。
6. **操作列表整体校验**：`update_json_ops()` 先解析全部元素，任一元素非法
   即抛 `InvalidArgumentException`，此时不执行任何写入，也不在查询构造器上
   留下半成品状态。

模型层可用同名方法，`where` 写法与 `Model.update` 一致：

```python
User.update_json('extra', '$.age', 19, where={'id': 1})
User.update_json_remove('extra', '$.tmp', where=lambda q: q.where('id', 1))
```

## 安全性


- **路径**：经白名单校验后才内联为 SQL 字面量，非法路径（`$.a' OR '1'='1`）
  立即抛 `InvalidArgumentException`。
- **列名 / 别名**：校验为合法标识符，杜绝 `field_json(..., 'x; DROP TABLE')`
  这类拼接注入。
- **值**：一律参数绑定，与其它条件查询同一套机制。
- **写入侧同一套闸口**：`update_json` 族的列名、路径走相同的白名单校验，
  值全部参数绑定（容器与布尔以 JSON 文本绑定，再由 `json(?)` 解析）；
  `ifnull` 只接受空 `dict` / 空 `list`，由驱动编译为**常量字面量**
  （`'{}'` / `'[]'`），不接受任意字面量。
- **同列多 SET 防护**：同一列的路径写入被合并进一次函数调用——SQL 中同列
  出现多个 SET 子句时只有最后一个生效。若同一字段同时出现在普通更新数据
  与路径写入中，直接抛 `QueryError` 而不是静默丢更新。

## 多数据库扩展


JSON 条件的 SQL 形态由**驱动**提供，Query / Builder 层不含任何方言知识。
新增数据库只需继承对应基类并覆写 JSON 方法：

| 能力 | SQLite（已实现） | MySQL（预留） | MongoDB / Redis（预留） |
|---|---|---|---|
| 能力开关 | `supports_json = True` | 同 SQLite | 同（`NoSQLDriver` 已内置） |
| 路径取值 | `json_extract` | `JSON_EXTRACT` / `->>` | 原生 dict 查询 |
| 路径存在 | `json_type(...) IS NOT NULL` | `JSON_CONTAINS_PATH` | 原生 |
| 数组包含 | `json_each` + EXISTS | `JSON_CONTAINS` | 原生 |
| 元素个数 | `json_each` 计数 | `JSON_LENGTH` | 原生 |
| 类型判断 | `json_type` | `JSON_TYPE` | 原生 |
| 路径写入 | `json_set` / `json_insert` | `JSON_SET` / `JSON_INSERT` | `$set` 更新算子 |
| 路径删除 | `json_remove` | `JSON_REMOVE` | `$unset` 更新算子 |
| 文档合并 | `json_patch` | `JSON_MERGE_PATCH` | `$merge` / 客户端合并 |
| JSON 类型绑定 | `json(?)` | `CAST(? AS JSON)` | 原生对象 |
| 值解码 | JSON 文本 → dict | 同 SQLite | **恒等透传**（已是 Python 对象） |

MongoDB / Redis 走 `NoSQLDriver`，`json_decode` / `json_encode` 为恒等函数，
因此上层"结果自动格式化为 dict"的代码路径**无需任何分支**即可复用。
`supports_json = False` 的驱动调用 JSON API 会抛出 `UnsupportedOperation`，
而不是静默生成错误 SQL。

详见 [`docs/json-query.md`](../docs/json-query.md)。

## 注意事项


1. **复合值不做相等比较**：`where_json('extra', '$.tags', '=', ['vip'])`
   会抛 `QueryError`。JSON 文本比较受键顺序与空白格式影响，结果不可靠；
   数组元素匹配请用 `where_json_contains`，复杂结构请用
   `where_raw` 配合 `json_extract`。
2. **自动嗅探的误判**：`json()` 无参会把形如 `'[1,2]'`、`'"text"'` 的普通
   字符串一并解析。字段语义固定时请用 `json(['extra'])` 显式声明。
3. **布尔在条件中的映射**：`where_json(..., True)` 会转成 `= 1` 比较——
   SQLite 的 `json_extract` 对 JSON `true` 返回整数 1，故能正确命中。
   写入侧（`insert` / `update` 的整列赋值与 `update_json` 族）一律落库为
   JSON `true` / `false`，类型保真。
4. **路径写入列需为 NULL 时**：见「路径级局部更新」须知 2（`ifnull`）。
