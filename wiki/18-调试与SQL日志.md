# 调试与 SQL 日志

> [TinkPyORM](https://github.com/zhaozigan666/TinkPyORM) 技术文档 · [文档索引](Home.md) · 上一页： [17-camelCase别名](17-camelCase别名.md) · 下一页： [19-查询缓存](19-查询缓存.md)


```python
# 只返回 SQL 字符串、不执行（参数已内联，可直接复制到 SQLite 客户端调试）
sql = Db.table("user").where("id", 1).fetch_sql(True).select()
print(sql)
# SELECT * FROM `user` WHERE `id` = 1

# 最后一条执行过的 SQL
Db.table("user").where("id", 1).select()
Db.get_last_sql()

# SQL 日志（[(sql, params), ...]）
Db.get_sql_log()
Db.sql_log_clear()

# 日志默认只保留最近 1000 条（环形裁剪），避免长驻进程内存无限增长。
# 生产环境可整体关闭；需要完整日志时设为 None（不限制）。
Db.sql_log_disable()          # 关闭并清空
Db.sql_log_enable()           # 重新开启，上限 1000 条
Db.sql_log_enable(max_size=5000)
Db.set_config({"database": "app.db", "sql_log_max": None})  # 不限制

# 查询分析
Db.table("user").where("age", ">", 18).explain()
```

**渲染完整 SQL（含实际参数值）**，便于复制到 SQLite 客户端调试：

```python
q = Db.table("user").where("id", 1)
q.conn_render()      # 返回带真实参数值的 SQL 字符串（仅调试用，勿直接执行）
```
