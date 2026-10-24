"""MongoDB 驱动（4.0 – 8.0，v0.12.0）。

依赖第三方客户端 ``pymongo``（可选依赖）：未安装时连接阶段会给出
``pip install tinkpyorm[mongo]`` 的提示，不影响 SQLite / MySQL 用户。

设计要点
========

1. **版本是一等配置**：``Db.config(type="mongodb", version="7.0", ...)``。
   与 MySQL 侧不同的是，MongoDB 的服务端版本**可以被廉价地探测**
   （``hello`` 命令返回 ``maxWireVersion``，无需额外权限），因此
   能力门禁以**实际探测到的版本**为准；显式声明的 ``version`` 退化为
   一条**断言**——声明版本高于实际服务端时立即报错。
2. **能力矩阵而非 if 散落**：所有版本差异集中在 :data:`MONGO_FEATURES`
   （最低版本）与 :data:`MONGO_REMOVED`（移除版本），上层通过
   ``driver.supports_feature("transactions")`` 询问。
3. **配置解析有明确优先级**：显式配置 > 配置中心回调 > 环境变量 > 默认值。
   任何一层缺失都在**连接期**报出"缺什么、去哪补"的明确错误，而不是
   静默用默认值蒙混过关。
4. **异常统一翻译**：``pymongo`` / ``bson`` 的异常在驱动层翻译成项目
   既有的异常体系（:mod:`tinkpyorm.exceptions`），调用方无需 import
   第三方库即可捕获。
"""
from __future__ import annotations

import os
from typing import Any, Dict, Optional, Sequence
from urllib.parse import quote

from ..exceptions import (
    ConnectionFailedError,
    DriverNotAvailable,
    DuplicateKeyError,
    InvalidArgumentException,
    OrmError,
    QueryError,
)
from ..versioning import (
    DEFAULT_MONGO_VERSION,
    MONGO_SUPPORTED,
    Version,
    parse_version,
)
from .base import NoSQLDriver, UnsupportedOperation

__all__ = [
    "MongoDBDriver", "MONGO_FEATURES", "MONGO_REMOVED",
    "MONGO_ENV_KEYS", "translate_mongo_error",
]

#: 功能名 -> 最低支持版本。未列出的功能视为"全版本支持"。
MONGO_FEATURES: Dict[str, Version] = {
    # 3.6 线
    "change_stream": Version(3, 6, 0),
    "retryable_writes": Version(3, 6, 0),
    "json_schema": Version(3, 6, 0),
    # 4.x 线
    "transactions": Version(4, 0, 0),
    "update_pipeline": Version(4, 2, 0),
    "merge_stage": Version(4, 2, 0),
    "wildcard_index": Version(4, 2, 0),
    "union_with": Version(4, 4, 0),
    "function_accumulator": Version(4, 4, 0),
    "hidden_index": Version(4, 4, 0),
    "sharded_lookup": Version(4, 4, 0),
    # 5.x 线
    "set_window_fields": Version(5, 0, 0),
    "time_series": Version(5, 0, 0),
    "densify_stage": Version(5, 1, 0),
    "top_n_accumulators": Version(5, 2, 0),
    "fill_stage": Version(5, 3, 0),
    # 6.x 线
    "documents_stage": Version(6, 0, 0),
    "sharded_change_stream": Version(6, 0, 0),
    # 7.x / 8.x 线
    "search_meta": Version(7, 0, 0),
    "rank_fusion": Version(8, 0, 0),
}

#: 功能名 -> 被移除的版本（``version >= 该版本`` 即不可用）。
MONGO_REMOVED: Dict[str, Version] = {
    # 4.0 移除 MONGODB-CR 认证机制
    "mongodb_cr_auth": Version(4, 0, 0),
    # 4.4 移除 geoHaystack 索引与 $geoSearch
    "geo_haystack": Version(4, 4, 0),
    # 5.1 移除 OP_QUERY 线协议（仅保留 OP_MSG）
    "op_query": Version(5, 1, 0),
}

#: 部署拓扑（``hello`` 命令识别出的形态）
MONGO_TOPOLOGIES = ("replica_set", "sharded", "standalone")

#: 依赖部署拓扑的能力 -> 允许的拓扑集合。
#:
#: **版本够也不代表能用**：多文档事务需要副本集或分片集群，单节点
#: （standalone）部署即使跑 MongoDB 8.x 也不支持。这类能力必须在
#: 版本门禁之外再加一道拓扑门禁，否则能力查询会给出错误结论。
MONGO_TOPOLOGY_FEATURES: Dict[str, tuple] = {
    "transactions": ("replica_set", "sharded"),
    "sharded_change_stream": ("sharded",),
    "sharded_lookup": ("sharded",),
}

#: 环境变量名 -> 配置字段。配置中心 / 显式配置缺失时按此顺序回退。
MONGO_ENV_KEYS: Dict[str, Sequence[str]] = {
    "uri": ("MONGODB_URI", "MONGO_URI"),
    "database": ("MONGODB_DATABASE", "MONGO_DB"),
    "user": ("MONGODB_USER", "MONGO_USERNAME"),
    "password": ("MONGODB_PASSWORD", "MONGO_PASSWORD"),
    "auth_source": ("MONGODB_AUTH_SOURCE", "MONGO_AUTH_SOURCE"),
    "host": ("MONGODB_HOST", "MONGO_HOST"),
    "port": ("MONGODB_PORT", "MONGO_PORT"),
}

#: 配置中心回调的键名（放在 ``options`` 里），返回 dict 覆盖连接参数。
MONGO_RESOLVER_KEY = "config_resolver"

#: 连接选项（``options`` 键）-> pymongo ``MongoClient`` 参数。
#:
#: 项目内部统一用 snake_case，对外转成 pymongo 的 camelCase。
MONGO_CONNECT_KEYS: Dict[str, str] = {
    "server_selection_timeout_ms": "serverSelectionTimeoutMS",
    "connect_timeout_ms": "connectTimeoutMS",
    "socket_timeout_ms": "socketTimeoutMS",
    "max_pool_size": "maxPoolSize",
    "min_pool_size": "minPoolSize",
    "max_idle_time_ms": "maxIdleTimeMS",
    "wait_queue_timeout_ms": "waitQueueTimeoutMS",
    "retry_writes": "retryWrites",
    "retry_reads": "retryReads",
    "auth_mechanism": "authMechanism",
    "appname": "appname",
    "direct_connection": "directConnection",
    "replica_set": "replicaSet",
    "tls": "tls",
    "compressors": "compressors",
}

#: 写关注（write concern）参数名映射。
MONGO_WRITE_CONCERN_KEYS: Dict[str, str] = {
    "w": "w",
    "j": "j",
    "wtimeout": "wtimeoutMS",
    "wtimeoutms": "wtimeoutMS",
    "wtimeout_ms": "wtimeoutMS",
}

#: ``maxWireVersion`` -> 最低服务端版本（用于版本探测）。
#:
#: 取"不高于该 wire 版本的最小发布版本"，与官方 wire version 表一致：
#: 3.6=6 / 4.0=7 / 4.2=8 / 4.4=9 / 5.0=13 / 6.0=17 / 7.0=21 / 8.0=25。
_WIRE_VERSION_TABLE = (
    (25, Version(8, 0, 0)),
    (21, Version(7, 0, 0)),
    (17, Version(6, 0, 0)),
    (13, Version(5, 0, 0)),
    (9, Version(4, 4, 0)),
    (8, Version(4, 2, 0)),
    (7, Version(4, 0, 0)),
    (6, Version(3, 6, 0)),
)

#: pymongo / bson 异常类名 -> 项目异常类。
#:
#: 用**类名**而非 ``isinstance`` 做匹配，是为了让翻译逻辑在
#: "pymongo 未安装"的环境里也能被单元测试覆盖（见 ``test_mongo.py``）。
_ERROR_TRANSLATION = {
    "DuplicateKeyError": DuplicateKeyError,
    "InvalidId": InvalidArgumentException,
    "InvalidDocument": InvalidArgumentException,
    "InvalidOperation": InvalidArgumentException,
    "ConfigurationError": InvalidArgumentException,
    "ServerSelectionTimeoutError": ConnectionFailedError,
    "ConnectionFailure": ConnectionFailedError,
    "NetworkTimeout": ConnectionFailedError,
    "AutoReconnect": ConnectionFailedError,
    "NotPrimaryError": ConnectionFailedError,
    "ExecutionTimeout": QueryError,
    "OperationFailure": QueryError,
    "WriteError": QueryError,
    "WriteConcernError": QueryError,
    "BulkWriteError": QueryError,
    "CursorNotFound": QueryError,
    "EncryptionError": QueryError,
}


def translate_mongo_error(exc: BaseException) -> BaseException:
    """把 ``pymongo`` / ``bson`` 异常翻译成项目异常体系。

    翻译规则（按类名匹配，未命中则归为 :class:`QueryError`）：

    ==========================  ===================================
    第三方异常                  项目异常
    ==========================  ===================================
    ``DuplicateKeyError``       :class:`DuplicateKeyError`
    ``InvalidId`` /             :class:`InvalidArgumentException`
    ``InvalidDocument`` /
    ``InvalidOperation`` /
    ``ConfigurationError``
    ``ServerSelectionTimeout``  :class:`ConnectionFailedError`
    ``Error`` / ``Connection``
    ``Failure`` / ``AutoReconnect``
    ``OperationFailure`` /      :class:`QueryError`
    ``WriteError`` 等
    ==========================  ===================================

    已经属于项目异常体系（``OrmError``）的异常原样返回。
    """
    if isinstance(exc, OrmError):
        return exc
    name = type(exc).__name__
    target = _ERROR_TRANSLATION.get(name)
    if target is None:
        return QueryError(f"MongoDB 操作失败: {exc}")
    # 认证失败（Unauthorized=13 / AuthenticationFailed=18）本质是连接问题，
    # 归入 ConnectionFailedError 便于调用方按"连不上"统一重试/告警。
    if target is QueryError and _is_auth_error(exc):
        target = ConnectionFailedError
    # 批量写撞唯一键时 pymongo 抛的是 BulkWriteError（不是 DuplicateKeyError），
    # 语义上仍是重复键，不能一律归成 QueryError。
    if target is QueryError and _is_duplicate_key_error(exc):
        target = DuplicateKeyError
    message = str(exc)
    if target is DuplicateKeyError:
        return DuplicateKeyError(f"MongoDB 唯一键冲突: {message}")
    if target is InvalidArgumentException:
        return InvalidArgumentException(f"MongoDB 参数非法: {message}")
    if target is ConnectionFailedError:
        return ConnectionFailedError(f"MongoDB 连接失败: {message}")
    return QueryError(f"MongoDB 操作失败: {message}")


def _is_auth_error(exc: BaseException) -> bool:
    """判断是否为认证 / 授权失败（MongoDB 错误码 13 / 18）。"""
    code = getattr(exc, "code", None)
    if code in (13, 18):
        return True
    text = str(exc).lower()
    return "requires authentication" in text or "auth failed" in text


def _is_duplicate_key_error(exc: BaseException) -> bool:
    """判断是否为唯一键冲突（MongoDB 错误码 11000）。

    ``insert_one`` 抛 ``DuplicateKeyError``，而 ``insert_many`` /
    ``bulk_write`` 抛的是 ``BulkWriteError``（详情里带 ``writeErrors``），
    两者语义相同，都应归为 :class:`DuplicateKeyError`。
    """
    if getattr(exc, "code", None) == 11000:
        return True
    details = getattr(exc, "details", None)
    if isinstance(details, dict):
        for item in details.get("writeErrors") or ():
            if isinstance(item, dict) and item.get("code") == 11000:
                return True
    text = str(exc)
    return "E11000" in text or "duplicate key error" in text.lower()


class MongoDBDriver(NoSQLDriver):
    """MongoDB 驱动（连接管理 + 版本门禁 + 异常翻译）。

    本类只负责**连接与方言元信息**；集合级数据访问由
    :class:`tinkpyorm.mongo.MongoCollection` 承担（服务层）。

    配置方式::

        Db.config({"type": "mongodb", "database": "app",
                   "options": {"uri": "mongodb://user:pwd@host:27017/?authSource=admin"}})

        Db.config(type="mongodb", database="app", host="127.0.0.1",
                  port=27017, user="app", password="secret")

    连接参数按 **显式配置 > 配置中心 > 环境变量 > 默认值** 解析，
    详见 :meth:`_resolve_settings`。
    """

    name = "mongodb"
    paramstyle = "qmark"
    supports_transactions = False   # MongoDB 事务需显式 session，不走 ORM 事务 API
    supports_json = True
    supports_document = False      # 文档集合是 SQLite 专有实现
    supports_fts = False           # MongoDB 用 text / wildcard 索引，语义不同
    supports_upsert = False        # upsert 语义由集合层 update(upsert=True) 提供
    supports_mongo = True

    def __init__(self, config: Any) -> None:
        super().__init__(config)
        raw = getattr(config, "version", None)
        self._declared_version: Optional[Version] = (
            parse_version(raw, product="mongodb") if raw not in (None, "") else None)
        self._detected_version: Optional[Version] = None
        #: 部署拓扑：``replica_set`` / ``sharded`` / ``standalone`` / None（未知）
        self._topology: Optional[str] = None
        self._client: Any = None

    # ------------------------------------------------------------------ #
    # 版本与能力
    # ------------------------------------------------------------------ #
    @property
    def version(self) -> Version:
        """当前生效的服务端版本。

        优先级：**实际探测值** > 显式声明值 > 默认值（4.0.0）。

        探测需要一次真实往返（``hello`` 命令），因此**在建立连接后**
        才可用；未连接时若显式声明了 ``version`` 则以声明值为准
        （此时它同时是一条断言：声明版本高于实际服务端会在连接时报错）。
        需要主动探测服务端版本时调用 :meth:`detect_version`。
        """
        if self._detected_version is not None:
            return self._detected_version
        if self._declared_version is not None:
            return self._declared_version
        return parse_version(DEFAULT_MONGO_VERSION, product="mongodb")

    def detect_version(self) -> Version:
        """连接服务端并探测其版本（返回 :class:`Version`）。

        与 MySQL 侧"必须显式声明版本"不同，MongoDB 的服务端版本可以廉价
        探测（``hello`` 命令，无需额外权限），因此优先以**实际能力**为准，
        避免声明值与现实不符导致门禁误判。
        """
        self.connect()
        if self._detected_version is None:
            raise ConnectionFailedError(
                "无法探测 MongoDB 服务端版本（hello 命令失败）")
        return self._detected_version

    @property
    def declared_version(self) -> Optional[Version]:
        """配置中显式声明的版本（未声明为 None）。"""
        return self._declared_version

    @property
    def server_version(self) -> Optional[Version]:
        """探测到的服务端版本（未连接时为 None）。"""
        return self._detected_version

    @property
    def topology(self) -> Optional[str]:
        """部署拓扑：``replica_set`` / ``sharded`` / ``standalone``。

        未连接时为 None。**拓扑会影响能力**——例如事务只在副本集或分片
        集群上可用，单节点（standalone）即使版本够也不支持。
        """
        return self._topology

    def supports_feature(self, feature: str) -> bool:
        """当前版本与部署拓扑是否支持某项 MongoDB 能力。

        两重判定：
        **版本门禁**（见 :data:`MONGO_FEATURES` / :data:`MONGO_REMOVED`）
        + **拓扑门禁**（少数能力依赖副本集/分片集群）。
        """
        key = str(feature or "").strip().lower()
        if key not in MONGO_FEATURES and key not in MONGO_REMOVED:
            raise InvalidArgumentException(
                f"未知的 MongoDB 能力: {feature!r}。可选: "
                + ", ".join(sorted(set(MONGO_FEATURES) | set(MONGO_REMOVED))))
        version = self.version
        removed = MONGO_REMOVED.get(key)
        if removed is not None and version >= removed:
            return False
        minimum = MONGO_FEATURES.get(key)
        if minimum is not None and version < minimum:
            return False
        # 拓扑门禁：单节点部署不具备这些能力（版本再高也不行）
        if key in MONGO_TOPOLOGY_FEATURES:
            required = MONGO_TOPOLOGY_FEATURES[key]
            if self._topology is not None and self._topology not in required:
                return False
        return True

    def require_feature(self, feature: str) -> None:
        """能力不足时抛出带版本/拓扑信息的 ``UnsupportedOperation``。"""
        key = str(feature or "").strip().lower()
        if self.supports_feature(key):
            return
        removed = MONGO_REMOVED.get(key)
        if removed is not None and self.version >= removed:
            raise UnsupportedOperation(
                f"{key} 已在 MongoDB {removed.major}.{removed.minor} 移除"
                f"（当前服务端版本 {self.version}）")
        required = MONGO_TOPOLOGY_FEATURES.get(key)
        minimum = MONGO_FEATURES.get(key)
        version_ok = minimum is None or self.version >= minimum
        if version_ok and required and self._topology is not None:
            raise UnsupportedOperation(
                f"{key} 需要 {' / '.join(required)} 部署，"
                f"当前是 {self._topology}（单节点不支持该能力）；"
                f"请改用副本集或分片集群")
        raise UnsupportedOperation(
            f"{key} 需要 MongoDB >= {minimum}，当前服务端版本 {self.version}；"
            f"如实际版本更高请在 Db.config(version=...) 中声明")

    def features(self) -> Dict[str, Dict[str, Any]]:
        """能力矩阵快照：``{功能: {min, removed_in, supported}}``。"""
        out: Dict[str, Dict[str, Any]] = {}
        for name in sorted(set(MONGO_FEATURES) | set(MONGO_REMOVED)):
            minimum = MONGO_FEATURES.get(name)
            removed = MONGO_REMOVED.get(name)
            out[name] = {
                "min": str(minimum) if minimum else None,
                "removed_in": str(removed) if removed else None,
                "supported": self.supports_feature(name),
            }
        return out

    def dialect_info(self) -> Dict[str, Any]:
        """方言概况（版本 / 拓扑 / 连接来源 / 关键能力）。"""
        return {
            "driver": self.name,
            "version": str(self.version),
            "declared_version": (str(self._declared_version)
                                 if self._declared_version else None),
            "detected_version": (str(self._detected_version)
                                 if self._detected_version else None),
            "topology": self._topology,
            "supported_range": MONGO_SUPPORTED,
            "paramstyle": self.paramstyle,
            "features": self.features(),
        }

    # ------------------------------------------------------------------ #
    # 配置解析
    # ------------------------------------------------------------------ #
    @staticmethod
    def _env_lookup(key: str) -> Optional[str]:
        """按 :data:`MONGO_ENV_KEYS` 的顺序读环境变量（首个非空者胜出）。"""
        for name in MONGO_ENV_KEYS.get(key, ()):
            value = os.environ.get(name)
            if value:
                return value
        return None

    def _resolver_settings(self) -> Dict[str, Any]:
        """调用配置中心回调，返回其提供的连接参数。

        回调签名 ``resolver() -> dict``，可返回 ``uri`` / ``database`` /
        ``user`` / ``password`` / ``host`` / ``port`` / ``options`` 等键。
        回调异常直接上抛（配置中心的故障不应被静默吞掉）。
        """
        resolver = self.config.options.get(MONGO_RESOLVER_KEY)
        if resolver is None:
            return {}
        if not callable(resolver):
            raise InvalidArgumentException(
                f"options[{MONGO_RESOLVER_KEY!r}] 必须是可调用对象"
                f"（签名为 resolver() -> dict），收到: {type(resolver).__name__}")
        data = resolver()
        if not isinstance(data, dict):
            raise InvalidArgumentException(
                f"配置中心回调必须返回 dict，收到: {type(data).__name__}")
        return dict(data)

    def _resolve_settings(self) -> Dict[str, Any]:
        """解析最终连接参数（显式 > 配置中心 > 环境变量 > 默认值）。

        显式配置同时包含 :class:`~tinkpyorm.config.Config` 的专有字段
        （``host`` / ``port`` / ``user`` / ``password`` / ``database``）与
        ``options`` 里的同名键；配置中心与环境变量的键名与之相同。

        缺失关键项时抛 :class:`InvalidArgumentException`，并明确指出
        "缺什么、去哪补"——这是本项目对配置错误的统一风格。
        """
        resolver = self._resolver_settings()
        resolver_options = dict(resolver.get("options") or {})
        explicit_options = dict(self.config.options or {})

        def pick(key: str, *fallbacks: Any) -> Any:
            # 显式配置 -> 配置中心 -> 环境变量 -> 兜底
            explicit = getattr(self.config, key, None)
            # database 的默认值是 ":memory:"（SQLite 语义），对 MongoDB 而言
            # 等同于"未设置"，不能挡住环境变量 / 配置中心的取值。
            if explicit == ":memory:" and key == "database":
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

        database = pick("database")
        uri = explicit_options.get("uri") or resolver_options.get("uri")
        if uri is None:
            uri = pick("uri")
        if uri is not None and str(uri).startswith(("mongodb://", "mongodb+srv://")):
            # database 字段可能被误填成整条 URI（``dsn`` 兼容路径）
            if database and str(database).startswith(("mongodb://", "mongodb+srv://")):
                uri = str(database)
                database = None

        settings: Dict[str, Any] = {
            "uri": uri,
            "database": (database if database and database != ":memory:" else None),
            "user": pick("user"),
            "password": pick("password"),
            "auth_source": pick("auth_source"),
            "host": pick("host", "127.0.0.1"),
            "port": pick("port", 27017),
            "options": {},
        }

        # 显式 options 覆盖配置中心 options
        merged_options: Dict[str, Any] = {}
        merged_options.update(resolver_options)
        merged_options.update(explicit_options)
        merged_options.pop(MONGO_RESOLVER_KEY, None)

        connect_kwargs: Dict[str, Any] = {}
        for key, target in MONGO_CONNECT_KEYS.items():
            if key in merged_options:
                connect_kwargs[target] = merged_options[key]

        # 通用超时配置兜底（秒 -> 毫秒）
        if self.config.connect_timeout is not None:
            millis = int(float(self.config.connect_timeout) * 1000)
            connect_kwargs.setdefault("serverSelectionTimeoutMS", millis)
            connect_kwargs.setdefault("connectTimeoutMS", millis)

        # 写关注
        concern = merged_options.get("write_concern")
        if isinstance(concern, dict):
            for key, value in concern.items():
                target = MONGO_WRITE_CONCERN_KEYS.get(str(key).lower())
                if target is None:
                    raise InvalidArgumentException(
                        f"未知的写关注参数: {key!r}。可选: "
                        + ", ".join(sorted(set(MONGO_WRITE_CONCERN_KEYS))))
                connect_kwargs[target] = value
        elif concern is not None:
            raise InvalidArgumentException(
                "write_concern 必须是 dict（如 {'w': 'majority', 'j': True}）")

        settings["options"] = connect_kwargs
        self._validate_settings(settings)
        return settings

    def _validate_settings(self, settings: Dict[str, Any]) -> None:
        """连接参数完整性校验（缺失即报错，不静默兜底）。

        主机与端口有合理的本地默认值（同 MySQL 驱动的 127.0.0.1:3306），
        但**数据库名没有默认值**——MongoDB 的默认库是 ``test``，
        几乎总是误配，因此缺失时明确报错。
        """
        if not settings.get("database"):
            raise InvalidArgumentException(
                "MongoDB 缺少数据库名：请设置 config 的 database"
                "（或在 options['uri'] 中带上库名），"
                "或提供环境变量 MONGODB_DATABASE")
        if settings.get("password") and not settings.get("user"):
            raise InvalidArgumentException(
                "MongoDB 提供了 password 但缺少 user，请补齐用户名")

    def _build_uri(self, settings: Dict[str, Any]) -> str:
        """由分散参数拼装连接串（已显式给出 uri 时原样使用）。"""
        uri = settings.get("uri")
        if uri:
            return str(uri)
        user = settings.get("user")
        password = settings.get("password")
        auth_source = settings.get("auth_source")
        host = settings.get("host") or "127.0.0.1"
        port = settings.get("port") or 27017
        credentials = ""
        if user:
            auth = quote(str(user), safe="")
            if password:
                auth += ":" + quote(str(password), safe="")
            credentials = auth + "@"
        query = ""
        if auth_source:
            query = "?authSource=" + quote(str(auth_source), safe="")
        return f"mongodb://{credentials}{host}:{port}/{query}"

    # ------------------------------------------------------------------ #
    # 连接
    # ------------------------------------------------------------------ #
    def connect(self) -> Any:
        """建立并返回 ``pymongo.MongoClient``（幂等）。"""
        if self._client is not None:
            return self._client
        try:
            from pymongo import MongoClient
        except ImportError as exc:  # pragma: no cover - 取决于环境
            raise DriverNotAvailable(
                "MongoDB 驱动需要 pymongo：pip install pymongo "
                "（或 pip install tinkpyorm[mongo]）") from exc

        settings = self._resolve_settings()
        uri = self._build_uri(settings)
        kwargs = dict(settings["options"])
        try:
            self._client = MongoClient(uri, **kwargs)
        except Exception as exc:
            raise translate_mongo_error(exc) from exc
        try:
            self._detect_version()
        except BaseException:
            # 探测失败（服务端不可达等）时回滚连接状态，避免留下
            # "看似已连接、实则不可用"的半成品客户端。
            self.close()
            raise
        return self._client

    def _detect_version(self) -> None:
        """探测服务端版本（失败不影响连接本身）。

        两条路径，**精确优先**：

        1. ``buildInfo``（``server_info()``）—— 返回服务端**真实版本号**
           （如 ``8.3.13``），但需要认证/权限，未授权时会失败；
        2. ``hello`` 的 ``maxWireVersion`` —— **无需权限**，但只能反查出
           **该版本线的下界**（8.x 共用 wire 25，一律得到 8.0.0）。

        因此有权限时以 ``buildInfo`` 为准；无权限时退化为下界（对能力门禁
        是保守的，不会误放行）。
        """
        version = self._version_from_buildinfo()
        if version is None:
            version = self._version_from_wire_command()
        else:
            # buildInfo 不含拓扑信息，需单独用 hello 探测一次
            self._detect_topology()
        if version is None:
            # 两条路径都拿不到版本，且 hello 也没通 —— 服务端不可达。
            # pymongo 的 MongoClient 是惰性连接，不在这里抛就会把错误
            # 推迟到第一次查询，让"配置/连通性"问题难以定位。
            raise ConnectionFailedError(
                f"MongoDB 连接失败：无法连接 {self._describe_target()}"
                f"（hello 命令无响应）；请检查地址、端口、网络与认证配置")
        self._detected_version = version
        self._check_declared_version()

    def _describe_target(self) -> str:
        """连接目标的可读描述（不含凭据）。"""
        try:
            settings = self._resolve_settings()
        except Exception:
            return "目标地址"
        if settings.get("uri"):
            uri = str(settings["uri"])
            # 去掉 user:pass@ 片段，避免密码进入日志/异常信息
            if "@" in uri:
                head, tail = uri.split("@", 1)
                scheme = head.split("://", 1)[0] if "://" in head else "mongodb"
                uri = f"{scheme}://***@{tail}"
            return uri
        return f"{settings.get('host')}:{settings.get('port')}"

    def _detect_topology(self) -> None:
        """用 ``hello`` 识别部署拓扑（失败不阻断连接）。"""
        for command in ("hello", "isMaster"):
            try:
                info = self._client.admin.command(command)
            except Exception:
                continue
            if isinstance(info, dict):
                self._topology = self._classify_topology(info)
            return

    def _version_from_buildinfo(self) -> Optional[Version]:
        """用 ``buildInfo`` 取精确版本（需权限，失败返回 None）。"""
        try:
            info = self._client.server_info()
        except Exception:
            return None
        raw = info.get("version") if isinstance(info, dict) else None
        if not raw:
            return None
        try:
            return parse_version(str(raw), product="mongodb")
        except InvalidArgumentException:
            # 版本号形态超出支持范围时不阻断连接，退化为 wire 下界
            # （parse_version 的唯一异常类型；其他异常应向上暴露）
            return None

    def _version_from_wire_command(self) -> Optional[Version]:
        """用 ``hello`` / ``isMaster`` 的 ``maxWireVersion`` 反查版本下界。

        顺带识别部署拓扑（``setName`` 有副本集名、``msg=isdbgrid`` 为分片
        集群、两者皆无则是单节点），供 :attr:`topology` 与拓扑门禁使用。
        """
        info = None
        for command in ("hello", "isMaster"):
            try:
                info = self._client.admin.command(command)
                break
            except Exception:
                continue
        if not isinstance(info, dict):
            return None
        if self._topology is None:
            self._topology = self._classify_topology(info)
        wire = info.get("maxWireVersion")
        if not isinstance(wire, int):
            return None
        return self._version_from_wire(wire)

    @staticmethod
    def _classify_topology(info: Dict[str, Any]) -> str:
        """由 ``hello`` 应答判定部署拓扑。"""
        if not isinstance(info, dict):
            return "standalone"
        if info.get("msg") == "isdbgrid":
            return "sharded"
        if info.get("setName"):
            return "replica_set"
        return "standalone"

    @staticmethod
    def _version_from_wire(wire: int) -> Version:
        """``maxWireVersion`` -> 该线的最低服务端版本。"""
        for threshold, version in _WIRE_VERSION_TABLE:
            if wire >= threshold:
                return version
        return Version(3, 6, 0)

    def _check_declared_version(self) -> None:
        """声明版本高于实际服务端时立即报错（配置与现实不符）。"""
        if self._declared_version is None or self._detected_version is None:
            return
        if self._declared_version > self._detected_version:
            raise InvalidArgumentException(
                f"声明的 MongoDB 版本 {self._declared_version} 高于实际服务端版本 "
                f"{self._detected_version}；请修正 Db.config(version=...)")

    def close(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            finally:
                self._client = None
                self._detected_version = None

    def ping(self) -> bool:
        """连接是否可用（执行 ``ping`` 命令；未连接时先建立连接）。"""
        try:
            if self._client is None:
                self.connect()
            self._client.admin.command("ping")
            return True
        except Exception:
            return False

    def is_connected(self) -> bool:
        return self._client is not None

    @property
    def raw_connection(self) -> Any:
        """原生 ``MongoClient``（逃生舱）。"""
        return self.connect()

    # ------------------------------------------------------------------ #
    # 集合访问（服务层入口）
    # ------------------------------------------------------------------ #
    def database(self, name: Optional[str] = None) -> Any:
        """取 ``pymongo.Database``；``name`` 为空时用配置中的库名。"""
        settings = self._resolve_settings()
        db_name = name or settings.get("database")
        if not db_name:
            raise InvalidArgumentException(
                "MongoDB 缺少数据库名：请设置 config 的 database")
        return self.connect()[db_name]

    def collection(self, name: str, database: Optional[str] = None) -> Any:
        """取 ``pymongo.Collection``。"""
        if not isinstance(name, str) or not name.strip():
            raise InvalidArgumentException(f"非法集合名: {name!r}")
        return self.database(database)[name]

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<MongoDBDriver {self.config.database} v{self.version}>"
