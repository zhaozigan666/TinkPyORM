"""Redis 驱动（4.0 – 8.x，v0.13.0）。

依赖第三方客户端 ``redis-py``（可选依赖）：未安装时连接阶段会给出
``pip install tinkpyorm[redis]`` 的提示，不影响 SQLite / MySQL / MongoDB 用户。

设计要点
========

1. **版本是一等配置，且以探测值为准**：``Db.config(type="redis", version="7.4", ...)``。
   Redis 的服务端版本可以被廉价探测（``INFO server`` 的 ``redis_version``），
   因此能力门禁以**实际探测到的版本**为准；显式声明的 ``version`` 退化为
   一条**断言**——声明版本高于实际服务端时立即报错（与 MongoDB 侧一致）。
   ``INFO`` 被 ACL 禁止时退化为"声明值 > 缺省值"。
2. **能力矩阵而非 if 散落**：所有版本差异集中在 :data:`REDIS_FEATURES`
   （最低版本）与 :data:`REDIS_REMOVED`（移除版本），上层通过
   ``driver.supports_feature("hash_field_expiry")`` 询问。
3. **配置解析有明确优先级**：显式配置 > 配置中心回调 > 环境变量 > 默认值。
   任何一层缺失都在**连接期**报出"缺什么、去哪补"的明确错误。
4. **异常统一翻译**：``redis.exceptions.*`` 在驱动层翻译成项目既有的异常
   体系（:mod:`tinkpyorm.exceptions`），调用方无需 import 第三方库。
5. **连接不是惰性的**：``redis.Redis`` 与 pymongo 的 ``MongoClient`` 一样
   延迟建连，若不主动 ``PING``，配置错误会被推迟到第一次命令；
   本驱动在 ``connect()`` 里强制握手，失败即回滚，不留半成品客户端。
"""
from __future__ import annotations

import os
from typing import Any, Dict, Optional, Sequence
from urllib.parse import quote, urlsplit

from ..exceptions import (
    ConnectionFailedError,
    DriverNotAvailable,
    InvalidArgumentException,
    OrmError,
    QueryError,
)
from ..versioning import (
    DEFAULT_REDIS_VERSION,
    REDIS_SUPPORTED,
    Version,
    parse_version,
)
from .base import NoSQLDriver, UnsupportedOperation

__all__ = [
    "RedisDriver", "REDIS_FEATURES", "REDIS_REMOVED", "REDIS_TOPOLOGY_FEATURES",
    "REDIS_ENV_KEYS", "REDIS_CONNECT_KEYS", "translate_redis_error",
]

#: 功能名 -> 最低支持版本。未列出的功能视为"全版本支持"。
REDIS_FEATURES: Dict[str, Version] = {
    # 4.0 线
    "unlink": Version(4, 0, 0),
    "object_freq": Version(4, 0, 0),
    "memory_usage": Version(4, 0, 0),
    "swapdb": Version(4, 0, 0),
    # 5.0 线
    "stream": Version(5, 0, 0),
    "zpopmin": Version(5, 0, 0),
    # 6.0 线
    "acl": Version(6, 0, 0),
    "resp3": Version(6, 0, 0),
    "client_tracking": Version(6, 0, 0),
    "tls": Version(6, 0, 0),
    "lpos": Version(6, 0, 0),
    "set_keepttl": Version(6, 0, 0),
    # 6.2 线
    "set_get": Version(6, 2, 0),
    "set_exat_pxat": Version(6, 2, 0),
    "getdel": Version(6, 2, 0),
    "getex": Version(6, 2, 0),
    "smismember": Version(6, 2, 0),
    "lmove": Version(6, 2, 0),
    "copy": Version(6, 2, 0),
    "zrangestore": Version(6, 2, 0),
    "zmscore": Version(6, 2, 0),
    "zadd_gt_lt": Version(6, 2, 0),
    # 7.0 线
    "function": Version(7, 0, 0),
    "lmpop": Version(7, 0, 0),
    "zmpop": Version(7, 0, 0),
    "sintercard": Version(7, 0, 0),
    "expire_nx_xx_gt_lt": Version(7, 0, 0),
    "sharded_pubsub": Version(7, 0, 0),
    # 7.2 线
    "waitaof": Version(7, 2, 0),
    "client_no_touch": Version(7, 2, 0),
    # 7.4 线
    "hash_field_expiry": Version(7, 4, 0),
    # 8.0 线
    "hash_field_getex": Version(8, 0, 0),
    "vector_set": Version(8, 0, 0),
    # 与版本无关、但受部署形态约束的能力（登记在这里是为了让
    # supports_feature 的白名单包含它们，实际判定看拓扑门禁）
    "select_db": Version(4, 0, 0),
    "multi_key_unrestricted": Version(4, 0, 0),
}

#: 功能名 -> 被移除的版本（``version >= 该版本`` 即不可用）。
#:
#: Redis 的公开命令几乎不做硬移除（多以 deprecated 形式保留），
#: 因此本表目前为空；保留结构是为了让"未来某个版本移除某项能力"
#: 有可落地的门禁位置，而不是把判断散进业务代码。
REDIS_REMOVED: Dict[str, Version] = {}

#: 部署形态（``INFO cluster`` + ``INFO replication`` 识别结果）
REDIS_TOPOLOGIES = ("standalone", "cluster", "sentinel")

#: 依赖部署形态的能力 -> 允许的形态集合。
#:
#: **版本够也不代表能用**：集群模式只有 db 0，``SELECT`` 不可用；
#: 多键命令要求所有键落在同一 slot（需 hash tag）。这类差异必须在
#: 版本门禁之外单独建模，否则能力查询会给出错误结论。
REDIS_TOPOLOGY_FEATURES: Dict[str, tuple] = {
    "select_db": ("standalone", "sentinel"),
    "multi_key_unrestricted": ("standalone", "sentinel"),
}

#: 环境变量名 -> 配置字段。配置中心 / 显式配置缺失时按此顺序回退。
REDIS_ENV_KEYS: Dict[str, Sequence[str]] = {
    "url": ("REDIS_URL",),
    "host": ("REDIS_HOST",),
    "port": ("REDIS_PORT",),
    "user": ("REDIS_USER", "REDIS_USERNAME"),
    "password": ("REDIS_PASSWORD", "REDIS_PASS"),
    "database": ("REDIS_DB", "REDIS_DATABASE"),
}

#: 配置中心回调的键名（放在 ``options`` 里），返回 dict 覆盖连接参数。
REDIS_RESOLVER_KEY = "config_resolver"

#: 连接选项（``options`` 键）-> ``redis.Redis`` 参数。
#:
#: 项目内部统一用 snake_case；这里的名字与 redis-py 恰好一致，映射表
#: 仍保留（一是便于将来改名，二是让"允许哪些键"显式白名单化）。
REDIS_CONNECT_KEYS: Dict[str, str] = {
    "socket_timeout": "socket_timeout",
    "socket_connect_timeout": "socket_connect_timeout",
    "socket_keepalive": "socket_keepalive",
    "retry_on_timeout": "retry_on_timeout",
    "max_connections": "max_connections",
    "health_check_interval": "health_check_interval",
    "client_name": "client_name",
    "decode_responses": "decode_responses",
    "encoding": "encoding",
    "protocol": "protocol",
    "ssl": "ssl",
    "ssl_cert_reqs": "ssl_cert_reqs",
    "ssl_ca_certs": "ssl_ca_certs",
    "ssl_check_hostname": "ssl_check_hostname",
}

#: ``redis.exceptions`` 类名 -> 项目异常类。
#:
#: 用**类名**而非 ``isinstance`` 做匹配，是为了让翻译逻辑在
#: "redis-py 未安装"的环境里也能被单元测试覆盖（与 MongoDB 侧同一做法）。
_ERROR_TRANSLATION = {
    # 连接类：连不上 / 握手失败 / 主节点不可用 / 认证失败
    "ConnectionError": ConnectionFailedError,
    "TimeoutError": ConnectionFailedError,
    "BusyLoadingError": ConnectionFailedError,
    "MasterDownError": ConnectionFailedError,
    "AuthenticationError": ConnectionFailedError,
    "AuthenticationWrongNumberOfArgsError": ConnectionFailedError,
    # 入参类：客户端本地校验失败（类型/值非法）
    "DataError": InvalidArgumentException,
    # 其余：服务端拒绝执行（语法、类型冲突、只读副本、OOM 等）
    "ResponseError": QueryError,
    "ReadOnlyError": QueryError,
    "NoScriptError": QueryError,
    "OutOfMemoryError": QueryError,
    "WatchError": QueryError,
    "ClusterDownError": QueryError,
    "ClusterCrossSlotError": QueryError,
    "CrossSlotTransactionError": QueryError,
    "ExecAbortError": QueryError,
    "InvalidResponse": QueryError,
}


def translate_redis_error(exc: BaseException) -> BaseException:
    """把 ``redis.exceptions.*`` 翻译成项目异常体系。

    映射规则（按类名匹配，未命中则归为 :class:`QueryError`）：

    ================================  ==================================
    第三方异常                        项目异常
    ================================  ==================================
    ``ConnectionError`` /             :class:`ConnectionFailedError`
    ``TimeoutError`` /
    ``BusyLoadingError`` /
    ``MasterDownError`` /
    ``AuthenticationError``
    ``DataError``                     :class:`InvalidArgumentException`
    ``ResponseError`` /               :class:`QueryError`
    ``ReadOnlyError`` /
    ``NoScriptError`` /
    ``OutOfMemoryError`` /
    ``WatchError`` /
    ``ClusterDownError`` /
    ``ClusterCrossSlotError``
    ================================  ==================================

    已经属于项目异常体系（``OrmError``）的异常原样返回。

    认证失败归入 :class:`ConnectionFailedError` 而非 ``QueryError``：
    对调用方而言它是"连不上"（凭据错），重试策略与查询错误完全不同。
    """
    if isinstance(exc, OrmError):
        return exc
    name = type(exc).__name__
    target = _ERROR_TRANSLATION.get(name)
    if target is None:
        # 跨 slot 的键在集群下由 redis-py 前置拦截
        if "crossslot" in name.lower():
            target = QueryError
        else:
            return QueryError(f"Redis 操作失败: {exc}")
    message = str(exc)
    if target is ConnectionFailedError:
        return ConnectionFailedError(f"Redis 连接失败: {message}")
    if target is InvalidArgumentException:
        return InvalidArgumentException(f"Redis 参数非法: {message}")
    return QueryError(f"Redis 操作失败: {message}")


class RedisDriver(NoSQLDriver):
    """Redis 驱动（连接管理 + 版本门禁 + 异常翻译）。

    本类只负责**连接与方言元信息**；键值读写由
    :class:`tinkpyorm.redis.RedisStore` 承担（服务层）。

    配置方式::

        Db.config({"type": "redis", "host": "127.0.0.1", "port": 6379,
                   "password": "secret", "database": "0"})

        Db.config(type="redis", version="7.4",
                  options={"url": "redis://:secret@127.0.0.1:6379/0"})

    连接参数按 **显式配置 > 配置中心 > 环境变量 > 默认值** 解析，
    详见 :meth:`_resolve_settings`。
    """

    name = "redis"
    paramstyle = "qmark"
    supports_transactions = False   # MULTI/EXEC 由 RedisPipeline 提供，不走 SQL 事务
    supports_json = False           # 无 SQL JSON 路径；RedisJSON 属可选模块
    supports_document = False       # 文档集合是 SQLite 专有实现
    supports_fts = False            # 检索由 RediSearch 模块提供，另一套语义
    supports_upsert = False
    supports_redis = True

    def __init__(self, config: Any) -> None:
        super().__init__(config)
        raw = getattr(config, "version", None)
        self._declared_version: Optional[Version] = (
            parse_version(raw, product="redis") if raw not in (None, "") else None)
        self._detected_version: Optional[Version] = None
        #: 部署形态：``standalone`` / ``cluster`` / ``sentinel`` / None（未知）
        self._topology: Optional[str] = None
        #: 当前生效的 db 序号（连接时确定；未连接时为 None）
        self._db: Optional[int] = None
        #: 服务端配置的 db 总数（``INFO`` 的 ``databases``，默认 16）
        self._server_databases: Optional[int] = None
        #: 是否只读副本（``INFO replication`` 的 ``role:slave``）
        self._replica: Optional[bool] = None
        self._client: Any = None

    # ------------------------------------------------------------------ #
    # 版本与能力
    # ------------------------------------------------------------------ #
    @property
    def version(self) -> Version:
        """当前生效的服务端版本。

        优先级：**实际探测值** > 显式声明值 > 默认值（7.0.0）。

        探测在 :meth:`connect` 时完成（``INFO server``），因此连接后
        得到的是服务端真实能力；未连接时退化为声明值或默认值。
        """
        if self._detected_version is not None:
            return self._detected_version
        if self._declared_version is not None:
            return self._declared_version
        return parse_version(DEFAULT_REDIS_VERSION, product="redis")

    def detect_version(self) -> Version:
        """连接服务端并探测其版本（返回 :class:`Version`）。"""
        self.connect()
        if self._detected_version is None:
            raise ConnectionFailedError(
                "无法探测 Redis 服务端版本（INFO server 被拒绝或返回异常）")
        return self._detected_version

    @property
    def declared_version(self) -> Optional[Version]:
        """配置中显式声明的版本（未声明为 None）。"""
        return self._declared_version

    @property
    def server_version(self) -> Optional[Version]:
        """探测到的服务端版本（未连接或探测失败时为 None）。"""
        return self._detected_version

    @property
    def topology(self) -> Optional[str]:
        """部署形态：``standalone`` / ``cluster`` / ``sentinel``。

        未连接时为 None。**形态会影响能力**——例如集群只有 db 0，
        ``SELECT`` 不可用；多键命令要求同 slot。
        """
        return self._topology

    @property
    def db(self) -> int:
        """当前生效的 db 序号（0 起）。

        未连接时按配置解析（不建立连接）；配置非法时返回 0 而不是抛错——
        本属性用于日志/展示，严格校验发生在 :meth:`connect`。
        """
        if self._db is not None:
            return self._db
        try:
            return int(self._resolve_settings()["database"])
        except Exception:
            return 0

    @property
    def server_databases(self) -> Optional[int]:
        """服务端配置的 db 总数（``INFO`` 的 ``databases``，未探测时为 None）。"""
        return self._server_databases

    @property
    def is_replica(self) -> Optional[bool]:
        """是否只读副本（未探测时为 None）。"""
        return self._replica

    def supports_feature(self, feature: str) -> bool:
        """当前版本与部署形态是否支持某项 Redis 能力。

        两重判定：**版本门禁**（:data:`REDIS_FEATURES` / :data:`REDIS_REMOVED`）
        + **形态门禁**（:data:`REDIS_TOPOLOGY_FEATURES`）。
        """
        key = str(feature or "").strip().lower()
        if key not in REDIS_FEATURES and key not in REDIS_REMOVED:
            raise InvalidArgumentException(
                f"未知的 Redis 能力: {feature!r}。可选: "
                + ", ".join(sorted(set(REDIS_FEATURES) | set(REDIS_REMOVED))))
        version = self.version
        removed = REDIS_REMOVED.get(key)
        if removed is not None and version >= removed:
            return False
        minimum = REDIS_FEATURES.get(key)
        if minimum is not None and version < minimum:
            return False
        if key in REDIS_TOPOLOGY_FEATURES:
            required = REDIS_TOPOLOGY_FEATURES[key]
            if self._topology is not None and self._topology not in required:
                return False
        return True

    def require_feature(self, feature: str) -> None:
        """能力不足时抛出带版本/形态信息的 ``UnsupportedOperation``。"""
        key = str(feature or "").strip().lower()
        if self.supports_feature(key):
            return
        removed = REDIS_REMOVED.get(key)
        if removed is not None and self.version >= removed:
            raise UnsupportedOperation(
                f"{key} 已在 Redis {removed.major}.{removed.minor} 移除"
                f"（当前服务端版本 {self.version}）")
        required = REDIS_TOPOLOGY_FEATURES.get(key)
        minimum = REDIS_FEATURES.get(key)
        version_ok = minimum is None or self.version >= minimum
        if version_ok and required and self._topology is not None:
            raise UnsupportedOperation(
                f"{key} 需要 {' / '.join(required)} 部署，"
                f"当前是 {self._topology}（该形态不支持此能力）")
        raise UnsupportedOperation(
            f"{key} 需要 Redis >= {minimum}，当前服务端版本 {self.version}；"
            f"如实际版本更高请在 Db.config(version=...) 中声明")

    def features(self) -> Dict[str, Dict[str, Any]]:
        """能力矩阵快照：``{功能: {min, removed_in, supported}}``。"""
        out: Dict[str, Dict[str, Any]] = {}
        for name in sorted(set(REDIS_FEATURES) | set(REDIS_REMOVED)):
            minimum = REDIS_FEATURES.get(name)
            removed = REDIS_REMOVED.get(name)
            out[name] = {
                "min": str(minimum) if minimum else None,
                "removed_in": str(removed) if removed else None,
                "supported": self.supports_feature(name),
            }
        return out

    def dialect_info(self) -> Dict[str, Any]:
        """方言概况（版本 / 形态 / 能力）。"""
        return {
            "driver": self.name,
            "version": str(self.version),
            "declared_version": (str(self._declared_version)
                                 if self._declared_version else None),
            "detected_version": (str(self._detected_version)
                                 if self._detected_version else None),
            "topology": self._topology,
            "is_replica": self._replica,
            "db": self.db,
            "server_databases": self._server_databases,
            "supported_range": REDIS_SUPPORTED,
            "features": self.features(),
        }

    # ------------------------------------------------------------------ #
    # 配置解析
    # ------------------------------------------------------------------ #
    @staticmethod
    def _env_lookup(key: str) -> Optional[str]:
        """按 :data:`REDIS_ENV_KEYS` 的顺序读环境变量（首个非空者胜出）。"""
        for name in REDIS_ENV_KEYS.get(key, ()):
            value = os.environ.get(name)
            if value:
                return value
        return None

    def _resolver_settings(self) -> Dict[str, Any]:
        """调用配置中心回调，返回其提供的连接参数。

        回调签名 ``resolver() -> dict``，可返回 ``url`` / ``host`` / ``port`` /
        ``user`` / ``password`` / ``database`` / ``options`` 等键。
        回调异常直接上抛（配置中心的故障不应被静默吞掉）。
        """
        resolver = self.config.options.get(REDIS_RESOLVER_KEY)
        if resolver is None:
            return {}
        if not callable(resolver):
            raise InvalidArgumentException(
                f"options[{REDIS_RESOLVER_KEY!r}] 必须是可调用对象"
                f"（签名为 resolver() -> dict），收到: {type(resolver).__name__}")
        data = resolver()
        if not isinstance(data, dict):
            raise InvalidArgumentException(
                f"配置中心回调必须返回 dict，收到: {type(data).__name__}")
        return dict(data)

    def _resolve_settings(self) -> Dict[str, Any]:
        """解析最终连接参数（显式 > 配置中心 > 环境变量 > 默认值）。

        ``database`` 对 Redis 而言是 **db 序号**（0–15），不是库名；
        :class:`~tinkpyorm.config.Config` 的默认值 ``":memory:"``
        在这里等同于"未设置"，不能用它挡住环境变量 / 配置中心的取值。
        """
        resolver = self._resolver_settings()
        resolver_options = dict(resolver.get("options") or {})
        explicit_options = dict(self.config.options or {})

        def pick(key: str, *fallbacks: Any) -> Any:
            explicit = getattr(self.config, key, None)
            if key == "database":
                # 非数字的 database（含 SQLite 的 ":memory:"）视为"未设置"
                if explicit is not None and not _looks_like_int(explicit):
                    explicit = None
            if explicit not in (None, ""):
                return explicit
            if key in explicit_options and explicit_options[key] not in (None, ""):
                return explicit_options[key]
            if key in resolver and resolver[key] not in (None, ""):
                return resolver[key]
            env = self._env_lookup(key)
            if env is not None:
                return env
            for value in fallbacks:
                if value not in (None, ""):
                    return value
            return None

        raw_db = getattr(self.config, "database", None)
        if raw_db not in (None, "", ":memory:") and not _looks_like_int(raw_db):
            raise InvalidArgumentException(
                f"Redis 的 database 是 db 序号（0–15），必须是整数，收到: "
                f"{raw_db!r}；如果要连的是 URL，请放在 options['url'] 里")

        # ``db`` 是 ``database`` 的别名（redis-py 的连接参数就叫 db）。
        # 四个来源（config 字段 / options.database / options.db / URL 的 /N）
        # 指向同一个值，互相不一致时**报错**而不是静默取一个——否则用户改了
        # 配置却不生效，排查成本极高。
        option_db = explicit_options.get("db")
        if option_db is None:
            option_db = resolver.get("db")
        if option_db is None:
            option_db = resolver_options.get("db")
        explicit_db = explicit_options.get("database")
        if explicit_db is None:
            explicit_db = resolver.get("database")

        url = explicit_options.get("url") or resolver_options.get("url")
        if url is None:
            url = pick("url")
        url_db = _db_from_url(url)

        candidates = [("config.database", raw_db),
                      ("options.database", explicit_db),
                      ("options.db", option_db)]
        if url_db is not None:
            candidates.append(("url", url_db))
        given = [(name, value) for name, value in candidates
                 if value not in (None, "", ":memory:")]
        if len({str(value).strip() for _, value in given}) > 1:
            detail = "、".join(f"{name}={value!r}" for name, value in given)
            raise InvalidArgumentException(
                f"Redis 的 db 序号给了多个不一致的值：{detail}；"
                f"请只保留一个（database / db / URL 里的 /N 是同一个配置项）")

        if url_db is not None:
            # URL 里的 /N 是最终生效值（redis-py 连接时就按它 SELECT）
            db_value: Any = url_db
        elif given:
            db_value = given[0][1]
        else:
            db_value = pick("database", 0)

        settings: Dict[str, Any] = {
            "url": url,
            "host": pick("host", "127.0.0.1"),
            "port": pick("port", 6379),
            "user": pick("user"),
            "password": pick("password"),
            "database": db_value,
            "options": {},
        }

        merged_options: Dict[str, Any] = {}
        merged_options.update(resolver_options)
        merged_options.update(explicit_options)
        merged_options.pop(REDIS_RESOLVER_KEY, None)
        merged_options.pop("url", None)

        connect_kwargs: Dict[str, Any] = {}
        for key, target in REDIS_CONNECT_KEYS.items():
            if key in merged_options:
                connect_kwargs[target] = merged_options[key]

        # 通用超时配置兜底（秒）
        if self.config.connect_timeout is not None:
            seconds = float(self.config.connect_timeout)
            connect_kwargs.setdefault("socket_connect_timeout", seconds)

        # 默认连接超时 5 秒：redis-py 默认无超时，配置错误会无限挂起
        connect_kwargs.setdefault("socket_connect_timeout", 5.0)
        # 默认解码为 str（键值都是文本时最省心；二进制场景显式设 False）
        connect_kwargs.setdefault("decode_responses", True)

        settings["options"] = connect_kwargs
        self._validate_settings(settings)
        return settings

    def _validate_settings(self, settings: Dict[str, Any]) -> None:
        """连接参数校验（非法即报错，不静默兜底）。"""
        if settings.get("password") and not settings.get("url"):
            pass  # 允许只给密码（Redis < 6 的 AUTH 只需密码）
        host = settings.get("host")
        if not host:
            raise InvalidArgumentException(
                "Redis 缺少连接地址：请设置 config 的 host"
                "（或 options['url']），或提供环境变量 REDIS_HOST")
        try:
            port = int(settings.get("port"))
        except (TypeError, ValueError):
            raise InvalidArgumentException(
                f"Redis 端口必须是整数，收到: {settings.get('port')!r}")
        if not 0 < port < 65536:
            raise InvalidArgumentException(f"Redis 端口越界: {port}")
        try:
            db = int(settings.get("database"))
        except (TypeError, ValueError):
            raise InvalidArgumentException(
                f"Redis 的 database 是 db 序号（0–15），必须是整数，收到: "
                f"{settings.get('database')!r}")
        if db < 0:
            raise InvalidArgumentException(f"Redis db 序号不能为负: {db}")
        settings["port"] = port
        settings["database"] = db

    def _build_url(self, settings: Dict[str, Any]) -> str:
        """由分散参数拼装连接串（已显式给出 url 时原样使用）。"""
        url = settings.get("url")
        if url:
            return str(url)
        scheme = "rediss" if settings["options"].get("ssl") else "redis"
        user = settings.get("user")
        password = settings.get("password")
        auth = ""
        if user:
            auth = quote(str(user), safe="")
            if password:
                auth += ":" + quote(str(password), safe="")
            auth += "@"
        elif password:
            auth = ":" + quote(str(password), safe="") + "@"
        return (f"{scheme}://{auth}{settings['host']}:{settings['port']}"
                f"/{settings['database']}")

    # ------------------------------------------------------------------ #
    # 连接
    # ------------------------------------------------------------------ #
    def connect(self) -> Any:
        """建立并返回 ``redis.Redis``（幂等，且**立即握手**）。

        ``redis.Redis`` 构造时不建连，因此这里显式 ``PING`` 一次：
        配置错误/网络不通/密码错误都必须在这里暴露，而不是推迟到
        第一条业务命令。失败时回滚，``is_connected()`` 保持 False。
        """
        if self._client is not None:
            return self._client
        try:
            import redis as _redis
        except ImportError as exc:  # pragma: no cover - 取决于环境
            raise DriverNotAvailable(
                "Redis 驱动需要 redis-py：pip install redis "
                "（或 pip install tinkpyorm[redis]）") from exc

        settings = self._resolve_settings()
        self._db = int(settings["database"])
        url = self._build_url(settings)
        kwargs = dict(settings["options"])
        # 连接串里已包含 host/port/db/凭据（密码经 quote 转义），
        # 不再用 kwargs 重复传，避免"URL 与参数谁生效"的歧义。
        try:
            client = _redis.Redis.from_url(url, **kwargs)
        except TypeError as exc:
            raise InvalidArgumentException(
                f"Redis 连接参数不被 redis-py 接受: {exc}；"
                f"请用 redis-py >= 4.0（protocol 选项需 >= 5.0）") from exc
        except Exception as exc:
            raise translate_redis_error(exc) from exc

        self._client = client
        try:
            # PING 也会抛第三方异常（认证失败 / 网络超时），必须与命令执行
            # 走同一套翻译——否则 redis.exceptions.* 会从 connect() 泄漏出去。
            try:
                client.ping()
            except OrmError:
                raise
            except Exception as exc:
                # SELECT 失败（db 越界 / 集群禁用 SELECT）要在建连阶段
                # 解释成"db 配错了"，而不是丢一个泛化的服务端错误。
                explained = self._select_error(exc)
                raise (explained or translate_redis_error(exc)) from exc
            self._detect_meta()
            self._check_db_index()
        except BaseException:
            # 握手失败：回滚，避免留下"看似已连接、实则不可用"的客户端
            self.close()
            raise
        return self._client

    def _detect_meta(self) -> None:
        """探测版本与部署形态（``INFO`` 的失败不阻断连接）。

        ``INFO`` 需要权限（ACL 可能只给 ``+@read``）；拿不到就退化为
        "声明值 > 缺省值"，不把权限问题误报成连接问题。
        """
        info: Optional[Dict[str, Any]] = None
        try:
            info = self._client.info()
        except Exception:
            info = None
        if isinstance(info, dict):
            raw = info.get("redis_version")
            if raw:
                try:
                    self._detected_version = parse_version(
                        str(raw), product="redis")
                except Exception:
                    # 版本号超出支持范围时不阻断连接（例如 8.10.2 这种
                    # 快速发布线），只记录、不参与门禁
                    try:
                        self._detected_version = parse_version(
                            str(raw), product="generic")
                    except Exception:
                        self._detected_version = None
            self._topology = self._classify_topology(info)
            databases = info.get("databases")
            if databases is not None:
                try:
                    self._server_databases = int(databases)
                except (TypeError, ValueError):
                    self._server_databases = None
            role = info.get("role")
            if role:
                self._replica = str(role).lower() in ("slave", "replica")
        else:
            self._topology = self._topology or "standalone"
        # INFO 不含 databases（实测 8.10.2），统一走 CONFIG GET 探测
        self._detect_databases()
        if self._detected_version is None:
            # 无 INFO 权限：以声明值为准（若也没有则用默认）
            self._detected_version = self._declared_version
        self._check_declared_version()

    @staticmethod
    def _classify_topology(info: Dict[str, Any]) -> str:
        """由 ``INFO`` 应答判定部署形态。"""
        if not isinstance(info, dict):
            return "standalone"
        if str(info.get("cluster_enabled", "0")).strip() in ("1", "true", "True"):
            return "cluster"
        if info.get("redis_mode") == "sentinel":
            return "sentinel"
        return "standalone"

    def _detect_databases(self) -> None:
        """探测服务端配置的 db 总数（``CONFIG GET databases``，默认 16）。

        ``INFO`` **不包含** ``databases`` 字段（实测 Redis 8.10.2），只能用
        ``CONFIG GET``；ACL 未授权 ``+config|get`` 时静默跳过——拿不到就
        不做"上限校验"，留给服务端在握手阶段自己拒绝。
        """
        if self._server_databases is not None:
            return
        try:
            raw = self._client.config_get("databases")
        except Exception:
            return
        if not isinstance(raw, dict):
            return
        value = raw.get("databases", raw.get(b"databases"))
        try:
            self._server_databases = int(value)
        except (TypeError, ValueError):
            self._server_databases = None

    def _check_db_index(self) -> None:
        """连接后校验 db 序号是否落在服务端配置的范围内。

        顺序在 :meth:`_detect_meta` 之后：``INFO`` 的 ``databases`` 是服务端
        **真实配置**（默认 16，可用 ``databases`` 指令改成别的值），只有连上
        才知道。越界即断开连接并报出"服务端只有 N 个 db"。
        """
        if self._db is None:
            return
        db = self._db          # close() 会复位 _db，先留存用于报错信息
        if self._topology == "cluster" and db != 0:
            self.close()
            raise InvalidArgumentException(
                f"Redis 集群模式只有 db 0，不支持 db {db}；"
                f"请把 database 改成 0（或改用非集群实例）")
        if self._server_databases is not None and db >= self._server_databases:
            total = self._server_databases
            self.close()
            raise InvalidArgumentException(
                f"Redis db 序号越界：配置的是 {db}，"
                f"而服务端只配置了 {total} 个 db（0–{total - 1}）；"
                f"请修正 database（服务端可用 databases 指令扩容）")

    def _select_error(self, exc: BaseException) -> Optional[BaseException]:
        """把"建连时 SELECT 失败"解释成可操作的配置错误。

        服务端在握手阶段执行 ``SELECT <db>``，所以 db 写错的表现是
        ``ResponseError: DB index is out of range``——直接归到 ``QueryError``
        会让人以为是命令写错了，这里明确指向配置项。
        """
        text = str(exc).lower()
        if "db index is out of range" in text or ("out of range" in text and "db" in text):
            db = self._db
            total = self._server_databases
            hint = f"（服务端配置了 {total} 个 db，0–{total - 1}）" if total else \
                "（默认共 16 个 db：0–15，服务端实际值见 CONFIG GET databases）"
            return InvalidArgumentException(
                f"Redis db 序号越界：配置的是 {db}{hint}；请修正 database")
        if "select" in text and ("cluster" in text or "not allowed" in text):
            return InvalidArgumentException(
                f"当前部署形态不支持选择 db（配置的是 {self._db}）："
                f"Redis 集群只有 db 0；请把 database 改成 0")
        return None

    def _check_declared_version(self) -> None:
        """声明版本高于实际服务端时立即报错（配置与现实不符）。"""
        if self._declared_version is None or self._detected_version is None:
            return
        if self._declared_version > self._detected_version:
            raise InvalidArgumentException(
                f"声明的 Redis 版本 {self._declared_version} 高于实际服务端版本 "
                f"{self._detected_version}；请修正 Db.config(version=...)")

    def close(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
            finally:
                self._client = None
                self._detected_version = None
                self._db = None
                self._server_databases = None

    def ping(self) -> bool:
        """连接是否可用（未连接时先建立连接）。"""
        try:
            if self._client is None:
                self.connect()
            self._client.ping()
            return True
        except Exception:
            return False

    def is_connected(self) -> bool:
        return self._client is not None

    @property
    def raw_connection(self) -> Any:
        """原生 ``redis.Redis``（逃生舱）。"""
        return self.connect()

    def client(self) -> Any:
        """返回底层 ``redis.Redis``（服务层入口）。"""
        return self.connect()

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<RedisDriver db{self.db} v{self.version}>"


def _db_from_url(url: Any) -> Optional[int]:
    """从连接串路径取出 db 序号（``redis://h:6379/2`` -> 2）。

    ``redis-py`` 在 ``from_url`` 时就按路径里的 ``/N`` SELECT，因此本层的
    "生效 db"必须与它一致；解析不出来时返回 None（交给配置字段决定）。
    """
    if not url:
        return None
    try:
        parsed = urlsplit(str(url))
    except ValueError:
        return None
    segment = (parsed.path or "").strip("/").split("/")[0]
    if not segment.isdigit():
        return None
    return int(segment)


def _looks_like_int(value: Any) -> bool:
    """判断取值能否当作 db 序号（``"0"`` / ``0`` 可以，``":memory:"`` 不行）。"""
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    if isinstance(value, str):
        return value.strip().lstrip("-").isdigit()
    return False
