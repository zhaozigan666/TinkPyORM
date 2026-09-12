# JOIN 与 UNION

> [TinkPyORM](https://github.com/zhaozigan666/TinkPyORM) 技术文档 · [文档索引](Home.md) · 上一页： [03-聚合分组排序分页](03-聚合分组排序分页.md) · 下一页： [05-原生表达式](05-原生表达式.md)


```python
# 闭包 ON 条件（推荐，自动 AND 连接）
Db.table("user u") \
  .join("article a", lambda q: q.where_column("a.user_id", "=", "u.id")) \
  .field("u.name", "a.title") \
  .select()

# 也支持 left / right / inner
Db.table("user u").left_join("article a", lambda q: q.where_column("a.user_id", "=", "u.id")).select()
Db.table("user u").right_join("profile p", lambda q: q.where_column("p.user_id", "=", "u.id")).select()
Db.table("user u").inner_join("role r", lambda q: q.where_column("r.id", "=", "u.role_id")).select()

# UNION（去重）/ UNION ALL（不去重）
Db.table("user").field("age").union(
    lambda q: q.table("user").field("age").where("age", 20)
).select()

Db.table("user").field("age").union_all(
    lambda q: q.table("user").field("age").where("age", 20)
).select()
```
