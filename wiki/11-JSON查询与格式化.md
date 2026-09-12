# JSON 查询与格式化

> [TinkPyORM](https://github.com/zhaozigan666/TinkPyORM) 技术文档 · [文档索引](Home.md) · 上一页： [10-类型转换](10-类型转换.md) · 下一页： [12-JSON路径写入](12-JSON路径写入.md)


SQLite 自 3.38 起内置 JSON1 函数，TinkPyORM 在其上封装了"写入自动序列化 →
路径条件查询 → 结果自动格式化"的完整闭环。开启方式只需一个 `json()`。

## 写入：Python 对象自动序列化


```python
Db.table('user').insert({
    'name': '张三',
    'extra': {'age': 18, 'city': '北京', 'tags': ['vip', 'new']},
})
# 落库为：'{"age": 18, "city": "北京", "tags": ["vip", "new"]}'

Db.table('user').where('id', 1).update({'extra': {'age': 19}})   # 同样生效
Db.table('user').insert_all([{...}, {...}])                      # 批量同样生效
```

`dict` / `list` 值自动编码为 JSON 文本，无需手动 `json.dumps`；
其余类型（含 `None`、数字、普通字符串）原样绑定，不受影响。

## 读取：自动格式化为 Python dict


```python
Db.table('user').json().find(1)          # {'id': 1, 'extra': {'age': 18, ...}}
Db.table('user').json(['extra']).select()  # 指定字段，零嗅探开销（推荐）
Db.table('user').json(False).select()      # 显式关闭
```

| 写法 | 行为 |
|---|---|
| `json()` / `json(True)` | 自动嗅探：字形如 JSON 的字符串字段一律解码 |
| `json(['extra', 'meta'])` | 只解码指定字段（推荐生产环境使用） |
| `json(False)` | 关闭解码（便于链式切换） |

解码结果同样是 `dict` 的路径覆盖 `find / select / value / column /
chunk / cursor / paginate`，以及模型属性访问与 `to_dict()`。

模型只需声明字段，查询时自动接入：

```python
class User(Model):
    __table__ = 'user'
    __json__ = ['extra']        # 或 __type__ = {'extra': 'json'}

User.find(1).extra              # {'age': 18, 'city': '北京', ...}
User.find(1).to_dict()['extra'] # dict，不是 JSON 文本
```

## 路径条件查询


```python
# 显式写法：字段 + 路径 + 运算符 + 值
Db.table('user').where_json('extra', '$.age', '>', 18).select()
Db.table('user').where_json('extra', '$.level', 'in', [2, 3]).select()
Db.table('user').where_json('extra', '$.name', 'like', '%张%').select()
Db.table('user').where_json('extra', '$.age', 'between', [18, 30]).select()

# 简写：省略运算符时视为"等于"
Db.table('user').where_json('extra', '$.city', '北京').select()
Db.table('user').where_json('extra', '$.age', 18).select()
Db.table('user').where_json('extra', '$.deleted', False).select()   # JSON 布尔
Db.table('user').where_json('extra', '$.note', None).select()       # → IS NULL

# OR 连接
Db.table('user').where_json_or('extra', '$.city', '上海').select()
```

语义化方法：

| 方法 | 语义 |
|---|---|
| `where_json_exists(f, p)` | 路径**存在**（值为 JSON `null` 也算存在） |
| `where_json_not_exists(f, p)` | 路径不存在 |
| `where_json_null(f, p)` | 路径值**为 null 或路径不存在** |
| `where_json_not_null(f, p)` | 路径值非 null |
| `where_json_contains(f, p, v)` | JSON 数组包含标量元素 `v` |
| `where_json_not_contains(f, p, v)` | JSON 数组不包含 `v` |
| `where_json_length(f, p, op, n)` | 数组 / 对象元素个数比较（`where_json_length(f, p, 3)` 即等于 3） |
| `where_json_type(f, p, type)` | 路径值类型（`array` / `object` / `integer` / `text` / `null` / `true` …） |

> `is null` 与 `exists` 的区别：JSON 中"键不存在"与"键值为 `null`"是两件事。
> `json_extract` 对两者都返回 SQL NULL，所以 `is null` 会同时命中；
> 需要严格判断键是否存在时用 `where_json_exists`。

路径写法很宽松，`'$.user.name'`、`'user.name'`、`'[0].id'` 均可，
统一归一化为 `$` 开头；支持嵌套、数组下标与带引号的键（`$."带空格的键"`）。

## 选取与排序 JSON 路径


```python
# 选取路径值作为字段（别名按路径末段自动生成，结果自动解码）
Db.table('user').field_json('extra', '$.city').select()
# SELECT *, json_extract(`extra`, '$.city') AS `city` FROM `user`

Db.table('user').field('name').field_json('extra', '$.city').select()
# → [{'name': '张三', 'city': '北京'}, ...]

# 按路径值排序
Db.table('user').order_json('extra', '$.score', 'desc').select()
```

`field_json` 走独立通道，与 `field()` 的调用顺序无关（不会被覆盖）；
生成的别名字段自动纳入解码列表，无需再调用 `json()`。
