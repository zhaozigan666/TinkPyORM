# 与 think-orm 对照表

> [TinkPyORM](https://github.com/zhaozigan666/TinkPyORM) 技术文档 · [文档索引](Home.md) · 上一页： [22-API速查表](22-API速查表.md) · 下一页： [24-注意事项与已知限制](24-注意事项与已知限制.md)


| think-orm (PHP) | TinkPyORM | 备注 |
|---|---|---|
| `Db::name('user')` | `Db.name("user")` | 一致 |
| `Db::table('user')` | `Db.table("user")` | 一致 |
| `->where('id', 1)` | `.where("id", 1)` | 一致 |
| `->whereIn('id', [1,2])` | `.where_in("id", [1,2])` 或 `.whereIn(...)` | snake_case + camelCase 双支持 |
| `->field('id,name')` | `.field("id,name")` | 一致 |
| `->order('id', 'desc')` | `.order("id", "desc")` | 一致 |
| `->limit(10)` / `->page(1,15)` | `.limit(10)` / `.page(1, 15)` | 一致 |
| `->select()` / `->find()` | `.select()` / `.find()` | 一致 |
| `->count()` / `->sum()` | `.count()` / `.sum()` | 一致 |
| `->insert($data)` | `.insert(data)` | 一致，返回自增 id |
| `->insertAll($list)` | `.insert_all(list)` | 一致 |
| `->update($data)` | `.update(data)` | 一致 |
| `->delete()` | `.delete()` | 一致 |
| `->inc('f', 1)` / `->dec()` | `.inc("f", 1)` / `.dec()` | 一致 |
| `Model::create($data)` | `Model.create(data)` | 一致 |
| `Model::destroy($id)` | `Model.destroy(id)` | 一致 |
| `->with('articles')` | `.with_("articles")` | ⚠️ `with` 是 Python 关键字，加下划线 |
| `->withTrashed()` | `.with_trashed()` / `.withTrashed()` | 一致 |
| `Db::raw('...')` | `raw("...")` / `Db.raw("...")` | 一致 |
| `Db::transaction(fn)` | `Db.transaction(fn)` / `with Db.transaction()` | 一致 + 上下文模式 |
| `getXxxAttr()` | `get_xxx_attr()` | 命名风格随语言 |
| `scopeXxx()` | `scope_xxx()` | 命名风格随语言 |
| `->paginate(15)` | `.paginate(15)` | 一致 |
| `->fetchSql()` | `.fetch_sql(True)` | Python 无 getter/setter 合一惯例 |
| `->failException()` | `.fail_exception()` | 一致 |
| `->json(['tags'])` | `.json(["tags"])` 或 `__json__` | 一致 |
