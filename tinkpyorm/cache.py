"""查询缓存（进程内，标准库实现）。

设计要点：

1. **进程级共享** —— 存储挂在模块级单例上，而非 ``Query`` 实例。
   这样 ``Db.name('t').where(...).cache(10).find()`` 这类"每次新建
   Query"的常见写法才能真正命中缓存。
2. **TTL 语义** —— ``cache(10)`` 表示结果在 10 秒内有效，之后自动失效。
3. **容量上限** —— 默认最多保留 :data:`DEFAULT_MAX_ITEMS` 个条目，
   超出按 LRU 淘汰，避免长时间运行后无限增长。
4. **线程安全** —— 内部使用 ``RLock``，可供多线程共享。
5. **可替换后端** —— :class:`CacheStore` 定义接口，可通过
   :func:`set_store` 注入自定义实现（如文件缓存、Redis 等）。

键的格式为 ``{database}\\x00{table}\\x00{hash}``，其中 ``hash`` 由 SQL 与
绑定参数摘要而来。分层的明文前缀使写操作可以按"库 + 表"精确失效。
"""
from __future__ import annotations

import hashlib
import threading
import time
from collections import OrderedDict
from typing import Any, Dict, List, Optional, Tuple

__all__ = [
    "CacheStore", "MemoryCacheStore",
    "DEFAULT_MAX_ITEMS", "SEPARATOR",
    "store", "set_store", "clear", "make_key", "prefix_for", "stats",
]

#: 默认条目上限（超出按 LRU 淘汰）
DEFAULT_MAX_ITEMS = 500

#: 键分隔符（用不可见控制字符，避免与库名/表名中的普通字符冲突）
SEPARATOR = "\x00"


class CacheStore:
    """缓存后端接口。

    自定义后端只需实现这四个方法；``get`` 约定返回 ``(是否命中, 值)``
    二元组，以便区分"未命中"与"命中但值为 None"。
    """

    def get(self, key: str) -> Tuple[bool, Any]:
        raise NotImplementedError

    def set(self, key: str, value: Any, ttl: Optional[float] = None) -> None:
        raise NotImplementedError

    def delete(self, key: str) -> bool:
        raise NotImplementedError

    def clear(self, prefix: Optional[str] = None) -> int:
        """清空缓存；给出 ``prefix`` 时仅清除该前缀下的条目，返回清除条数。"""
        raise NotImplementedError

    def stats(self) -> Dict[str, Any]:
        """返回统计信息（命中率等），供调试使用。"""
        return {}


class MemoryCacheStore(CacheStore):
    """进程内内存缓存（``OrderedDict`` + TTL + LRU）。"""

    def __init__(self, max_items: int = DEFAULT_MAX_ITEMS) -> None:
        self.max_items = max_items
        self._data: "OrderedDict[str, Tuple[Optional[float], Any]]" = OrderedDict()
        self._lock = threading.RLock()
        self._hits = 0
        self._misses = 0

    # ------------------------------------------------------------------ #
    # 读写
    # ------------------------------------------------------------------ #
    def get(self, key: str) -> Tuple[bool, Any]:
        with self._lock:
            item = self._data.get(key)
            if item is None:
                self._misses += 1
                return False, None
            expire_at, value = item
            if expire_at is not None and time.time() >= expire_at:
                del self._data[key]
                self._misses += 1
                return False, None
            self._data.move_to_end(key)  # LRU：命中即置为最近使用
            self._hits += 1
            return True, value

    def set(self, key: str, value: Any, ttl: Optional[float] = None) -> None:
        expire_at = None
        if ttl is not None and ttl > 0:
            expire_at = time.time() + ttl
        with self._lock:
            if key in self._data:
                del self._data[key]
            self._data[key] = (expire_at, value)
            while self.max_items and len(self._data) > self.max_items:
                self._data.popitem(last=False)

    def delete(self, key: str) -> bool:
        with self._lock:
            return self._data.pop(key, None) is not None

    def clear(self, prefix: Optional[str] = None) -> int:
        with self._lock:
            if not prefix:
                n = len(self._data)
                self._data.clear()
                return n
            victims = [k for k in self._data if k.startswith(prefix)]
            for k in victims:
                del self._data[k]
            return len(victims)

    # ------------------------------------------------------------------ #
    # 维护
    # ------------------------------------------------------------------ #
    def purge(self) -> int:
        """清理所有已过期条目，返回清理条数。"""
        now = time.time()
        with self._lock:
            victims = [k for k, (exp, _) in self._data.items()
                       if exp is not None and now >= exp]
            for k in victims:
                del self._data[k]
            return len(victims)

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            total = self._hits + self._misses
            return {
                "backend": "memory",
                "items": len(self._data),
                "max_items": self.max_items,
                "hits": self._hits,
                "misses": self._misses,
                "hit_rate": (self._hits / total) if total else 0.0,
            }

    def __len__(self) -> int:
        return len(self._data)

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<MemoryCacheStore items={len(self._data)}/{self.max_items}>"


# ---------------------------------------------------------------------- #
# 全局单例
# ---------------------------------------------------------------------- #
_store: CacheStore = MemoryCacheStore()
_store_lock = threading.RLock()


def store() -> CacheStore:
    """返回当前缓存后端。"""
    return _store


def set_store(new_store: Optional[CacheStore]) -> CacheStore:
    """替换缓存后端，返回被替换掉的旧后端（便于用完后恢复）。

    传 ``None`` 表示恢复默认内存后端::

        from tinkpyorm import cache

        old = cache.set_store(MyRedisStore())   # 接入自定义后端
        ...
        cache.set_store(old)                    # 恢复原来的后端
    """
    global _store
    with _store_lock:
        old = _store
        _store = new_store if new_store is not None else MemoryCacheStore()
        return old


def clear(prefix: Optional[str] = None) -> int:
    """清空缓存（或按前缀清理），返回清理条数。"""
    return _store.clear(prefix)


def stats() -> Dict[str, Any]:
    """当前缓存统计信息。"""
    return _store.stats()


# ---------------------------------------------------------------------- #
# 键构造
# ---------------------------------------------------------------------- #
def prefix_for(database: Optional[str], table: Optional[str]) -> str:
    """构造"库 + 表"层级前缀，用于写操作后的精确失效。"""
    return f"{database or ''}{SEPARATOR}{table or ''}{SEPARATOR}"


def make_key(database: Optional[str], table: Optional[str],
             sql: str, params: Any = None) -> str:
    """由「库 + 表 + SQL + 绑定参数」生成缓存键（摘要部分为 MD5）。"""
    raw = f"{sql}\x1f{params!r}"
    digest = hashlib.md5(raw.encode("utf-8", "replace")).hexdigest()
    return prefix_for(database, table) + digest
