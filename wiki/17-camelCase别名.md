# camelCase 别名

> [TinkPyORM](https://github.com/zhaozigan666/TinkPyORM) 技术文档 · [文档索引](Home.md) · 上一页： [16-结果集与分页](16-结果集与分页.md) · 下一页： [18-调试与SQL日志](18-调试与SQL日志.md)


为照顾 PHP / JavaScript 开发者习惯，所有 `snake_case` 方法都自动支持 `camelCase` 写法：

```python
User.whereIn("id", [1, 2]).select()          # → where_in
User.whereNotIn("id", [1, 2]).select()       # → where_not_in
User.whereNotNull("email").select()          # → where_not_null
User.whereBetween("age", [20, 30]).select()  # → where_between
User.findOrEmpty(999)                        # → find_or_empty
User.findOrFail(999)                         # → find_or_fail
User.selectOrFail()                          # → select_or_fail
User.withTrashed().select()                  # → with_trashed（需启用软删除）
User.onlyTrashed().select()                  # → only_trashed（需启用软删除）
Db.table("user").fieldRaw("COUNT(*)").select()   # → field_raw
Db.table("user").unionAll(...).select()          # → union_all
```

> 两种写法完全等价，团队内统一即可。**注意**：映射目标是 `snake_case` 的**实际方法名**，例如 `Query` 上只有 `order`（没有 `order_by`），因此 `orderBy` 不可用；`withTrashed` / `onlyTrashed` 要求模型已配置 `__soft_delete__`。
