"""Redis 数据访问层（v0.13.0）。

本模块是 Redis 的**服务层**：驱动层（:mod:`tinkpyorm.drivers.redis`）
只负责连接、版本门禁与异常翻译；键空间访问、输入校验、命名空间前缀、
流水线/发布订阅封装都在这里。

与项目内既有风格的一致性
========================

* **命名空间即集合**：MongoDB 用 ``Db.mongo("users")`` 打开集合，Redis 没有
  集合概念，最接近的是**键前缀**，因此 ``Db.redis("user:")`` 打开的是
  "以 ``user:`` 为前缀的键空间"；所有键方法都会自动补前缀。
* **终端方法直接返回结果**：Redis 的每个命令本身就是终端的（没有 SQL 那样的
  条件累积），所以本层不做链式状态，键名 + 参数即调用——与
  ``Query`` / ``MongoCollection`` 的链式风格刻意区分，避免"看起来能链式
  但语义不同"的假一致。
* **异常归口**：所有 ``redis.exceptions.*`` 在 :meth:`RedisStore._call`
  这一单一入口翻译为项目异常，本层只补充**输入校验**类错误。
* **能力门禁**：需要新版本/非集群形态的命令在调用前
  ``driver.require_feature(...)``，不支持的版本给出"需要 >= x"的明确报错，
  而不是等 Redis 回一个 ``ERR unknown command``。

基本用法::

    from tinkpyorm import Db

    Db.config({"type": "redis", "host": "127.0.0.1", "port": 6379,
               "password": "secret", "database": 0})

    cache = Db.redis("app:")                 # 命名空间前缀 app:
    cache.set("user:1", "张三", ex=60)        # 60 秒后过期
    cache.get("user:1")

    cache.hset("user:1:profile", {"name": "张三", "age": 25})
    cache.hgetall("user:1:profile")

    cache.set_json("user:1:tags", ["vip", "beta"])
    cache.get_json("user:1:tags")

    cache.zadd("rank", {"张三": 98.5, "李四": 91.0})
    cache.zrange("rank", 0, 9, withscores=True)      # 排行榜前 10

    with cache.pipeline() as pipe:                   # 单次往返批量执行
        pipe.incr("stats:visits").set("stats:last", "now")
        results = pipe.execute()

性能要点（详见 ``wiki/31-Redis支持.md``）：

* **用 :meth:`RedisStore.scan_iter` 遍历键，不要用 :meth:`RedisStore.keys`**
  ——``KEYS`` 是单线程阻塞命令，百万键会卡住整个实例；
* **批量操作用 :meth:`RedisStore.pipeline`**，一次网络往返执行 N 条命令，
  比循环调用快一个数量级（跨网络尤其明显）；
* **列表/集合/有序集合按需取范围**：``lrange(key, 0, 99)`` 而不是先
  ``llen`` 再全取；``smembers`` 只适合小集合，大集合用 ``sscan_iter``；
* **给键设过期时间**（``ex`` / ``expire``），Redis 是内存库，无 TTL 的键
  会一直占用内存直到淘汰策略介入；
* **集群模式下多键命令要求同一 slot**：用 ``{tag}`` 形式的键名
  （如 ``user:{1}:name``）把相关键锁到同一 slot。
"""
from __future__ import annotations

import json
from typing import Any, Dict, Iterator, List, Mapping, Optional, Sequence, Union

from .drivers.redis import translate_redis_error
from .exceptions import InvalidArgumentException, OrmError, QueryError
from .utils import UNSET

__all__ = ["RedisStore", "RedisPipeline", "RedisSubscription"]

#: 单次 SCAN 默认批量（太小往返多、太大阻塞主线程）。
DEFAULT_SCAN_COUNT = 500

#: ``keys()`` 的安全阈值：超过这个键数直接拒绝（防止误阻塞线上实例）。
SAFE_KEYS_LIMIT = 10_000

#: ``clear()`` 的安全护栏：不允许无模式地清库。
_UNSAFE_PATTERNS = ("", "*", "**")


def _check_key(key: Any) -> str:
    """校验键名（Redis 键是二进制安全的，但空键与非法类型必须拦下）。"""
    if isinstance(key, bytes):
        try:
            value = key.decode("utf-8")
        except UnicodeDecodeError:
            raise InvalidArgumentException(
                "键名是二进制字节，请改用 str（本层的命名空间前缀只支持文本键）")
    elif isinstance(key, str):
        value = key
    elif isinstance(key, bool):
        raise InvalidArgumentException(f"非法键名: {key!r}（布尔不是合法键）")
    elif isinstance(key, int):
        value = str(key)
    elif isinstance(key, float):
        value = repr(key)
    else:
        raise InvalidArgumentException(
            f"非法键名: {key!r}（支持 str / int / float / bytes）")
    if not value:
        raise InvalidArgumentException("键名不能为空字符串")
    return value


def _check_field(name: Any) -> Union[str, bytes]:
    """校验 hash / zset 的成员名（与键名同规则，但不做前缀处理）。"""
    if isinstance(name, (str, bytes)):
        if not name:
            raise InvalidArgumentException("成员名不能为空")
        return name
    if isinstance(name, bool):
        raise InvalidArgumentException(f"非法成员名: {name!r}")
    if isinstance(name, (int, float)):
        return str(name)
    raise InvalidArgumentException(
        f"非法成员名: {name!r}（支持 str / bytes / int / float）")


def _encode_value(value: Any) -> Union[str, bytes]:
    """把标量编码为可写入 Redis 的值。

    ``dict`` / ``list`` 等容器**明确拒绝**（不做隐式 JSON 序列化）：
    隐式编码会让 ``get()`` 的返回类型取决于写入路径，读代码的人无法判断
    取回来的是字符串还是对象。容器请用 :meth:`RedisStore.set_json`。
    """
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (str, bytes)):
        return value
    if isinstance(value, (int, float)):
        return repr(value) if isinstance(value, float) else str(value)
    if isinstance(value, (dict, list, tuple, set)):
        raise InvalidArgumentException(
            f"Redis 字符串值只支持标量，收到 {type(value).__name__}；"
            f"容器请用 set_json()/get_json()（整键 JSON）")
    if value is None:
        raise InvalidArgumentException(
            "Redis 不能存 None；如需表示空值请存空串，或用 delete() 移除键")
    raise InvalidArgumentException(
        f"不支持的值类型: {type(value).__name__}")


def _encode_mapping(mapping: Mapping[Any, Any]) -> Dict[Any, Any]:
    """编码 hash 的字段映射（值同样只允许标量）。"""
    if not isinstance(mapping, Mapping):
        raise InvalidArgumentException(
            f"hset 需要 dict/Mapping，收到: {type(mapping).__name__}")
    return {_check_field(k): _encode_value(v) for k, v in mapping.items()}


class RedisStore:
    """Redis 键空间的读写入口。

    实例由 :meth:`tinkpyorm.db.Db.redis` 创建。所有键名都会自动补上
    构造时给定的前缀（命名空间），``scan_iter`` / ``keys`` 返回的则是
    **去掉前缀后的逻辑键名**，便于业务代码直接使用。
    """

    def __init__(self, conn: Any, prefix: str = "",
                 decode_responses: Optional[bool] = None):
        driver = getattr(conn, "driver", None)
        if driver is not None and not getattr(driver, "supports_redis", False):
            from .drivers import UnsupportedOperation
            raise UnsupportedOperation(
                f"{getattr(driver, 'name', '当前')} 驱动不支持 Redis 键空间："
                f"该功能依赖真实 Redis 服务端（redis-py）；"
                f"SQL 数据库请用 Db.table() 链式查询")
        self.conn = conn
        self.prefix = str(prefix or "")
        self._decode_responses = decode_responses

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #
    @property
    def driver(self) -> Any:
        return self.conn.driver

    def _driver_client(self) -> Any:
        """底层 ``redis.Redis``（建连失败也会被 :meth:`_call` 翻译）。"""
        return self.driver.client()

    @staticmethod
    def _call(fn: Any, *args: Any, **kwargs: Any) -> Any:
        """执行一次 redis-py 调用，并把第三方异常翻译成项目异常。

        统一走这一个入口，避免逐个调用点漏翻译——漏一个就会让
        ``redis.exceptions.*`` 泄漏给调用方。
        """
        try:
            return fn(*args, **kwargs)
        except OrmError:
            raise
        except Exception as exc:
            raise translate_redis_error(exc) from exc

    def _exec(self, method: str, *args: Any, **kwargs: Any) -> Any:
        """在底层客户端上调用 ``method`` 并翻译异常（含建连异常）。"""
        client = self._call(self._driver_client)
        return self._call(getattr(client, method), *args, **kwargs)

    def key(self, name: Any) -> str:
        """返回补全前缀后的完整键名（调试/日志用）。"""
        return self.prefix + _check_key(name)

    def logical(self, full_key: Any) -> str:
        """去掉命名空间前缀，还原为逻辑键名。"""
        text = full_key.decode("utf-8") if isinstance(full_key, bytes) else str(full_key)
        if self.prefix and text.startswith(self.prefix):
            return text[len(self.prefix):]
        return text

    def client(self) -> Any:
        """底层 ``redis.Redis``（逃生舱，键名需自行处理前缀）。"""
        return self._driver_client()

    @property
    def decode_responses(self) -> bool:
        """是否把返回值解码为 str（由连接选项 ``decode_responses`` 决定）。"""
        return bool(self.driver.config.options.get("decode_responses", True))

    # ------------------------------------------------------------------ #
    # 连接 / 元信息
    # ------------------------------------------------------------------ #
    def ping(self) -> bool:
        """服务端是否可达。"""
        return bool(self._exec("ping"))

    def info(self, section: Optional[str] = None) -> Dict[str, Any]:
        """``INFO`` 的 dict 形态（``section`` 如 ``server`` / ``memory``）。"""
        return self._exec("info", section) if section else self._exec("info")

    def dbsize(self) -> int:
        """当前 db 的键总数（**不是**本命名空间的键数）。"""
        return int(self._exec("dbsize"))

    def config_get(self, pattern: str = "*") -> Dict[str, Any]:
        """读服务端配置（需 ACL 授权 ``+config|get``）。"""
        return self._exec("config_get", pattern)

    def server_version(self) -> str:
        """服务端版本字符串（探测值优先）。"""
        return str(self.driver.version)

    def topology(self) -> Optional[str]:
        """部署形态：``standalone`` / ``cluster`` / ``sentinel``。"""
        return getattr(self.driver, "topology", None)

    # ------------------------------------------------------------------ #
    # 字符串
    # ------------------------------------------------------------------ #
    def set(self, key: Any, value: Any, ex: Optional[int] = None,
            px: Optional[int] = None, exat: Optional[int] = None,
            pxat: Optional[int] = None, nx: bool = False, xx: bool = False,
            keepttl: bool = False, get: bool = False) -> Any:
        """写入字符串值。

        ``ex`` / ``px`` 为相对过期（秒 / 毫秒），``exat`` / ``pxat`` 为
        绝对过期（Unix 时间戳，需 Redis >= 6.2）；``keepttl`` 保留原 TTL
        （>= 6.0）；``get`` 返回旧值（>= 6.2）。``nx`` / ``xx`` 互斥。
        """
        if nx and xx:
            raise InvalidArgumentException("nx 与 xx 不能同时为真")
        if ex is not None and px is not None:
            raise InvalidArgumentException("ex 与 px 不能同时指定")
        if (exat is not None or pxat is not None):
            self.driver.require_feature("set_exat_pxat")
        if keepttl:
            self.driver.require_feature("set_keepttl")
        if get:
            self.driver.require_feature("set_get")
        return self._exec(
            "set", self.key(key), _encode_value(value), ex=ex, px=px,
            exat=exat, pxat=pxat, nx=nx, xx=xx, keepttl=keepttl, get=get)

    def setex(self, key: Any, seconds: int, value: Any) -> bool:
        """写入并设置过期秒数（``SETEX``）。"""
        return bool(self._exec("setex", self.key(key), int(seconds),
                               _encode_value(value)))

    def psetex(self, key: Any, milliseconds: int, value: Any) -> bool:
        """写入并设置过期毫秒数（``PSETEX``）。"""
        return bool(self._exec("psetex", self.key(key), int(milliseconds),
                               _encode_value(value)))

    def setnx(self, key: Any, value: Any) -> bool:
        """仅当键不存在时写入（分布式锁的原子原语）。"""
        return bool(self._exec("setnx", self.key(key), _encode_value(value)))

    def get(self, key: Any, default: Any = None) -> Any:
        """读字符串值；键不存在时返回 ``default``。"""
        value = self._exec("get", self.key(key))
        return default if value is None else value

    def getdel(self, key: Any) -> Any:
        """读并删除（>= 6.2，原子的"取走"语义）。"""
        self.driver.require_feature("getdel")
        return self._exec("getdel", self.key(key))

    def getex(self, key: Any, ex: Optional[int] = None,
              px: Optional[int] = None, persist: bool = False) -> Any:
        """读值并顺带改 TTL（>= 6.2）。"""
        self.driver.require_feature("getex")
        return self._exec("getex", self.key(key), ex=ex, px=px,
                          persist=persist)

    def mset(self, mapping: Mapping[Any, Any]) -> bool:
        """批量写入（单次往返；不做原子性保证之外的任何承诺）。"""
        if not isinstance(mapping, Mapping) or not mapping:
            raise InvalidArgumentException("mset 需要非空 dict/Mapping")
        pairs: Dict[str, Any] = {}
        for k, v in mapping.items():
            pairs[self.key(k)] = _encode_value(v)
        return bool(self._exec("mset", pairs))

    def mget(self, *keys: Any) -> List[Any]:
        """批量读取，缺失位置返回 ``None``。"""
        if not keys:
            raise InvalidArgumentException("mget 至少需要一个键")
        return list(self._exec("mget", [self.key(k) for k in keys]))

    def append(self, key: Any, value: Any) -> int:
        """追加到字符串尾部，返回新长度。"""
        return int(self._exec("append", self.key(key), _encode_value(value)))

    def strlen(self, key: Any) -> int:
        """字符串长度（键不存在返回 0）。"""
        return int(self._exec("strlen", self.key(key)))

    # ---- JSON（整键序列化，显式 API） ---- #
    def set_json(self, key: Any, value: Any, **kwargs: Any) -> Any:
        """把一个 Python 对象整体序列化为 JSON 写入（``set`` 的容器版）。

        额外关键字参数透传给 :meth:`set`（``ex`` / ``nx`` 等）。
        """
        try:
            text = json.dumps(value, ensure_ascii=False, default=str)
        except (TypeError, ValueError) as exc:
            raise InvalidArgumentException(f"无法序列化为 JSON: {exc}") from exc
        return self.set(key, text, **kwargs)

    def get_json(self, key: Any, default: Any = UNSET) -> Any:
        """读回 :meth:`set_json` 写入的对象；键不存在时返回 ``default``。

        显式传入 ``default=None`` 与"不传"不同：不传且键不存在时抛
        ``QueryError``，避免把"没有这个键"和"值就是 null"混为一谈。
        """
        value = self._exec("get", self.key(key))
        if value is None:
            if default is UNSET:
                raise QueryError(f"Redis 键不存在: {self.key(key)}")
            return default
        try:
            return json.loads(value)
        except (TypeError, ValueError) as exc:
            raise QueryError(
                f"键 {self.key(key)} 的内容不是合法 JSON：{exc}") from exc

    # ------------------------------------------------------------------ #
    # 计数
    # ------------------------------------------------------------------ #
    def incr(self, key: Any, amount: int = 1) -> int:
        """自增（键不存在按 0 起算）。"""
        return int(self._exec("incrby", self.key(key), int(amount)))

    def decr(self, key: Any, amount: int = 1) -> int:
        """自减。"""
        return int(self._exec("decrby", self.key(key), int(amount)))

    def incrbyfloat(self, key: Any, amount: float) -> float:
        """浮点自增（Redis 内部按字符串保留精度）。"""
        return float(self._exec("incrbyfloat", self.key(key), amount))

    # ------------------------------------------------------------------ #
    # 键管理 / 过期
    # ------------------------------------------------------------------ #
    def exists(self, *keys: Any) -> int:
        """存在的键数量（多键时是计数，不是布尔）。"""
        if not keys:
            raise InvalidArgumentException("exists 至少需要一个键")
        return int(self._exec("exists", *[self.key(k) for k in keys]))

    def delete(self, *keys: Any) -> int:
        """删除键，返回实际删除数量。"""
        if not keys:
            raise InvalidArgumentException("delete 至少需要一个键")
        return int(self._exec("delete", *[self.key(k) for k in keys]))

    def unlink(self, *keys: Any) -> int:
        """异步删除（后台回收内存，>= 4.0；大键场景优于 ``delete``）。"""
        self.driver.require_feature("unlink")
        if not keys:
            raise InvalidArgumentException("unlink 至少需要一个键")
        return int(self._exec("unlink", *[self.key(k) for k in keys]))

    def type_of(self, key: Any) -> str:
        """键的类型：``string`` / ``hash`` / ``list`` / ``set`` / ``zset`` / ``none``。"""
        value = self._exec("type", self.key(key))
        return value.decode() if isinstance(value, bytes) else str(value)

    def rename(self, src: Any, dst: Any) -> bool:
        """重命名（目标已存在会被覆盖）。"""
        return bool(self._exec("rename", self.key(src), self.key(dst)))

    def renamenx(self, src: Any, dst: Any) -> bool:
        """仅当目标不存在时重命名。"""
        return bool(self._exec("renamenx", self.key(src), self.key(dst)))

    def touch(self, *keys: Any) -> int:
        """更新访问时间（影响 LRU/LFU 淘汰顺序）。"""
        if not keys:
            raise InvalidArgumentException("touch 至少需要一个键")
        return int(self._exec("touch", *[self.key(k) for k in keys]))

    def expire(self, key: Any, seconds: int, nx: bool = False,
               xx: bool = False, gt: bool = False, lt: bool = False) -> bool:
        """设置过期秒数；``nx`` / ``xx`` / ``gt`` / ``lt`` 需 Redis >= 7.0。"""
        if nx or xx or gt or lt:
            self.driver.require_feature("expire_nx_xx_gt_lt")
        return bool(self._exec("expire", self.key(key), int(seconds),
                               nx=nx, xx=xx, gt=gt, lt=lt))

    def pexpire(self, key: Any, milliseconds: int) -> bool:
        """设置过期毫秒数。"""
        return bool(self._exec("pexpire", self.key(key), int(milliseconds)))

    def expireat(self, key: Any, timestamp: int) -> bool:
        """在指定 Unix 时间戳（秒）过期。"""
        return bool(self._exec("expireat", self.key(key), int(timestamp)))

    def persist(self, key: Any) -> bool:
        """移除过期时间（变成永久键）。"""
        return bool(self._exec("persist", self.key(key)))

    def ttl(self, key: Any) -> int:
        """剩余秒数：``-1`` 无过期、``-2`` 键不存在。"""
        return int(self._exec("ttl", self.key(key)))

    def pttl(self, key: Any) -> int:
        """剩余毫秒数（语义同 :meth:`ttl`）。"""
        return int(self._exec("pttl", self.key(key)))

    def expiretime(self, key: Any) -> int:
        """过期时间戳（秒，>= 7.0；``-1`` 表示无过期）。"""
        self.driver.require_feature("expire_nx_xx_gt_lt")
        return int(self._exec("expiretime", self.key(key)))

    def randomkey(self) -> Optional[str]:
        """随机返回一个键（逻辑键名；db 为空时返回 None）。"""
        value = self._exec("randomkey")
        return None if value is None else self.logical(value)

    # ---- 键遍历 ---- #
    def scan_iter(self, match: Optional[str] = None,
                  count: int = DEFAULT_SCAN_COUNT,
                  type: Optional[str] = None) -> Iterator[str]:
        """用 ``SCAN`` 增量遍历键（**推荐**，不阻塞服务端）。

        ``match`` 是**逻辑键名**的模式（前缀会自动补上），返回的也是逻辑
        键名。``type`` 按数据类型过滤（字符串 ``string`` / ``hash`` 等，
        需 Redis >= 6.0）。
        """
        pattern = self.prefix + (match or "*")
        kwargs: Dict[str, Any] = {"match": pattern, "count": int(count)}
        if type:
            kwargs["_type"] = type
        cursor = 0
        while True:
            cursor, keys = self._exec("scan", cursor=cursor, **kwargs)
            for key in keys:
                yield self.logical(key)
            if int(cursor) == 0:
                return

    def keys(self, pattern: str = "*") -> List[str]:
        """``KEYS`` 全量匹配（**阻塞命令**，仅适合小库/调试）。

        生产环境请用 :meth:`scan_iter`；本方法在键数超过
        :data:`SAFE_KEYS_LIMIT` 时会直接拒绝，避免误伤线上实例。
        """
        total = self.dbsize()
        if total > SAFE_KEYS_LIMIT:
            raise QueryError(
                f"当前 db 有 {total} 个键，KEYS 会阻塞服务端；"
                f"请改用 scan_iter(match=...) 增量遍历")
        found = self._exec("keys", self.prefix + (pattern or "*"))
        return [self.logical(k) for k in found]

    def clear(self, match: str, batch: int = 500) -> int:
        """按模式删除本命名空间下的键（``SCAN`` + ``UNLINK`` 分批）。

        安全护栏：``match`` 必填且不允许 ``*`` 这类"整库"模式——Redis 没有
        "库"的概念，一条 ``FLUSHDB`` 会连同别人的数据一起清掉。返回值是
        实际删除的键数。
        """
        pattern = str(match or "").strip()
        if pattern in _UNSAFE_PATTERNS:
            raise InvalidArgumentException(
                "clear() 必须指定具体模式（如 'session:*'），"
                "拒绝 '*/空' 这类整库删除；确需清库请直接用原生命令 FLUSHDB")
        deleted = 0
        buffer: List[str] = []
        for key in self.scan_iter(match=pattern, count=batch):
            buffer.append(self.key(key))
            if len(buffer) >= batch:
                deleted += int(self._exec("unlink", *buffer))
                buffer = []
        if buffer:
            deleted += int(self._exec("unlink", *buffer))
        return deleted

    # ------------------------------------------------------------------ #
    # 哈希
    # ------------------------------------------------------------------ #
    def hset(self, key: Any, mapping: Optional[Mapping[Any, Any]] = None,
             **kwargs: Any) -> int:
        """写入一个或多个字段，返回**新增**字段数。"""
        data: Dict[Any, Any] = {}
        if mapping is not None:
            data.update(_encode_mapping(mapping))
        if kwargs:
            data.update(_encode_mapping(kwargs))
        if not data:
            raise InvalidArgumentException("hset 至少需要一个字段")
        return int(self._exec("hset", self.key(key), mapping=data))

    def hsetnx(self, key: Any, field: Any, value: Any) -> bool:
        """仅当字段不存在时写入。"""
        return bool(self._exec("hsetnx", self.key(key), _check_field(field),
                               _encode_value(value)))

    def hget(self, key: Any, field: Any, default: Any = None) -> Any:
        """读单个字段。"""
        value = self._exec("hget", self.key(key), _check_field(field))
        return default if value is None else value

    def hmget(self, key: Any, *fields: Any) -> List[Any]:
        """读多个字段（缺失为 None）。"""
        if not fields:
            raise InvalidArgumentException("hmget 至少需要一个字段")
        return list(self._exec("hmget", self.key(key),
                               [_check_field(f) for f in fields]))

    def hgetall(self, key: Any) -> Dict[Any, Any]:
        """读全部字段（大 hash 请用 :meth:`hscan_iter`）。"""
        return dict(self._exec("hgetall", self.key(key)))

    def hdel(self, key: Any, *fields: Any) -> int:
        """删除字段，返回删除数量。"""
        if not fields:
            raise InvalidArgumentException("hdel 至少需要一个字段")
        return int(self._exec("hdel", self.key(key),
                              *[_check_field(f) for f in fields]))

    def hexists(self, key: Any, field: Any) -> bool:
        """字段是否存在。"""
        return bool(self._exec("hexists", self.key(key), _check_field(field)))

    def hkeys(self, key: Any) -> List[Any]:
        """全部字段名。"""
        return list(self._exec("hkeys", self.key(key)))

    def hvals(self, key: Any) -> List[Any]:
        """全部字段值。"""
        return list(self._exec("hvals", self.key(key)))

    def hlen(self, key: Any) -> int:
        """字段数量。"""
        return int(self._exec("hlen", self.key(key)))

    def hincrby(self, key: Any, field: Any, amount: int = 1) -> int:
        """整数字段自增。"""
        return int(self._exec("hincrby", self.key(key),
                              _check_field(field), int(amount)))

    def hincrbyfloat(self, key: Any, field: Any, amount: float) -> float:
        """浮点字段自增。"""
        return float(self._exec("hincrbyfloat", self.key(key),
                                _check_field(field), amount))

    def hrandfield(self, key: Any, count: Optional[int] = None,
                   withvalues: bool = False) -> Any:
        """随机取字段（``count`` 为负表示可重复）。"""
        return self._exec("hrandfield", self.key(key), count=count,
                          withvalues=withvalues)

    def hscan_iter(self, key: Any, match: Optional[str] = None,
                   count: int = DEFAULT_SCAN_COUNT
                   ) -> Iterator[Dict[Any, Any]]:
        """增量遍历 hash 字段（大 hash 的推荐读法）。"""
        cursor = 0
        while True:
            cursor, data = self._exec("hscan", self.key(key), cursor=cursor,
                                      match=match, count=int(count))
            yield dict(data)
            if int(cursor) == 0:
                return

    # ---- 哈希字段级过期（Redis >= 7.4） ---- #
    def hexpire(self, key: Any, seconds: int, *fields: Any) -> List[int]:
        """为指定字段设置过期秒数（>= 7.4）。"""
        self.driver.require_feature("hash_field_expiry")
        if not fields:
            raise InvalidArgumentException("hexpire 至少需要一个字段")
        return list(self._exec("hexpire", self.key(key), int(seconds),
                               *[_check_field(f) for f in fields]))

    def httl(self, key: Any, *fields: Any) -> List[int]:
        """字段剩余秒数（>= 7.4；-2 字段不存在 / -1 无过期）。"""
        self.driver.require_feature("hash_field_expiry")
        return list(self._exec("httl", self.key(key),
                               *[_check_field(f) for f in fields]))

    def hpersist(self, key: Any, *fields: Any) -> List[int]:
        """移除字段级过期（>= 7.4）。"""
        self.driver.require_feature("hash_field_expiry")
        return list(self._exec("hpersist", self.key(key),
                               *[_check_field(f) for f in fields]))

    # ------------------------------------------------------------------ #
    # 列表
    # ------------------------------------------------------------------ #
    def lpush(self, key: Any, *values: Any) -> int:
        """从左侧压入，返回列表长度。"""
        if not values:
            raise InvalidArgumentException("lpush 至少需要一个值")
        return int(self._exec("lpush", self.key(key),
                              *[_encode_value(v) for v in values]))

    def rpush(self, key: Any, *values: Any) -> int:
        """从右侧压入。"""
        if not values:
            raise InvalidArgumentException("rpush 至少需要一个值")
        return int(self._exec("rpush", self.key(key),
                              *[_encode_value(v) for v in values]))

    def lpop(self, key: Any, count: Optional[int] = None) -> Any:
        """从左侧弹出（``count`` 给定时返回列表）。"""
        return self._exec("lpop", self.key(key), count=count)

    def rpop(self, key: Any, count: Optional[int] = None) -> Any:
        """从右侧弹出。"""
        return self._exec("rpop", self.key(key), count=count)

    def lrange(self, key: Any, start: int = 0, end: int = -1) -> List[Any]:
        """按索引取范围（``-1`` 表示末尾）。"""
        return list(self._exec("lrange", self.key(key), int(start), int(end)))

    def llen(self, key: Any) -> int:
        """列表长度。"""
        return int(self._exec("llen", self.key(key)))

    def lindex(self, key: Any, index: int) -> Any:
        """按索引取单个元素。"""
        return self._exec("lindex", self.key(key), int(index))

    def lset(self, key: Any, index: int, value: Any) -> bool:
        """按下标覆盖。"""
        return bool(self._exec("lset", self.key(key), int(index),
                               _encode_value(value)))

    def lrem(self, key: Any, count: int, value: Any) -> int:
        """删除等于 ``value`` 的元素（``count`` 决定方向与数量）。"""
        return int(self._exec("lrem", self.key(key), int(count),
                              _encode_value(value)))

    def ltrim(self, key: Any, start: int, end: int) -> bool:
        """保留区间内的元素，其余删除。"""
        return bool(self._exec("ltrim", self.key(key), int(start), int(end)))

    def lpos(self, key: Any, value: Any, rank: Optional[int] = None,
             count: Optional[int] = None) -> Any:
        """查找元素下标（>= 6.0）。"""
        self.driver.require_feature("lpos")
        return self._exec("lpos", self.key(key), _encode_value(value),
                          rank=rank, count=count)

    def lmove(self, src: Any, dst: Any, wherefrom: str = "RIGHT",
              whereto: str = "LEFT") -> Any:
        """跨列表原子移动元素（>= 6.2）。"""
        self.driver.require_feature("lmove")
        return self._exec("lmove", self.key(src), self.key(dst),
                          wherefrom.upper(), whereto.upper())

    def lmpop(self, keys: Sequence[Any], direction: str = "LEFT",
              count: int = 1) -> Any:
        """从多个列表里弹出首个非空列表的元素（>= 7.0）。"""
        self.driver.require_feature("lmpop")
        if not keys:
            raise InvalidArgumentException("lmpop 至少需要一个键")
        numkeys = len(keys)
        return self._exec("lmpop", numkeys, *[self.key(k) for k in keys],
                          direction.upper(), count=int(count))

    def blpop(self, keys: Sequence[Any], timeout: int = 0) -> Any:
        """阻塞式左弹出（``timeout=0`` 表示永久阻塞）。"""
        if not keys:
            raise InvalidArgumentException("blpop 至少需要一个键")
        return self._exec("blpop", [self.key(k) for k in keys], timeout)

    # ------------------------------------------------------------------ #
    # 集合
    # ------------------------------------------------------------------ #
    def sadd(self, key: Any, *members: Any) -> int:
        """添加成员，返回新增数量。"""
        if not members:
            raise InvalidArgumentException("sadd 至少需要一个成员")
        return int(self._exec("sadd", self.key(key),
                              *[_check_field(m) for m in members]))

    def srem(self, key: Any, *members: Any) -> int:
        """移除成员。"""
        if not members:
            raise InvalidArgumentException("srem 至少需要一个成员")
        return int(self._exec("srem", self.key(key),
                              *[_check_field(m) for m in members]))

    def smembers(self, key: Any) -> List[Any]:
        """全部成员（大集合请用 :meth:`sscan_iter`）。"""
        return list(self._exec("smembers", self.key(key)))

    def sismember(self, key: Any, member: Any) -> bool:
        """成员是否存在。"""
        return bool(self._exec("sismember", self.key(key),
                               _check_field(member)))

    def smismember(self, key: Any, *members: Any) -> List[bool]:
        """批量判断成员是否存在（>= 6.2）。"""
        self.driver.require_feature("smismember")
        if not members:
            raise InvalidArgumentException("smismember 至少需要一个成员")
        return list(self._exec("smismember", self.key(key),
                               [_check_field(m) for m in members]))

    def scard(self, key: Any) -> int:
        """成员数量。"""
        return int(self._exec("scard", self.key(key)))

    def spop(self, key: Any, count: Optional[int] = None) -> Any:
        """随机弹出一个/多个成员。"""
        return self._exec("spop", self.key(key), count=count)

    def srandmember(self, key: Any, count: Optional[int] = None) -> Any:
        """随机取成员（不删除）。"""
        return self._exec("srandmember", self.key(key), count=count)

    def sunion(self, *keys: Any) -> List[Any]:
        """并集。"""
        if not keys:
            raise InvalidArgumentException("sunion 至少需要一个键")
        return list(self._exec("sunion", *[self.key(k) for k in keys]))

    def sinter(self, *keys: Any) -> List[Any]:
        """交集。"""
        if not keys:
            raise InvalidArgumentException("sinter 至少需要一个键")
        return list(self._exec("sinter", *[self.key(k) for k in keys]))

    def sdiff(self, *keys: Any) -> List[Any]:
        """差集。"""
        if not keys:
            raise InvalidArgumentException("sdiff 至少需要一个键")
        return list(self._exec("sdiff", *[self.key(k) for k in keys]))

    def sintercard(self, keys: Sequence[Any], limit: int = 0) -> int:
        """交集基数（>= 7.0；只算数量不返回成员，大集合更省内存）。"""
        self.driver.require_feature("sintercard")
        if not keys:
            raise InvalidArgumentException("sintercard 至少需要一个键")
        # redis-py 的签名是 sintercard(numkeys, keys, limit=0)：numkeys 与
        # keys 列表都要显式给，不能把键摊成位置参数（会被当成 limit）
        return int(self._exec("sintercard", len(keys),
                              [self.key(k) for k in keys], limit=int(limit)))

    def sscan_iter(self, key: Any, match: Optional[str] = None,
                   count: int = DEFAULT_SCAN_COUNT) -> Iterator[Any]:
        """增量遍历集合成员。"""
        cursor = 0
        while True:
            cursor, members = self._exec("sscan", self.key(key), cursor=cursor,
                                         match=match, count=int(count))
            for member in members:
                yield member
            if int(cursor) == 0:
                return

    # ------------------------------------------------------------------ #
    # 有序集合
    # ------------------------------------------------------------------ #
    def zadd(self, key: Any, mapping: Mapping[Any, float], nx: bool = False,
             xx: bool = False, gt: bool = False, lt: bool = False,
             ch: bool = False) -> int:
        """添加/更新成员分值，返回新增（``ch=True`` 时为变更）数量。

        ``gt`` / ``lt``（只在新分值更大/更小时更新）需 Redis >= 6.2。
        """
        if not isinstance(mapping, Mapping) or not mapping:
            raise InvalidArgumentException("zadd 需要非空 dict/Mapping")
        if gt or lt:
            self.driver.require_feature("zadd_gt_lt")
        pairs: Dict[Any, Any] = {}
        for member, score in mapping.items():
            try:
                pairs[_check_field(member)] = float(score)
            except (TypeError, ValueError):
                raise InvalidArgumentException(
                    f"zadd 分值必须是数字，成员 {member!r} 收到: {score!r}")
        return int(self._exec("zadd", self.key(key), pairs, nx=nx, xx=xx,
                              gt=gt, lt=lt, ch=ch))

    def zrange(self, key: Any, start: int = 0, end: int = -1,
               withscores: bool = False, desc: bool = False) -> List[Any]:
        """按排名取范围（升序；``desc=True`` 反序）。"""
        method = "zrevrange" if desc else "zrange"
        return list(self._exec(method, self.key(key), int(start), int(end),
                               withscores=withscores))

    def zrangebyscore(self, key: Any, minimum: Any, maximum: Any,
                      withscores: bool = False, offset: int = 0,
                      count: Optional[int] = None) -> List[Any]:
        """按分值区间取成员（``'-inf'`` / ``'+inf'`` 表开区间边界）。"""
        kwargs: Dict[str, Any] = {"withscores": withscores}
        if count is not None:
            # redis-py 要求 start 与 num 同时出现，只给一个会抛 DataError
            kwargs["start"] = int(offset)
            kwargs["num"] = int(count)
        return list(self._exec("zrangebyscore", self.key(key), minimum,
                               maximum, **kwargs))

    def zscore(self, key: Any, member: Any) -> Optional[float]:
        """成员分值（不存在返回 None）。"""
        value = self._exec("zscore", self.key(key), _check_field(member))
        return None if value is None else float(value)

    def zmscore(self, key: Any, *members: Any) -> List[Optional[float]]:
        """批量取分值（>= 6.2）。"""
        self.driver.require_feature("zmscore")
        if not members:
            raise InvalidArgumentException("zmscore 至少需要一个成员")
        return list(self._exec("zmscore", self.key(key),
                               [_check_field(m) for m in members]))

    def zrank(self, key: Any, member: Any, desc: bool = False) -> Optional[int]:
        """排名（从 0 开始；``desc=True`` 为倒序排名）。"""
        method = "zrevrank" if desc else "zrank"
        value = self._exec(method, self.key(key), _check_field(member))
        return None if value is None else int(value)

    def zincrby(self, key: Any, amount: float, member: Any) -> float:
        """成员分值自增。"""
        return float(self._exec("zincrby", self.key(key), amount,
                                _check_field(member)))

    def zrem(self, key: Any, *members: Any) -> int:
        """移除成员。"""
        if not members:
            raise InvalidArgumentException("zrem 至少需要一个成员")
        return int(self._exec("zrem", self.key(key),
                              *[_check_field(m) for m in members]))

    def zcard(self, key: Any) -> int:
        """成员数量。"""
        return int(self._exec("zcard", self.key(key)))

    def zcount(self, key: Any, minimum: Any, maximum: Any) -> int:
        """分值区间内的成员数量。"""
        return int(self._exec("zcount", self.key(key), minimum, maximum))

    def zpopmin(self, key: Any, count: Optional[int] = None) -> Any:
        """弹出分值最小的成员（>= 5.0）。"""
        self.driver.require_feature("zpopmin")
        return self._exec("zpopmin", self.key(key), count=count)

    def zpopmax(self, key: Any, count: Optional[int] = None) -> Any:
        """弹出分值最大的成员。"""
        self.driver.require_feature("zpopmin")
        return self._exec("zpopmax", self.key(key), count=count)

    def zremrangebyscore(self, key: Any, minimum: Any, maximum: Any) -> int:
        """按分值区间删除成员，返回删除数量。"""
        return int(self._exec("zremrangebyscore", self.key(key),
                              minimum, maximum))

    def zscan_iter(self, key: Any, match: Optional[str] = None,
                   count: int = DEFAULT_SCAN_COUNT) -> Iterator[Any]:
        """增量遍历有序集合成员（产出 ``(成员, 分值)``）。"""
        cursor = 0
        while True:
            cursor, data = self._exec("zscan", self.key(key), cursor=cursor,
                                      match=match, count=int(count))
            items = list(data.items()) if isinstance(data, dict) else list(data)
            for member, score in items:
                yield member, float(score)
            if int(cursor) == 0:
                return

    # ------------------------------------------------------------------ #
    # 流水线 / 事务
    # ------------------------------------------------------------------ #
    def pipeline(self, transaction: bool = True) -> "RedisPipeline":
        """批量执行入口（``transaction=True`` 时包在 ``MULTI/EXEC`` 里）。

        ::

            with store.pipeline() as pipe:
                pipe.incr("visits").set("last", "now")
                results = pipe.execute()      # [1, True]

        ``transaction=False`` 时不加 MULTI（允许中间插入别的客户端命令，
        但仍在一次往返内发送）。键名由包装器自动补前缀。
        """
        return RedisPipeline(self, transaction=transaction)

    # ------------------------------------------------------------------ #
    # 发布订阅
    # ------------------------------------------------------------------ #
    def publish(self, channel: Any, message: Any) -> int:
        """向频道发布消息，返回收到消息的订阅者数量。"""
        return int(self._exec("publish", self.key(channel),
                              _encode_value(message)))

    def subscribe(self, *channels: Any,
                  patterns: Optional[Sequence[Any]] = None) -> "RedisSubscription":
        """订阅频道（可选模式订阅），返回订阅包装器。

        ::

            sub = store.subscribe("events")
            for kind, channel, data in sub.listen():
                ...

        用完必须 ``sub.close()``（或 ``with``），否则连接会一直被占用。
        """
        if not channels and not patterns:
            raise InvalidArgumentException("subscribe 至少需要一个频道或模式")
        names = [self.key(c) for c in channels]
        raw_patterns = patterns
        if isinstance(raw_patterns, (str, bytes)):
            raw_patterns = [raw_patterns]
        pats = [self.key(p) for p in (raw_patterns or ())]
        client = self._call(self._driver_client)
        pubsub = self._call(client.pubsub)
        return RedisSubscription(pubsub, names, pats)

    # ------------------------------------------------------------------ #
    # 其他
    # ------------------------------------------------------------------ #
    def close(self) -> None:
        """关闭底层连接（可再次使用，驱动会自动重连）。"""
        self.conn.close()

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<RedisStore prefix={self.prefix!r}>"


# ---------------------------------------------------------------------- #
# 流水线包装
# ---------------------------------------------------------------------- #
#: 第一个位置参数是键的命令。
_PIPELINE_KEY_FIRST = frozenset({
    "set", "setex", "psetex", "setnx", "get", "getdel", "getex", "append",
    "strlen", "incr", "incrby", "incrbyfloat", "decr", "decrby",
    "expire", "pexpire", "expireat", "persist", "ttl", "pttl", "expiretime",
    "type", "hset", "hsetnx", "hget", "hmget", "hgetall", "hdel", "hexists",
    "hkeys", "hvals", "hlen", "hincrby", "hincrbyfloat", "hrandfield",
    "lpush", "rpush", "lpop", "rpop", "lrange", "llen", "lindex", "lset",
    "lrem", "ltrim", "lpos", "lmpop",
    "sadd", "srem", "smembers", "sismember", "smismember", "scard", "spop",
    "srandmember", "sintercard",
    "zadd", "zrange", "zrevrange", "zrangebyscore", "zscore", "zmscore",
    "zrank", "zrevrank", "zincrby", "zrem", "zcard", "zcount",
    "zpopmin", "zpopmax", "zremrangebyscore",
    "publish",
})

#: 所有位置参数都是键的命令。
_PIPELINE_ALL_KEYS = frozenset({"delete", "unlink", "exists", "touch",
                                "mget", "watch"})

#: 前两个位置参数都是键的命令。
_PIPELINE_TWO_KEYS = frozenset({"rename", "renamenx", "copy", "smove",
                                "lmove", "rpoplpush", "brpoplpush"})


class RedisPipeline:
    """流水线包装器：自动补键前缀、统一翻译异常。

    键名前缀规则只覆盖**明确登记**的命令（:data:`_PIPELINE_KEY_FIRST` 等）。
    未登记的命令不猜参数语义，直接拒绝并提示用 :meth:`raw`——猜错前缀
    会静默写到错误的键上，比报错危险得多。
    """

    def __init__(self, store: RedisStore, transaction: bool = True):
        self._store = store
        self._transaction = bool(transaction)
        self._pipe = store._call(store._driver_client().pipeline,
                                 transaction=self._transaction)
        self._executed = False

    # -- 键前缀 -- #
    def _prefix(self, name: str, args: Sequence[Any]) -> List[Any]:
        store = self._store
        args = list(args)
        if name in _PIPELINE_ALL_KEYS:
            if not args:
                raise InvalidArgumentException(f"流水线命令 {name} 缺少键参数")
            return [store.key(a) for a in args]
        if name in _PIPELINE_TWO_KEYS:
            if len(args) < 2:
                raise InvalidArgumentException(f"流水线命令 {name} 需要两个键")
            args[0] = store.key(args[0])
            args[1] = store.key(args[1])
            return args
        if name in _PIPELINE_KEY_FIRST:
            if not args:
                raise InvalidArgumentException(f"流水线命令 {name} 缺少键参数")
            args[0] = store.key(args[0])
            return args
        raise InvalidArgumentException(
            f"流水线未登记命令 {name!r}：为避免把键写到错误的前缀上，"
            f"这里不做猜测。请用 pipe.raw() 取原生 pipeline 自行处理键名")

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)

        def call(*args: Any, **kwargs: Any) -> "RedisPipeline":
            prepared = self._prefix(name, args)
            self._store._call(getattr(self._pipe, name), *prepared, **kwargs)
            return self

        return call

    # -- 容器型命令：显式覆写，避免"位置参数传 dict"落到错误的重载上 -- #
    def hset(self, key: Any, mapping: Optional[Mapping[Any, Any]] = None,
             **kwargs: Any) -> "RedisPipeline":
        """同 :meth:`RedisStore.hset`（流水线版）。"""
        data: Dict[Any, Any] = {}
        if mapping is not None:
            data.update(_encode_mapping(mapping))
        if kwargs:
            data.update(_encode_mapping(kwargs))
        if not data:
            raise InvalidArgumentException("hset 至少需要一个字段")
        self._store._call(self._pipe.hset, self._store.key(key), mapping=data)
        return self

    def zadd(self, key: Any, mapping: Mapping[Any, float],
             **flags: Any) -> "RedisPipeline":
        """同 :meth:`RedisStore.zadd`（流水线版）。"""
        if not isinstance(mapping, Mapping) or not mapping:
            raise InvalidArgumentException("zadd 需要非空 dict/Mapping")
        pairs: Dict[Any, Any] = {}
        for member, score in mapping.items():
            try:
                pairs[_check_field(member)] = float(score)
            except (TypeError, ValueError):
                raise InvalidArgumentException(
                    f"zadd 分值必须是数字，成员 {member!r} 收到: {score!r}")
        self._store._call(self._pipe.zadd, self._store.key(key), pairs, **flags)
        return self

    def mset(self, mapping: Mapping[Any, Any]) -> "RedisPipeline":
        """同 :meth:`RedisStore.mset`（流水线版）。"""
        if not isinstance(mapping, Mapping) or not mapping:
            raise InvalidArgumentException("mset 需要非空 dict/Mapping")
        pairs: Dict[str, Any] = {}
        for k, v in mapping.items():
            pairs[self._store.key(k)] = _encode_value(v)
        self._store._call(self._pipe.mset, pairs)
        return self

    def execute(self, raise_on_error: bool = True) -> List[Any]:
        """发送并执行全部命令，返回结果列表（顺序与入队一致）。"""
        self._executed = True
        return self._store._call(self._pipe.execute,
                                 raise_on_error=raise_on_error)

    def reset(self) -> None:
        """丢弃已入队的命令，可重新使用。"""
        self._store._call(self._pipe.reset)

    def raw(self) -> Any:
        """底层 ``redis.client.Pipeline``（键名需自行处理前缀）。"""
        return self._pipe

    def __enter__(self) -> "RedisPipeline":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if exc_type is not None:
            try:
                self.reset()
            except Exception:
                pass
        return None

    def __len__(self) -> int:
        return int(self._store._call(len, self._pipe))

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<RedisPipeline transaction={self._transaction} executed={self._executed}>"


class RedisSubscription:
    """订阅包装器：频道名已补前缀，消息在 :meth:`listen` 中逐条产出。"""

    def __init__(self, pubsub: Any, channels: Sequence[str],
                 patterns: Sequence[str]):
        self._pubsub = pubsub
        if channels:
            self._call(pubsub.subscribe, *channels)
        if patterns:
            self._call(pubsub.psubscribe, *patterns)

    @staticmethod
    def _call(fn: Any, *args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except OrmError:
            raise
        except Exception as exc:
            raise translate_redis_error(exc) from exc

    def get_message(self, timeout: float = 1.0,
                    ignore_subscribe_messages: bool = True) -> Optional[Dict[str, Any]]:
        """取一条消息（超时返回 None）。"""
        return self._call(self._pubsub.get_message,
                          timeout=timeout,
                          ignore_subscribe_messages=ignore_subscribe_messages)

    def listen(self) -> Iterator[Dict[str, Any]]:
        """持续监听，逐条产出消息 dict（``type`` / ``channel`` / ``data``）。"""
        for message in self._pubsub.listen():
            yield message

    def messages(self) -> Iterator[Any]:
        """只产出 ``(type, channel, data)`` 三元组。"""
        for message in self.listen():
            yield (message.get("type"), message.get("channel"),
                   message.get("data"))

    def close(self) -> None:
        """取消订阅并释放连接。"""
        self._call(self._pubsub.close)

    def __enter__(self) -> "RedisSubscription":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()
        return None

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<RedisSubscription {self._pubsub}>"
