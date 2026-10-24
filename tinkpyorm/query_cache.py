"""查询缓存关注点（v0.13.2 自 Query 拆出，审查报告 K4）。

:class:`QueryCacheMixin` 承载 Query 的全部查询缓存逻辑：缓存配置解析、
缓存键生成、写入（含事务内不写的幻影读防护）与写后失效。``Query``
混入本类后只保留"链式构造 + 委托 Builder/连接执行"的职责，缓存语义
可独立阅读与测试。

方法间约定（对 Query 的依赖面，仅三处）：
* ``self.options``——链式构造的选项字典（``cache`` / ``table`` 键）；
* ``self.conn``——构造时绑定的连接（可为 ``None``）；
* ``self._resolve_conn()``——Query 侧的连接解析（含默认连接回退）。
"""
from __future__ import annotations

from typing import Any, Optional, Sequence, Tuple

from . import cache as _cache

__all__ = ["QueryCacheMixin"]


class QueryCacheMixin:
    """Query 的查询缓存混入（详见模块 docstring）。"""

    def _cache_spec(self) -> Tuple[Optional[str], float]:
        """解析缓存配置，返回 ``(键, TTL 秒)``。

        键为 ``None`` 表示未启用缓存（或 TTL <= 0，即不缓存）；
        返回空串表示"按 SQL 与参数自动生成键"。
        """
        spec = self.options.get("cache")
        if not spec:
            return None, 0.0
        key, ttl = spec
        ttl = float(ttl or 0)
        if ttl <= 0:
            return None, 0.0
        if isinstance(key, str) and key:
            return key, ttl
        return "", ttl

    def _cache_table(self) -> str:
        """缓存键使用的表名（取首个表，忽略别名）。"""
        tables = self.options.get("table") or []
        if tables and isinstance(tables[0], dict):
            return tables[0].get("name") or ""
        return ""

    def _cache_database(self) -> str:
        """缓存键使用的库标识（未绑定连接时为空串）。"""
        if self.conn is None:
            return ""
        return getattr(self.conn.config, "database", "") or ""

    def _resolve_cache_key(self, sql: str, params: Sequence[Any]) -> Tuple[Optional[str], float]:
        """生成最终缓存键。

        自定义键同样带上"库 + 表"前缀，使写操作能按前缀统一失效。
        """
        key, ttl = self._cache_spec()
        if key is None:
            return None, 0.0
        if key == "":
            return _cache.make_key(self._cache_database(),
                                   self._cache_table(), sql, params), ttl
        prefix = _cache.prefix_for(self._cache_database(), self._cache_table())
        return prefix + "user:" + key, ttl

    def _cache_set(self, key: str, value: Any, ttl: float) -> None:
        """写入查询缓存。

        **事务进行中不写**：事务内的读取看到的是未提交状态，一旦回滚，
        缓存里就会留下从未存在过的行（幻影读），且在 TTL 内无人清理。
        命中侧不受影响——缓存中的条目永远来自已提交的快照。
        """
        conn = self._resolve_conn()
        if getattr(conn, "_in_transaction", False):
            return
        _cache.store().set(key, value, ttl)

    def _invalidate_cache(self) -> int:
        """写操作后清除同一库同一表的全部缓存，返回清除条数。

        同时清理依赖本表的全文检索缓存：FTS 表由内容表的触发器写入，
        不经过 Query，自身无法感知这次写入。
        """
        prefix = _cache.prefix_for(self._cache_database(), self._cache_table())
        cleared = _cache.clear(prefix)
        from .fts import invalidate_dependents
        cleared += invalidate_dependents(self._cache_database(),
                                         self._cache_table())
        return cleared
