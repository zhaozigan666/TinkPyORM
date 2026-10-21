"""Redis 驱动的离线测试（v0.13.0）。

**全部离线**：用内存里的假 ``redis`` 模块替换第三方客户端，因此不依赖
真实 Redis、也不要求安装 redis-py，可在任意 Python 版本上跑。

覆盖范围
========

1. 版本解析与校验（含 Redis 的"发布线"语义：6.0 / 6.2 / 7.0 / 7.2 / 7.4）；
2. 能力矩阵与版本门禁（含部署形态门禁：集群下 ``select_db`` 不可用）；
3. 配置解析优先级（显式 > 配置中心 > 环境变量 > 默认值）与参数校验；
4. 异常翻译（``redis.exceptions.*`` -> 项目异常）；
5. 键前缀与逻辑键名还原、键名/值编码校验；
6. 各数据类型命令的**参数构造**（假客户端记录收到的调用）；
7. 流水线的键前缀规则与未知命令拒绝；
8. 发布订阅的频道前缀；
9. 安全护栏（``keys()`` 的键数阈值、``clear()`` 拒绝整库模式）。
"""
import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from tinkpyorm import Db, RedisPipeline, RedisStore  # noqa: E402
from tinkpyorm.drivers import UnsupportedOperation  # noqa: E402
from tinkpyorm.drivers.redis import (  # noqa: E402
    REDIS_FEATURES, REDIS_TOPOLOGY_FEATURES, RedisDriver, translate_redis_error,
)
from tinkpyorm.exceptions import (  # noqa: E402
    ConnectionFailedError, DuplicateKeyError, InvalidArgumentException,
    QueryError,
)
from tinkpyorm.versioning import (  # noqa: E402
    DEFAULT_REDIS_VERSION, REDIS_SUPPORTED, Version, parse_version,
)


# ---------------------------------------------------------------------- #
# 假 redis-py
# ---------------------------------------------------------------------- #
#: 各命令的缺省返回值（未列出的返回 None）
_DEFAULTS = {
    "ping": True,
    "dbsize": 0,
    "get": None,
    "mget": [],
    "getdel": None,
    "getex": None,
    "set": True,
    "setex": True,
    "psetex": True,
    "setnx": True,
    "mset": True,
    "append": 0,
    "strlen": 0,
    "incrby": 1,
    "decrby": -1,
    "incrbyfloat": 1.5,
    "delete": 0,
    "unlink": 0,
    "exists": 0,
    "touch": 0,
    "type": "none",
    "rename": True,
    "renamenx": True,
    "expire": True,
    "pexpire": True,
    "expireat": True,
    "persist": True,
    "ttl": -1,
    "pttl": -1,
    "expiretime": -1,
    "randomkey": None,
    "hset": 1,
    "hsetnx": True,
    "hget": None,
    "hmget": [],
    "hgetall": {},
    "hdel": 0,
    "hexists": False,
    "hkeys": [],
    "hvals": [],
    "hlen": 0,
    "hincrby": 1,
    "hincrbyfloat": 1.0,
    "hrandfield": None,
    "hexpire": [1],
    "httl": [10],
    "hpersist": [1],
    "lpush": 1,
    "rpush": 1,
    "lpop": None,
    "rpop": None,
    "lrange": [],
    "llen": 0,
    "lindex": None,
    "lset": True,
    "lrem": 0,
    "ltrim": True,
    "lpos": None,
    "lmove": None,
    "lmpop": None,
    "blpop": None,
    "sadd": 1,
    "srem": 0,
    "smembers": [],
    "sismember": False,
    "smismember": [],
    "scard": 0,
    "spop": None,
    "srandmember": None,
    "sunion": [],
    "sinter": [],
    "sdiff": [],
    "sintercard": 0,
    "zadd": 1,
    "zrange": [],
    "zrevrange": [],
    "zrangebyscore": [],
    "zscore": None,
    "zmscore": [],
    "zrank": None,
    "zrevrank": None,
    "zincrby": 1.0,
    "zrem": 0,
    "zcard": 0,
    "zcount": 0,
    "zpopmin": [],
    "zpopmax": [],
    "zremrangebyscore": 0,
    "publish": 0,
    "config_get": {},
}


class _FakePipeline:
    """记录入队命令的假流水线。"""

    def __init__(self, client, transaction=True):
        self._client = client
        self.transaction = transaction
        self.commands = []
        self.results = []
        self.reset_called = False

    def _record(self, name, args, kwargs):
        self.commands.append((name, args, kwargs))

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)

        def call(*args, **kwargs):
            self._record(name, args, kwargs)
            return self

        return call

    def execute(self, raise_on_error=True):
        if self._client.fail_on_execute is not None:
            raise self._client.fail_on_execute
        self._client.calls.append(("pipeline.execute", (), {}))
        return self.results or [True] * len(self.commands)

    def reset(self):
        self.reset_called = True
        self.commands = []

    def __len__(self):
        return len(self.commands)


class _FakePubSub:
    def __init__(self, client):
        self._client = client
        self.subscribed = []
        self.psubscribed = []
        self.closed = False

    def subscribe(self, *channels):
        self.subscribed.extend(channels)

    def psubscribe(self, *patterns):
        self.psubscribed.extend(patterns)

    def get_message(self, timeout=1.0, ignore_subscribe_messages=True):
        return {"type": "message", "channel": "ch", "data": "x"}

    def listen(self):
        yield {"type": "message", "channel": "ch", "data": "x"}

    def close(self):
        self.closed = True


class _FakeRedis:
    """假 ``redis.Redis``：记录连接参数与命令调用，返回值可预设。"""

    #: 由 _install_fake_redis() 设置
    server_version = "8.0.0"
    topology = "standalone"
    role = "master"
    info_raises = False
    backend_failure = None
    #: 握手（PING）时抛出的异常，用于模拟 SELECT 失败
    ping_error = None

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.url = None
        self.calls = []
        self.closed = False
        self.fail_on_execute = _FakeRedis.backend_failure
        self._responses = dict(_FakeRedis.responses)
        self.info_raises = _FakeRedis.info_raises

    # 记录型 __getattr__：任何命令都会被记录并按预设返回
    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)

        def call(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            if name == "info":
                return self._info()
            if name == "pipeline":
                return _FakePipeline(self, kwargs.get("transaction", True))
            if name == "pubsub":
                return _FakePubSub(self)
            if self.fail_on_execute is not None:
                # 模拟服务端拒绝执行（WRONGTYPE / OOM / 只读副本 等）
                raise self.fail_on_execute
            if name in self._responses:
                return self._responses[name]
            return _DEFAULTS.get(name)

        return call

    def _info(self):
        if self.info_raises:
            raise RuntimeError("INFO 被 ACL 拒绝")
        data = {"redis_version": _FakeRedis.server_version,
                "redis_mode": "standalone",
                "role": _FakeRedis.role,
                "cluster_enabled": 1 if _FakeRedis.topology == "cluster" else 0}
        return data

    def ping(self):
        self.calls.append(("ping", (), {}))
        if _FakeRedis.ping_error is not None:
            raise _FakeRedis.ping_error
        return True

    def close(self):
        self.closed = True

    @classmethod
    def from_url(cls, url, **kwargs):
        instance = cls(**kwargs)
        instance.url = url
        return instance

    #: 预设返回值（安装时复制到实例）
    responses = {}


def _install_fake_redis(server_version="8.0.0", topology="standalone",
                        role="master", info_raises=False,
                        responses=None, backend_failure=None,
                        ping_error=None, databases=None):
    """把假 ``redis`` 模块装进 sys.modules（测试结束由 tearDown 还原）。"""
    _FakeRedis.server_version = server_version
    _FakeRedis.topology = topology
    _FakeRedis.role = role
    _FakeRedis.info_raises = info_raises
    _FakeRedis.responses = dict(responses or {})
    _FakeRedis.backend_failure = backend_failure
    _FakeRedis.ping_error = ping_error
    if databases is not None:
        # INFO 不含 databases（真实服务端如此），只能由 CONFIG GET 提供
        _FakeRedis.responses = dict(_FakeRedis.responses or {})
        _FakeRedis.responses["config_get"] = {"databases": str(databases)}

    module = types.ModuleType("redis")
    module.Redis = _FakeRedis
    module.__version__ = "8.1.0-fake"
    existing = _ORIGINAL_REDIS
    sys.modules["redis"] = module
    return existing


_ORIGINAL_REDIS = sys.modules.get("redis", "MISSING")


def _restore_redis():
    if _ORIGINAL_REDIS == "MISSING":
        sys.modules.pop("redis", None)
    else:
        sys.modules["redis"] = _ORIGINAL_REDIS


def _reset_db():
    """重置 Db 的全局状态。

    ``Db._prefix`` 是类级状态，只在配置显式给出 ``prefix`` 时被改写，
    测试之间必须手动清空——否则上一个用例的命名空间会串到下一个用例。
    """
    Db.close()
    Db._prefix = ""


def _config(version=None, **options):
    """切换到 Redis 配置（假客户端），返回 Connection。

    假服务端版本默认跟随 ``version``（未声明时 6.0.0）——否则连接后
    ``driver.version`` 会用探测值覆盖声明值，版本门禁测试就失去意义。
    """
    fake = options.pop("fake", {})
    fake.setdefault("server_version", version or "6.0.0")
    _install_fake_redis(**fake)
    _reset_db()
    data = {"type": "redis", "host": "127.0.0.1", "port": 6379,
            "password": "pwd", "database": "0"}
    if version is not None:
        data["version"] = version
    data.update(options)
    return Db.config(data)


def _clear_env():
    for name in list(os.environ):
        if name.startswith("REDIS_"):
            del os.environ[name]


# ---------------------------------------------------------------------- #
# 版本
# ---------------------------------------------------------------------- #
class TestRedisVersion(unittest.TestCase):
    def test_default_version(self):
        self.assertEqual(DEFAULT_REDIS_VERSION, "7.0.0")
        self.assertEqual(parse_version(None, product="redis"), Version(7, 0, 0))

    def test_parse_release_lines(self):
        for text, expected in [("8.10.2", (8, 10, 2)), ("7.4", (7, 4, 0)),
                               ("6.2.14", (6, 2, 14)), ("5.0", (5, 0, 0))]:
            self.assertEqual(parse_version(text, product="redis"),
                             Version(*expected))

    def test_accepts_int_and_tuple(self):
        self.assertEqual(parse_version(7, product="redis"), Version(7, 0, 0))
        self.assertEqual(parse_version((7, 2), product="redis"),
                         Version(7, 2, 0))
        self.assertEqual(parse_version(7.4, product="redis"),
                         Version(7, 4, 0))

    def test_rejects_unknown_release_lines(self):
        for text in ("3.2", "6.1", "7.1", "7.3", "9.0", "abc"):
            with self.assertRaises(InvalidArgumentException):
                parse_version(text, product="redis")

    def test_rejects_bool(self):
        with self.assertRaises(InvalidArgumentException):
            parse_version(True, product="redis")

    def test_supported_range_text_mentions_lines(self):
        for line in ("5.0", "6.2", "7.4", "8.x"):
            self.assertIn(line, REDIS_SUPPORTED)

    def test_at_least(self):
        self.assertTrue(Version(8, 0, 0).at_least("7.4"))
        self.assertFalse(Version(6, 2, 0).at_least("7.0"))


# ---------------------------------------------------------------------- #
# 能力矩阵 / 门禁
# ---------------------------------------------------------------------- #
class TestFeatureMatrix(unittest.TestCase):
    def setUp(self):
        _clear_env()

    def tearDown(self):
        _reset_db()
        _restore_redis()

    def _driver(self, version):
        conn = _config(version=version)
        driver = conn.driver
        driver.connect()
        return driver

    def test_6_0_features(self):
        d = self._driver("6.0")
        self.assertTrue(d.supports_feature("acl"))
        self.assertFalse(d.supports_feature("getdel"))
        self.assertFalse(d.supports_feature("lmove"))
        self.assertFalse(d.supports_feature("function"))

    def test_6_2_features(self):
        d = self._driver("6.2")
        self.assertTrue(d.supports_feature("getdel"))
        self.assertTrue(d.supports_feature("lmove"))
        self.assertTrue(d.supports_feature("set_get"))
        self.assertFalse(d.supports_feature("lmpop"))
        self.assertFalse(d.supports_feature("hash_field_expiry"))

    def test_7_0_features(self):
        d = self._driver("7.0")
        self.assertTrue(d.supports_feature("lmpop"))
        self.assertTrue(d.supports_feature("sintercard"))
        self.assertTrue(d.supports_feature("expire_nx_xx_gt_lt"))
        self.assertFalse(d.supports_feature("hash_field_expiry"))
        self.assertFalse(d.supports_feature("waitaof"))

    def test_7_4_features(self):
        d = self._driver("7.4")
        self.assertTrue(d.supports_feature("hash_field_expiry"))
        self.assertTrue(d.supports_feature("waitaof"))
        self.assertFalse(d.supports_feature("hash_field_getex"))

    def test_8_0_features(self):
        d = self._driver("8.0")
        self.assertTrue(d.supports_feature("hash_field_getex"))
        self.assertTrue(d.supports_feature("vector_set"))

    def test_unknown_feature_rejected(self):
        d = self._driver("7.0")
        with self.assertRaises(InvalidArgumentException) as ctx:
            d.supports_feature("not_a_feature")
        self.assertIn("未知的 Redis 能力", str(ctx.exception))

    def test_require_feature_message_has_version(self):
        d = self._driver("6.0")
        with self.assertRaises(UnsupportedOperation) as ctx:
            d.require_feature("lmpop")
        message = str(ctx.exception)
        self.assertIn("7.0.0", message)
        self.assertIn("6.0.0", message)

    def test_enabled_feature_does_not_raise(self):
        d = self._driver("8.0")
        d.require_feature("lmpop")   # 不抛即通过

    def test_topology_gate_in_cluster(self):
        """集群只有 db 0，``select_db`` 必须被判为不可用。"""
        _install_fake_redis(server_version="7.2.0", topology="cluster")
        _reset_db()
        conn = Db.config({"type": "redis", "host": "h", "password": "p"})
        driver = conn.driver
        driver.connect()
        self.assertEqual(driver.topology, "cluster")
        self.assertFalse(driver.supports_feature("select_db"))
        with self.assertRaises(UnsupportedOperation) as ctx:
            driver.require_feature("select_db")
        self.assertIn("cluster", str(ctx.exception))

    def test_standalone_allows_select_db(self):
        d = self._driver("7.2")
        self.assertEqual(d.topology, "standalone")
        self.assertTrue(d.supports_feature("select_db"))

    def test_topology_unknown_does_not_gate(self):
        """INFO 拿不到时不猜拓扑，只按版本判断（避免误杀）。"""
        _install_fake_redis(server_version="7.2.0", info_raises=True)
        _reset_db()
        conn = Db.config({"type": "redis", "host": "h", "password": "p",
                          "version": "7.2"})
        driver = conn.driver
        driver.connect()
        self.assertEqual(driver.topology, "standalone")   # 兜底
        self.assertTrue(driver.supports_feature("select_db"))

    def test_declared_version_higher_than_server(self):
        _install_fake_redis(server_version="6.2.0")
        _reset_db()
        conn = Db.config({"type": "redis", "host": "h", "password": "p",
                          "version": "8.0"})
        with self.assertRaises(InvalidArgumentException) as ctx:
            conn.driver.connect()
        self.assertIn("高于实际服务端版本", str(ctx.exception))

    def test_declared_version_lower_is_allowed(self):
        _install_fake_redis(server_version="8.10.2")
        _reset_db()
        conn = Db.config({"type": "redis", "host": "h", "password": "p",
                          "version": "7.0"})
        driver = conn.driver
        driver.connect()
        self.assertEqual(driver.declared_version, Version(7, 0, 0))
        self.assertEqual(driver.server_version, Version(8, 10, 2))

    def test_detect_version_returns_server_version(self):
        d = self._driver(None)          # 假服务端默认 6.0.0
        self.assertEqual(d.detect_version(), Version(6, 0, 0))
        self.assertEqual(d.version, Version(6, 0, 0))

    def test_features_snapshot(self):
        d = self._driver("8.0")
        snapshot = d.features()
        self.assertIn("lmpop", snapshot)
        self.assertTrue(snapshot["lmpop"]["supported"])
        self.assertEqual(snapshot["lmpop"]["min"], "7.0.0")

    def test_dialect_info(self):
        d = self._driver("8.0")
        info = d.dialect_info()
        self.assertEqual(info["driver"], "redis")
        self.assertEqual(info["version"], "8.0.0")
        self.assertEqual(info["topology"], "standalone")
        self.assertFalse(info["is_replica"])

    def test_matrix_covers_topology_features(self):
        for name in REDIS_TOPOLOGY_FEATURES:
            self.assertIn(name, REDIS_FEATURES)


# ---------------------------------------------------------------------- #
# 配置解析
# ---------------------------------------------------------------------- #
class TestRedisConfig(unittest.TestCase):
    def setUp(self):
        _clear_env()
        _install_fake_redis()

    def tearDown(self):
        _reset_db()
        _restore_redis()
        _clear_env()

    def _settings(self, config):
        _reset_db()
        conn = Db.config(config)
        return conn.driver._resolve_settings()

    def test_explicit_config_wins(self):
        settings = self._settings({
            "type": "redis", "host": "1.2.3.4", "port": 7000,
            "password": "p", "database": "3"})
        self.assertEqual(settings["host"], "1.2.3.4")
        self.assertEqual(settings["port"], 7000)
        self.assertEqual(settings["database"], 3)

    def test_env_fallback(self):
        os.environ.update({"REDIS_HOST": "env-host", "REDIS_PORT": "6380",
                           "REDIS_PASSWORD": "env-pwd", "REDIS_DB": "5"})
        settings = self._settings({"type": "redis"})
        self.assertEqual(settings["host"], "env-host")
        self.assertEqual(settings["port"], 6380)
        self.assertEqual(settings["password"], "env-pwd")
        self.assertEqual(settings["database"], 5)

    def test_resolver_beats_env(self):
        os.environ["REDIS_HOST"] = "env-host"
        settings = self._settings({
            "type": "redis",
            "options": {"config_resolver": lambda: {
                "host": "center-host", "port": 6390, "password": "c"}}})
        self.assertEqual(settings["host"], "center-host")
        self.assertEqual(settings["port"], 6390)

    def test_explicit_beats_resolver(self):
        settings = self._settings({
            "type": "redis", "host": "explicit-host",
            "options": {"config_resolver": lambda: {"host": "center-host"}}})
        self.assertEqual(settings["host"], "explicit-host")

    def test_defaults(self):
        settings = self._settings({"type": "redis", "password": "p"})
        self.assertEqual(settings["host"], "127.0.0.1")
        self.assertEqual(settings["port"], 6379)
        self.assertEqual(settings["database"], 0)

    def test_memory_database_is_ignored(self):
        """``Config`` 的默认 ``:memory:`` 对 Redis 等于"未设置"。"""
        settings = self._settings({"type": "redis", "password": "p"})
        self.assertEqual(settings["database"], 0)

    def test_url_from_options(self):
        settings = self._settings({
            "type": "redis",
            "options": {"url": "redis://:p@h:6380/2"}})
        self.assertEqual(settings["url"], "redis://:p@h:6380/2")

    def test_url_from_env(self):
        os.environ["REDIS_URL"] = "rediss://:p@h:6380/1"
        settings = self._settings({"type": "redis"})
        self.assertEqual(settings["url"], "rediss://:p@h:6380/1")

    def test_invalid_port(self):
        with self.assertRaises(InvalidArgumentException):
            self._settings({"type": "redis", "port": 70000, "password": "p"})

    def test_invalid_database(self):
        with self.assertRaises(InvalidArgumentException) as ctx:
            self._settings({"type": "redis", "database": "app",
                            "password": "p"})
        self.assertIn("db 序号", str(ctx.exception))

    def test_resolver_must_be_callable(self):
        with self.assertRaises(InvalidArgumentException):
            self._settings({"type": "redis", "options": {
                "config_resolver": "nope"}})

    def test_resolver_must_return_dict(self):
        with self.assertRaises(InvalidArgumentException):
            self._settings({"type": "redis", "options": {
                "config_resolver": lambda: ["x"]}})

    def test_connect_builds_url(self):
        _reset_db()
        conn = Db.config({"type": "redis", "host": "h", "port": 6380,
                          "password": "p", "database": 2})
        client = conn.driver.connect()
        self.assertEqual(client.url, "redis://:p@h:6380/2")
        # URL 路径不再重复传 db/password（否则与 URL 冲突）
        self.assertNotIn("db", client.kwargs)
        self.assertNotIn("password", client.kwargs)

    def test_connect_uses_url_directly(self):
        _reset_db()
        conn = Db.config({"type": "redis",
                          "options": {"url": "redis://:p@h:6380/2"}})
        client = conn.driver.connect()
        self.assertEqual(client.url, "redis://:p@h:6380/2")

    def test_decode_responses_default_true(self):
        settings = self._settings({"type": "redis", "password": "p"})
        self.assertTrue(settings["options"]["decode_responses"])

    def test_connect_timeout_default(self):
        settings = self._settings({"type": "redis", "password": "p"})
        self.assertEqual(settings["options"]["socket_connect_timeout"], 5.0)

    def test_unknown_option_is_not_passed_through(self):
        """未知 options 不透传给 redis-py（避免 TypeError）。"""
        settings = self._settings({"type": "redis", "password": "p",
                                   "options": {"bogus_option": 1}})
        self.assertNotIn("bogus_option", settings["options"])


# ---------------------------------------------------------------------- #
# 异常翻译
# ---------------------------------------------------------------------- #
class TestRedisErrors(unittest.TestCase):
    def _err(self, name, message="boom", **attrs):
        cls = type(name, (Exception,), {"__name__": name})
        exc = cls(message)
        for key, value in attrs.items():
            setattr(exc, key, value)
        return exc

    def test_connection_error(self):
        self.assertIsInstance(translate_redis_error(
            self._err("ConnectionError")), ConnectionFailedError)

    def test_timeout_error(self):
        self.assertIsInstance(translate_redis_error(
            self._err("TimeoutError")), ConnectionFailedError)

    def test_authentication_error(self):
        err = translate_redis_error(self._err("AuthenticationError", "WRONGPASS"))
        self.assertIsInstance(err, ConnectionFailedError)
        self.assertIn("Redis 连接失败", str(err))

    def test_busy_loading(self):
        self.assertIsInstance(translate_redis_error(
            self._err("BusyLoadingError")), ConnectionFailedError)

    def test_data_error(self):
        self.assertIsInstance(translate_redis_error(
            self._err("DataError")), InvalidArgumentException)

    def test_response_error(self):
        self.assertIsInstance(translate_redis_error(
            self._err("ResponseError", "WRONGTYPE")), QueryError)

    def test_readonly_error(self):
        self.assertIsInstance(translate_redis_error(
            self._err("ReadOnlyError")), QueryError)

    def test_cluster_cross_slot(self):
        self.assertIsInstance(translate_redis_error(
            self._err("ClusterCrossSlotError")), QueryError)

    def test_unknown_error_defaults_to_query_error(self):
        err = translate_redis_error(ValueError("weird"))
        self.assertIsInstance(err, QueryError)
        self.assertIn("Redis 操作失败", str(err))

    def test_orm_error_passthrough(self):
        original = DuplicateKeyError("x")
        self.assertIs(translate_redis_error(original), original)

    def test_connect_failure_translated_and_rolled_back(self):
        """握手失败必须是 ConnectionFailedError，且不留半成品连接。"""
        boom = self._err("ConnectionError", "down")

        class BadPing(_FakeRedis):
            def ping(self_inner):
                raise boom

        _install_fake_redis()
        sys.modules["redis"].Redis = BadPing
        _reset_db()
        conn = Db.config({"type": "redis", "host": "h", "password": "p"})
        with self.assertRaises(ConnectionFailedError):
            conn.driver.connect()
        self.assertFalse(conn.driver.is_connected())

    def test_command_error_translated_at_store_level(self):
        _install_fake_redis()
        _reset_db()
        Db.config({"type": "redis", "host": "h", "password": "p"})
        store = Db.redis()
        # 建连后再注入故障：否则 connect() 的 PING 就会先失败
        store.client().fail_on_execute = self._err("ResponseError", "WRONGTYPE")
        with self.assertRaises(QueryError):
            store.get("k")

# ---------------------------------------------------------------------- #
# 键与值
# ---------------------------------------------------------------------- #
class TestKeyAndValue(unittest.TestCase):
    def setUp(self):
        _clear_env()
        _install_fake_redis()
        _reset_db()
        Db.config({"type": "redis", "host": "h", "password": "p"})
        self.store = Db.redis("app:")

    def tearDown(self):
        _reset_db()
        _restore_redis()

    def test_key_prefix(self):
        self.assertEqual(self.store.key("user:1"), "app:user:1")

    def test_logical_strips_prefix(self):
        self.assertEqual(self.store.logical("app:user:1"), "user:1")
        self.assertEqual(self.store.logical("other"), "other")

    def test_invalid_key_types(self):
        for bad in (None, "", {}, [], True):
            with self.assertRaises(InvalidArgumentException):
                self.store.key(bad)

    def test_int_key_allowed(self):
        self.assertEqual(self.store.key(42), "app:42")

    def test_container_value_rejected(self):
        with self.assertRaises(InvalidArgumentException) as ctx:
            self.store.set("k", {"a": 1})
        self.assertIn("set_json", str(ctx.exception))

    def test_none_value_rejected(self):
        with self.assertRaises(InvalidArgumentException):
            self.store.set("k", None)

    def test_bool_value_encoded(self):
        self.store.set("k", True)
        call = [c for c in self.store.client().calls if c[0] == "set"][-1]
        self.assertEqual(call[1][1], "1")

    def test_float_value_encoded(self):
        self.store.set("k", 1.5)
        call = [c for c in self.store.client().calls if c[0] == "set"][-1]
        self.assertEqual(call[1][1], "1.5")

    def test_set_json_roundtrip(self):
        client = self.store.client()
        client._responses["get"] = '{"a": [1, 2], "b": "中文"}'
        self.store.set_json("j", {"a": [1, 2], "b": "中文"})
        self.assertEqual(self.store.get_json("j"), {"a": [1, 2], "b": "中文"})

    def test_get_json_missing_raises_by_default(self):
        with self.assertRaises(QueryError):
            self.store.get_json("missing")

    def test_get_json_missing_returns_default(self):
        self.assertIsNone(self.store.get_json("missing", default=None))

    def test_get_json_invalid_payload(self):
        client = self.store.client()
        client._responses["get"] = "not-json"
        with self.assertRaises(QueryError):
            self.store.get_json("k")


# ---------------------------------------------------------------------- #
# 命令参数构造
# ---------------------------------------------------------------------- #
class TestCommands(unittest.TestCase):
    def setUp(self):
        _clear_env()
        # 假服务端按 6.0 报版本：这样才能验证 6.2 / 7.0 的门禁确实生效
        _install_fake_redis(server_version="6.0.0")
        _reset_db()
        Db.config({"type": "redis", "host": "h", "password": "p"})
        self.store = Db.redis("app:")
        self.client = self.store.client()

    def tearDown(self):
        _reset_db()
        _restore_redis()

    def _last(self, name):
        calls = [c for c in self.client.calls if c[0] == name]
        return calls[-1] if calls else None

    def test_set_arguments(self):
        self.store.set("k", "v", ex=60, nx=True)
        name, args, kwargs = self._last("set")
        self.assertEqual(args, ("app:k", "v"))
        self.assertEqual(kwargs["ex"], 60)
        self.assertTrue(kwargs["nx"])

    def test_set_nx_xx_conflict(self):
        with self.assertRaises(InvalidArgumentException):
            self.store.set("k", "v", nx=True, xx=True)

    def test_set_ex_px_conflict(self):
        with self.assertRaises(InvalidArgumentException):
            self.store.set("k", "v", ex=1, px=1)

    def test_get_default(self):
        self.assertEqual(self.store.get("missing", default="fallback"),
                         "fallback")

    def test_getdel_gated(self):
        with self.assertRaises(UnsupportedOperation):
            self.store.getdel("k")

    def test_getdel_allowed_on_newer(self):
        _install_fake_redis(server_version="7.0.0")
        _reset_db()
        Db.config({"type": "redis", "host": "h", "password": "p"})
        store = Db.redis("app:")
        store.getdel("k")
        self.assertIsNotNone([c for c in store.client().calls
                              if c[0] == "getdel"])

    def test_mget_prefixes_all(self):
        self.store.mget("a", "b")
        name, args, _ = self._last("mget")
        self.assertEqual(args[0], ["app:a", "app:b"])

    def test_delete_prefixes_all(self):
        self.store.delete("a", "b")
        name, args, _ = self._last("delete")
        self.assertEqual(args, ("app:a", "app:b"))

    def test_hset_mapping(self):
        self.store.hset("h", {"name": "张三", "age": 25})
        name, args, kwargs = self._last("hset")
        self.assertEqual(args[0], "app:h")
        self.assertEqual(kwargs["mapping"], {"name": "张三", "age": "25"})

    def test_hset_kwargs_form(self):
        self.store.hset("h", name="张三")
        name, args, kwargs = self._last("hset")
        self.assertEqual(kwargs["mapping"], {"name": "张三"})

    def test_hset_empty_rejected(self):
        with self.assertRaises(InvalidArgumentException):
            self.store.hset("h", {})

    def test_zadd_scores_coerced(self):
        self.store.zadd("z", {"a": 1, "b": "2.5"})
        name, args, kwargs = self._last("zadd")
        self.assertEqual(args[0], "app:z")
        self.assertEqual(args[1], {"a": 1.0, "b": 2.5})

    def test_zadd_invalid_score(self):
        with self.assertRaises(InvalidArgumentException):
            self.store.zadd("z", {"a": "not-a-number"})

    def test_zadd_gt_gated(self):
        with self.assertRaises(UnsupportedOperation):
            self.store.zadd("z", {"a": 1}, gt=True)

    def test_expire_flags_gated(self):
        with self.assertRaises(UnsupportedOperation):
            self.store.expire("k", 10, nx=True)

    def test_expire_plain_allowed(self):
        self.store.expire("k", 10)
        name, args, kwargs = self._last("expire")
        self.assertEqual(args, ("app:k", 10))

    def test_incr_uses_incrby(self):
        self.store.incr("c", 5)
        name, args, _ = self._last("incrby")
        self.assertEqual(args, ("app:c", 5))

    def test_lpush_encodes_values(self):
        self.store.lpush("l", "a", 1, True)
        name, args, _ = self._last("lpush")
        self.assertEqual(args, ("app:l", "a", "1", "1"))

    def test_sadd_members(self):
        self.store.sadd("s", "x", 2)
        name, args, _ = self._last("sadd")
        self.assertEqual(args, ("app:s", "x", "2"))

    def test_sintercard_numkeys(self):
        """SINTERCARD 需 7.0，故本用例切到 7.0 的假服务端。"""
        _install_fake_redis(server_version="7.0.0")
        _reset_db()
        Db.config({"type": "redis", "host": "h", "password": "p"})
        store = Db.redis("app:")
        store.sintercard(["s1", "s2"], limit=10)
        calls = [c for c in store.client().calls if c[0] == "sintercard"]
        # redis-py 签名：sintercard(numkeys, keys, limit=0)
        self.assertEqual(calls[-1][1], (2, ["app:s1", "app:s2"]))
        self.assertEqual(calls[-1][2]["limit"], 10)

    def test_scan_iter_prefixes_and_strips(self):
        self.client._responses["scan"] = (0, ["app:a", "app:b"])
        self.assertEqual(sorted(self.store.scan_iter(match="*")), ["a", "b"])
        name, args, kwargs = self._last("scan")
        self.assertEqual(kwargs["match"], "app:*")

    def test_keys_refuses_huge_db(self):
        self.client._responses["dbsize"] = 50000
        with self.assertRaises(QueryError) as ctx:
            self.store.keys("*")
        self.assertIn("scan_iter", str(ctx.exception))

    def test_clear_rejects_global_pattern(self):
        for pattern in ("", "*"):
            with self.assertRaises(InvalidArgumentException):
                self.store.clear(pattern)

    def test_clear_uses_unlink_batches(self):
        self.client._responses["scan"] = (0, ["app:s1", "app:s2"])
        deleted = self.store.clear("s:*")
        self.assertEqual(deleted, 0)     # 假 unlink 返回 0
        self.assertIsNotNone(self._last("unlink"))

    def test_type_of_decodes_bytes(self):
        self.client._responses["type"] = b"hash"
        self.assertEqual(self.store.type_of("h"), "hash")

    def test_publish_prefixes_channel(self):
        self.store.publish("events", "hi")
        name, args, _ = self._last("publish")
        self.assertEqual(args, ("app:events", "hi"))


# ---------------------------------------------------------------------- #
# 流水线
# ---------------------------------------------------------------------- #
class TestPipeline(unittest.TestCase):
    def setUp(self):
        _clear_env()
        _install_fake_redis()
        _reset_db()
        Db.config({"type": "redis", "host": "h", "password": "p"})
        self.store = Db.redis("app:")

    def tearDown(self):
        _reset_db()
        _restore_redis()

    def test_key_first_prefix(self):
        pipe = self.store.pipeline()
        pipe.set("a", "1")
        self.assertEqual(pipe.raw().commands[-1][1][0], "app:a")

    def test_all_keys_prefix(self):
        pipe = self.store.pipeline()
        pipe.delete("a", "b")
        self.assertEqual(pipe.raw().commands[-1][1], ("app:a", "app:b"))

    def test_two_keys_prefix(self):
        pipe = self.store.pipeline()
        pipe.rename("a", "b")
        self.assertEqual(pipe.raw().commands[-1][1], ("app:a", "app:b"))

    def test_chaining_returns_pipeline(self):
        pipe = self.store.pipeline()
        self.assertIs(pipe.set("a", "1").incr("b"), pipe)

    def test_transaction_flag(self):
        self.assertTrue(self.store.pipeline().raw().transaction)
        self.assertFalse(self.store.pipeline(transaction=False)
                         .raw().transaction)

    def test_container_commands(self):
        pipe = self.store.pipeline()
        pipe.hset("h", {"a": 1}).zadd("z", {"m": 2}).mset({"x": 1})
        commands = pipe.raw().commands
        self.assertEqual(commands[0][2]["mapping"], {"a": "1"})
        self.assertEqual(commands[1][1][1], {"m": 2.0})
        self.assertEqual(commands[2][1][0], {"app:x": "1"})

    def test_unknown_command_rejected(self):
        pipe = self.store.pipeline()
        with self.assertRaises(InvalidArgumentException) as ctx:
            pipe.some_new_command("a")
        self.assertIn("raw()", str(ctx.exception))

    def test_execute_returns_results(self):
        pipe = self.store.pipeline()
        pipe.set("a", "1").incr("b")
        self.assertEqual(pipe.execute(), [True, True])

    def test_execute_translates_errors(self):
        exc = type("ResponseError", (Exception,), {"__name__": "ResponseError"})
        self.store.client().fail_on_execute = exc("bad")
        pipe = self.store.pipeline()
        pipe.set("a", "1")
        with self.assertRaises(QueryError):
            pipe.execute()

    def test_context_manager_resets_on_error(self):
        pipe = self.store.pipeline()
        with self.assertRaises(RuntimeError):
            with pipe:
                pipe.set("a", "1")
                raise RuntimeError("boom")
        self.assertTrue(pipe.raw().reset_called)

    def test_missing_key_arg_rejected(self):
        pipe = self.store.pipeline()
        with self.assertRaises(InvalidArgumentException):
            pipe.set()


# ---------------------------------------------------------------------- #
# 发布订阅
# ---------------------------------------------------------------------- #
class TestSubscription(unittest.TestCase):
    def setUp(self):
        _clear_env()
        _install_fake_redis()
        _reset_db()
        Db.config({"type": "redis", "host": "h", "password": "p"})
        self.store = Db.redis("app:")

    def tearDown(self):
        _reset_db()
        _restore_redis()

    def test_subscribe_prefixes_channels(self):
        sub = self.store.subscribe("events", "alerts")
        self.assertEqual(sub._pubsub.subscribed, ["app:events", "app:alerts"])

    def test_psubscribe_prefixes_patterns(self):
        sub = self.store.subscribe(patterns=["ev*"])
        self.assertEqual(sub._pubsub.psubscribed, ["app:ev*"])

    def test_get_message(self):
        sub = self.store.subscribe("events")
        message = sub.get_message(timeout=0.1)
        self.assertEqual(message["type"], "message")

    def test_listen_yields_triples(self):
        sub = self.store.subscribe("events")
        self.assertEqual(next(sub.messages()), ("message", "ch", "x"))

    def test_close(self):
        sub = self.store.subscribe("events")
        sub.close()
        self.assertTrue(sub._pubsub.closed)

    def test_requires_channel(self):
        with self.assertRaises(InvalidArgumentException):
            self.store.subscribe()


# ---------------------------------------------------------------------- #
# 与其它驱动的边界
# ---------------------------------------------------------------------- #
class TestDriverBoundary(unittest.TestCase):
    def setUp(self):
        _reset_db()

    def tearDown(self):
        Db.close()
        _install_fake_redis()

    def test_db_redis_rejected_on_sqlite(self):
        Db.config({"database": ":memory:"})
        with self.assertRaises(UnsupportedOperation) as ctx:
            Db.redis()
        self.assertIn("不支持 Redis", str(ctx.exception))

    def test_sql_query_rejected_on_redis(self):
        _install_fake_redis()
        Db.config({"type": "redis", "host": "h", "password": "p"})
        with self.assertRaises(UnsupportedOperation):
            Db.table("user").select()

    def test_mongo_collection_rejected_on_redis(self):
        _install_fake_redis()
        Db.config({"type": "redis", "host": "h", "password": "p"})
        with self.assertRaises(UnsupportedOperation):
            Db.mongo("users")

    def test_redis_driver_registered(self):
        from tinkpyorm.drivers import available_drivers, get_driver
        self.assertIn("redis", available_drivers())
        self.assertIs(get_driver("redis"), RedisDriver)

    def test_prefix_from_connection_config(self):
        _install_fake_redis()
        Db.config({"type": "redis", "host": "h", "password": "p",
                   "prefix": "shared_"})
        store = Db.redis("app:")
        self.assertEqual(store.key("k"), "shared_app:k")

    def test_store_type(self):
        _install_fake_redis()
        Db.config({"type": "redis", "host": "h", "password": "p"})
        self.assertIsInstance(Db.redis(), RedisStore)
        self.assertIsInstance(Db.redis().pipeline(), RedisPipeline)


# ---------------------------------------------------------------------- #
# 选择 db（Redis 默认 16 个 db）
# ---------------------------------------------------------------------- #
class TestRedisDbSelection(unittest.TestCase):
    """``database`` / ``db`` 两种写法、冲突检测、越界与集群门禁。"""

    def setUp(self):
        _clear_env()
        _install_fake_redis()
        _reset_db()

    def tearDown(self):
        _reset_db()
        _restore_redis()
        _clear_env()

    def _settings(self, config):
        _reset_db()
        conn = Db.config(config)
        return conn.driver._resolve_settings()

    def test_database_field(self):
        settings = self._settings({"type": "redis", "host": "h",
                                   "password": "p", "database": 3})
        self.assertEqual(settings["database"], 3)

    def test_database_accepts_numeric_string(self):
        settings = self._settings({"type": "redis", "host": "h",
                                   "password": "p", "database": "7"})
        self.assertEqual(settings["database"], 7)

    def test_db_alias_in_options(self):
        """``options={'db': n}`` 与 ``database=n`` 等价（redis-py 的写法）。"""
        settings = self._settings({"type": "redis", "host": "h",
                                   "password": "p",
                                   "options": {"db": 4}})
        self.assertEqual(settings["database"], 4)

    def test_db_alias_accepts_string(self):
        settings = self._settings({"type": "redis", "host": "h",
                                   "password": "p",
                                   "options": {"db": "6"}})
        self.assertEqual(settings["database"], 6)

    def test_db_from_config_resolver(self):
        settings = self._settings({"type": "redis", "host": "h",
                                   "password": "p",
                                   "options": {"config_resolver":
                                               lambda: {"db": 9}}})
        self.assertEqual(settings["database"], 9)

    def test_db_from_env(self):
        os.environ["REDIS_DB"] = "11"
        settings = self._settings({"type": "redis", "host": "h",
                                   "password": "p"})
        self.assertEqual(settings["database"], 11)

    def test_url_db_is_parsed(self):
        """连接串路径里的 /N 就是生效 db（redis-py 连接时按它 SELECT）。"""
        settings = self._settings({"type": "redis",
                                   "options": {"url": "redis://:p@h:6379/12"}})
        self.assertEqual(settings["url"], "redis://:p@h:6379/12")
        self.assertEqual(settings["database"], 12)

    def test_url_without_db(self):
        settings = self._settings({"type": "redis",
                                   "options": {"url": "redis://:p@h:6379"}})
        self.assertEqual(settings["database"], 0)

    def test_url_db_conflicts_with_field(self):
        """URL 的 /4 与 database=5 不一致 -> 报错（不静默取一个）。"""
        _reset_db()
        conn = Db.config({"type": "redis", "database": 5,
                          "options": {"url": "redis://:p@h:6379/4"}})
        with self.assertRaises(InvalidArgumentException) as ctx:
            conn.driver._resolve_settings()
        self.assertIn("不一致", str(ctx.exception))

    def test_conflicting_db_values_rejected(self):
        """``database`` 与 ``db`` 同时给出且不一致 -> 报错（不静默取一个）。"""
        _reset_db()
        conn = Db.config({"type": "redis", "host": "h", "password": "p",
                          "database": 3, "options": {"db": 5}})
        with self.assertRaises(InvalidArgumentException) as ctx:
            conn.driver.connect()
        message = str(ctx.exception)
        self.assertIn("不一致", message)
        self.assertIn("database", message)

    def test_consistent_db_values_allowed(self):
        _reset_db()
        conn = Db.config({"type": "redis", "host": "h", "password": "p",
                          "database": 3, "options": {"db": "3"}})
        conn.driver.connect()
        self.assertEqual(conn.driver.db, 3)

    def test_driver_db_property_without_connect(self):
        _reset_db()
        conn = Db.config({"type": "redis", "host": "h", "password": "p",
                          "database": 5})
        self.assertEqual(conn.driver.db, 5)   # 未连接也能解析

    def test_store_db(self):
        _reset_db()
        Db.config({"type": "redis", "host": "h", "password": "p",
                   "database": 2})
        self.assertEqual(Db.redis("app:").db(), 2)

    def test_dialect_info_reports_db(self):
        _reset_db()
        conn = Db.config({"type": "redis", "host": "h", "password": "p",
                          "database": 2})
        info = conn.driver.dialect_info()
        self.assertEqual(info["db"], 2)
        self.assertIn("server_databases", info)

    def test_server_databases_detected_via_config_get(self):
        """``INFO`` 不含 databases，必须用 ``CONFIG GET`` 探测。"""
        _install_fake_redis(databases=16)
        _reset_db()
        conn = Db.config({"type": "redis", "host": "h", "password": "p"})
        conn.driver.connect()
        self.assertEqual(conn.driver.server_databases, 16)

    def test_db_beyond_server_range_rejected(self):
        """服务端只配了 4 个 db 时，db=4 必须被拦下并说清范围。"""
        _install_fake_redis(databases=4)
        _reset_db()
        conn = Db.config({"type": "redis", "host": "h", "password": "p",
                          "database": 4})
        with self.assertRaises(InvalidArgumentException) as ctx:
            conn.driver.connect()
        message = str(ctx.exception)
        self.assertIn("越界", message)
        self.assertIn("4 个 db", message)
        self.assertFalse(conn.driver.is_connected())

    def test_last_valid_db_accepted(self):
        _install_fake_redis(databases=4)
        _reset_db()
        conn = Db.config({"type": "redis", "host": "h", "password": "p",
                          "database": 3})
        conn.driver.connect()
        self.assertEqual(conn.driver.db, 3)

    def test_select_failure_explained_as_config_error(self):
        """握手阶段 SELECT 越界（ResponseError）要解释成配置错误。"""
        exc = type("ResponseError", (Exception,), {"__name__": "ResponseError"})
        _install_fake_redis(ping_error=exc("DB index is out of range"))
        _reset_db()
        conn = Db.config({"type": "redis", "host": "h", "password": "p",
                          "database": 99})
        with self.assertRaises(InvalidArgumentException) as ctx:
            conn.driver.connect()
        message = str(ctx.exception)
        self.assertIn("越界", message)
        self.assertIn("99", message)
        self.assertFalse(conn.driver.is_connected())

    def test_cluster_rejects_non_zero_db(self):
        """集群只有 db 0：配置非 0 必须在连接期拒绝。"""
        _install_fake_redis(topology="cluster", databases=16)
        _reset_db()
        conn = Db.config({"type": "redis", "host": "h", "password": "p",
                          "database": 2})
        with self.assertRaises(InvalidArgumentException) as ctx:
            conn.driver.connect()
        message = str(ctx.exception)
        self.assertIn("集群", message)
        self.assertIn("db 0", message)

    def test_cluster_allows_db_zero(self):
        _install_fake_redis(topology="cluster", databases=16)
        _reset_db()
        conn = Db.config({"type": "redis", "host": "h", "password": "p",
                          "database": 0})
        conn.driver.connect()
        self.assertEqual(conn.driver.db, 0)
        self.assertEqual(conn.driver.topology, "cluster")

    def test_negative_db_rejected(self):
        with self.assertRaises(InvalidArgumentException):
            self._settings({"type": "redis", "host": "h", "password": "p",
                            "database": -1})

    def test_non_numeric_db_rejected(self):
        with self.assertRaises(InvalidArgumentException) as ctx:
            self._settings({"type": "redis", "host": "h", "password": "p",
                            "database": "app"})
        self.assertIn("db 序号", str(ctx.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)
