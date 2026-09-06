# TinkPyORM 统一配置与多数据库架构重构方案

> 版本：草案 v1.0 ｜ 目标版本：v0.3.0（配置中心化）→ v0.4.0（驱动抽象）
> 适用代码基：TinkPyORM v0.2.0（`tinkpyorm/` 10 个模块）

---

## 0. 结论摘要

1. `Db.set_config` 的根本缺陷不是"写法繁琐"，而是**配置不进入全局共享的注册表**——它把连接挂到 `Db` 类变量上，却又与 `connection.py` 的模块级单例 `_default_connection` 形成**两套互不同步的状态**，导致"未配置时静默回退到 `:memory:` 空库"，这是最难排查的一类线上问题。
2. 解决路径是引入 **`Config`（配置值对象）+ `DatabaseManager`（全局注册表）+ 懒连接**，让配置在应用启动时注册一次，任意模块按需取用；`set_config` 降级为兼容包装。
3. 多数据库支持需要新增 **`Driver` 驱动抽象层**。当前代码**只支持 SQLite**（`connection.py` 直接 `import sqlite3`），并不存在 MySQL 支持，因此这是从零建抽象层而非扩展现有能力。
4. MongoDB / Redis 属于非关系型，**不应**套用 Query/Builder/Model；建议只纳入连接管理子集，原生客户端逃生舱访问。

---

## 1. 现状调用链

```
应用代码
  ├─ Db.set_config({...})          → Connection(sqlite3) ─┐
  │                                                        ├→ Db._connections[name]  ← 状态 A
  │                                                        └→ connection._default_connection（仅 name=="default"）← 状态 B
  ├─ Db.table("user")              → Query(conn=get_connection(), prefix=Db._prefix)
  └─ User.find(1)                  → Model._connection() → Db.get_connection(cls.__connection__)

Query._resolve_conn() 兜底        → connection.get_default_connection()  ← 未配置时静默建 :memory:
```

关键文件与职责：

| 文件 | 职责 | 与配置相关的耦合 |
|---|---|---|
| `db.py` | `Db` 门面 | `_connections` 字典、类级 `_prefix`、`set_config` |
| `connection.py` | `Connection`（sqlite3 封装） | 模块级 `_default_connection` 单例（L236-249） |
| `query.py` | 链式查询 | `_resolve_conn()`（L363-368）兜底默认连接 |
| `model.py` | ActiveRecord | `__connection__`（L83）→ `_connection()`（L285-287） |
| `builder.py` | SQL 编译 | `?` 占位符（L226/250/255/403/420）、`LIMIT`（L390-395） |

---

## 2. `Db.set_config` 的缺陷清单

| # | 缺陷 | 位置 | 影响 | 严重度 |
|---|---|---|---|---|
| D1 | **双状态不同步**：`Db._connections` 与 `connection._default_connection` 是两套状态，只有 `name == "default"` 时才同步 | `db.py` L52-54 | `Db.set_config(cfg, name="log")` 后 `Db.table(...)` 静默使用 `:memory:` 空库 | **高** |
| D2 | **未配置静默回退**：`get_connection()` 找不到时返回默认内存库而非报错 | `db.py` L63-65、`connection.py` L239-243 | 配置缺失不报错，表现为"表不存在 / 查到空数据"，排查成本极高 | **高** |
| D3 | **导入顺序敏感**：`Query` 在 `Db.table()` 调用瞬间就 `get_connection()`，配置必须先于任何查询构造 | `db.py` L82-89 | 模块 A 配置、模块 B 先被导入即出错；测试与 CLI 入口行为不一致 | **高** |
| D4 | **`_prefix` 是类级单值且不受 name 隔离** | `db.py` L26、L49 | 多连接时后一次 `set_config` 覆盖前一次的 prefix；`set_config("app.db")` 字符串分支根本不设置 prefix，残留旧值 | 中 |
| D5 | **配置类型不支持文件 / 环境变量 / DSN**：只接受 `dict`/`str`/`Connection` | `db.py` L41-51 | 无法从 `.env`/`toml`/`json` 加载；打包后路径（FreeVault）与开发路径不一致只能改代码 | 中 |
| D6 | **`type` 字段被静默丢弃**：`config["type"]` 既不校验也不使用 | `db.py` L47 | 用户写 `{"type": "mysql", ...}` 会被当成 SQLite 处理，**不报错** | **高** |
| D7 | **未知键无差别透传给 `sqlite3.connect()`** | `db.py` L47-48 | `{"host": "1.2.3.4", "user": "root"}` → `TypeError: 'host' is an invalid keyword argument`；这是接入新数据库最直接的障碍 | **高** |
| D8 | **重复 `set_config` 泄漏连接**：旧 `Connection` 未被 `close()` 即被替换 | `db.py` L52 | SQLite 文件锁残留、句柄泄漏；热重载/测试场景常见 | 中 |
| D9 | **多连接在 `Db` 门面不可用**：无 `Db.connect(name)` 入口，只有 `Model.__connection__` 支持命名连接 | `db.py` L81-89 | 读写分离、日志库分离等场景无法用门面表达 | 中 |
| D10 | **无生命周期管理 API**：缺 `has()`/`reset()`/`reconfigure()`/`names()` | `db.py` | 测试无法干净重置；`close()` 后 `_prefix` 未清、`_default_connection` 单例仍在 → 关闭不彻底 | 中 |
| D11 | **线程模型未定义**：`check_same_thread=False` 默认开启，但事务状态 `_in_transaction` 是实例变量，无锁 | `connection.py` L61、L182-192 | 多线程共享一个 `Connection` 会串事务 | 中 |

### 2.1 复现 D1/D2 的最小用例

```python
from tinkpyorm import Db

Db.set_config({"database": "log.db"}, name="log")
Db.table("user").select()          # 期望：用 log；实际：静默使用 :memory: 空库
                                   # → sqlite3.OperationalError: no such table: user
Db.get_connection("log")           # 有连接，但 Db.table() 永远拿不到它
```

### 2.2 复现 D6/D7 的最小用例

```python
Db.set_config({"type": "mysql", "host": "127.0.0.1", "user": "root",
               "password": "x", "database": "app"})
# → TypeError: 'host' is an invalid keyword argument for sqlite3.connect()
# 即便去掉 host/user/password，type="mysql" 也会被忽略，当 SQLite 用
```

---

## 3. 目标架构

### 3.1 分层

```
┌──────────────────────────────────────────────────────────┐
│ 应用侧：db_config.py（唯一定义 DATABASES）+ main.py（configure）│
└──────────────────────────┬───────────────────────────────┘
┌──────────────────────────▼───────────────────────────────┐
│ 门面层  Db / Model          配置入口：configure()          │
├──────────────────────────────────────────────────────────┤
│ 管理层  DatabaseManager     注册表 · 懒连接 · 生命周期     │
├──────────────────────────────────────────────────────────┤
│ 配置层  Config              dict / DSN / env / file        │
├──────────────────────────────────────────────────────────┤
│ 语义层  Connection          query/execute/事务/SQL日志（不变）│
├──────────────────────────────────────────────────────────┤
│ 驱动层  Driver（新增）       connect/execute/方言/事务原语   │
├──────────────────────────────────────────────────────────┤
│ 方言层  Grammar（新增）      占位符/标识符引用/LIMIT/分页    │
└──────────────────────────────────────────────────────────┘
```

### 3.2 设计要点

1. **配置与连接分离**：`Config` 只是值对象，注册时不建立连接；首次取用时才实例化 `Connection`（避免 import 即打开数据库文件，也便于测试注入）。
2. **单一注册中心**：全局唯一 `DatabaseManager` 实例，消除 D1 的双状态问题；`connection.py` 的 `_default_connection` 单例逐步废弃。
3. **命名连接 + 显式默认**：`default` 只是名字，可切换；未配置时按策略处理（见 §7 兼容）。
4. **方言下沉**：`Builder` 不再硬编码 `?` 与 `LIMIT`，改向 `Driver` 索取。
5. **逃生舱**：任何驱动都暴露 `raw_connection`，允许绕过 ORM 使用原生客户端（Redis/Mongo 的主要用法）。

---

## 4. 模块划分

```
tinkpyorm/
├── config.py            【新增】Config 值对象 + parse_dsn + 四种构造器
├── manager.py           【新增】DatabaseManager 注册表（懒连接 / 生命周期）
├── drivers/
│   ├── __init__.py      【新增】DRIVERS 注册表 + get_driver() / register()
│   ├── base.py          【新增】Driver 抽象基类 + SQLDriver 通用实现
│   ├── sqlite.py        【新增】现有 connection.py 的 sqlite3 逻辑下沉
│   ├── mysql.py         （v0.4.0）占位骨架，依赖 pymysql（可选）
│   ├── postgres.py      （v0.4.0）占位骨架，依赖 psycopg（可选）
│   └── nosql.py         （v0.4.0）Mongo/Redis 连接适配器
├── connection.py        【改造】保留公开 API，内部委派 Driver
├── db.py                【改造】接入 Manager；新增 connect()/connection()/configured()
├── builder.py           【改造】占位符与 LIMIT 改由 Driver 提供（5+1 处）
├── __init__.py          【改造】导出 configure()/connection()/Config
└── （其余模块不变）
```

---

## 5. 公共接口设计

### 5.1 `config.py`

```python
@dataclass
class Config:
    name: str = "default"
    type: str = "sqlite"                 # sqlite | mysql | postgresql | mssql | mongodb | redis
    database: str = ":memory:"           # SQL：库名 / SQLite 文件路径；Redis：db 号
    host: Optional[str] = None
    port: Optional[int] = None
    user: Optional[str] = None
    password: Optional[str] = None
    prefix: str = ""                     # 表前缀（按连接隔离，修复 D4）
    options: Dict[str, Any] = field(default_factory=dict)   # 驱动专属透传，修复 D7
    # 通用运行时（驱动无关）
    sql_log_enabled: bool = True
    sql_log_max: Optional[int] = 1000
    connect_timeout: Optional[float] = None
    path_expand: bool = True             # 是否展开 ~ 与环境变量

    @classmethod
    def from_dict(cls, d: dict, name: str = "default") -> "Config": ...
    @classmethod
    def from_dsn(cls, dsn: str, name: str = "default") -> "Config": ...
    @classmethod
    def from_env(cls, prefix: str = "TINKPYORM_", name: str = "default") -> "Config": ...
    @classmethod
    def from_file(cls, path: str, name: str = "default") -> "Config": ...   # json / toml / ini

def parse_dsn(dsn: str) -> dict:
    """sqlite:///abs/app.db · sqlite:///:memory: · mysql://u:p@h:3306/db
       postgresql://u:p@h:5432/db · redis://h:6379/0 · mongodb://h:27017/db"""
```

设计约束：
- `from_dict` 对未知键**不再透传给底层 connect**，而是归入 `options` 并在驱动侧按需取用（修复 D7）。
- `type` 未知时**立即抛 `ConfigError`**，不静默降级（修复 D6）。
- SQLite 文件路径统一 `Path.expanduser()` + `Path(p).expanduser().resolve()`，避免打包后 CWD 变化导致路径漂移。

### 5.2 `manager.py`

```python
class DatabaseManager:
    def configure(self, databases: Dict[str, Any], default: Optional[str] = None) -> None:
        """批量注册。值为 dict / DSN 字符串 / Config，统一归一化。"""

    def add(self, name: str, config: Union[Config, dict, str]) -> None:
        """登记配置，不建立连接（幂等：同名重复注册先关闭旧连接，修复 D8）。"""

    def connection(self, name: Optional[str] = None) -> "Connection":
        """取连接：懒建立 + 缓存。未注册时按 strict 策略处理（修复 D2）。"""

    def set_default(self, name: str) -> None: ...
    def has(self, name: str) -> bool: ...
    def config(self, name: str) -> Config: ...
    def names(self) -> List[str]: ...
    def disconnect(self, name: Optional[str] = None) -> None: ...
    def reconnect(self, name: Optional[str] = None) -> "Connection": ...
    def clear(self) -> None: ...          # 关闭全部并清空（测试用）

# 模块级单例
manager = DatabaseManager()
```

### 5.3 `drivers/base.py`

```python
class Driver(abc.ABC):
    name: str = ""                 # "sqlite"
    paramstyle: str = "qmark"      # qmark(?) | format(%s) | pyformat(%(name)s) | numeric(:1)

    def __init__(self, config: Config) -> None: ...

    # —— 连接生命周期（必选）——
    @abc.abstractmethod
    def connect(self) -> Any: ...
    @abc.abstractmethod
    def close(self) -> None: ...
    @abc.abstractmethod
    def ping(self) -> bool: ...

    # —— 执行（必选）——
    @abc.abstractmethod
    def query(self, sql: str, params: Sequence[Any] = ()) -> List[dict]: ...
    @abc.abstractmethod
    def execute(self, sql: str, params: Sequence[Any] = ()) -> int: ...
    @abc.abstractmethod
    def insert(self, sql: str, params: Sequence[Any] = ()) -> int:
        """返回自增主键。PG/SQLite5 需用 RETURNING，MySQL 用 lastrowid —— 差异在此收口。"""

    # —— 事务（可选覆写，SQLDriver 提供 SAVEPOINT 默认实现）——
    def begin(self) -> None: ...
    def commit(self) -> None: ...
    def rollback(self) -> None: ...
    def savepoint(self, name: str) -> None: ...
    def release(self, name: str) -> None: ...
    def rollback_to(self, name: str) -> None: ...

    # —— 方言（可选覆写，默认 SQLite 风格）——
    def quote_identifier(self, name: str) -> str: return f"`{name}`"
    def placeholder(self, n: int = 1) -> str: return ", ".join("?" * n)
    def limit_sql(self, limit: Any, offset: Optional[int] = None) -> str: ...
    def table_exists(self, table: str) -> bool: ...
    def table_fields(self, table: str) -> List[str]: ...

    # —— 逃生舱 ——
    @property
    def raw_connection(self) -> Any: ...

class SQLDriver(Driver):
    """关系型数据库公共基类：实现事务/方言默认行为，子类只覆写差异点。"""

class NoSQLDriver(Driver):
    """非关系型基类：仅 connect/close/ping/raw_connection 可用，
    其余方法抛 UnsupportedOperation（见 §6.2）。"""
```

### 5.4 `Db` / 顶层门面增量

```python
import tinkpyorm

tinkpyorm.configure(databases: dict, default: str = "default") -> None
tinkpyorm.connection(name: Optional[str] = None) -> Connection

class Db:
    @classmethod
    def connect(cls, name: str) -> "DbProxy": ...        # Db.connect("log").table("event")
    @classmethod
    def connection(cls, name: Optional[str] = None) -> Connection: ...
    @classmethod
    def configured(cls, name: Optional[str] = None) -> bool: ...
    # 以下保持语义不变：table/name/raw/query/execute/transaction/日志方法
    @classmethod
    def set_config(cls, config, name="default") -> Connection:
        """保留（兼容包装），内部转 manager.add() + 立即建连。"""
```

---

## 6. 多数据库扩展：新增一个数据库需要改什么

### 6.1 关系型数据库（以 PostgreSQL 为例）

| 步骤 | 文件 | 改动内容 | 是否必须 |
|---|---|---|---|
| 1 | `drivers/postgres.py` | 实现 `SQLDriver`：connect/close/ping/query/execute/insert | 必须 |
| 2 | `drivers/__init__.py` | `DRIVERS["postgresql"] = PostgresDriver` 或在类上用 `@register("postgresql")` | 必须 |
| 3 | `config.py` | 通常无需改动——`parse_dsn` 通用 URL 解析已覆盖；仅当 scheme 别名特殊时加映射 | 基本不用 |
| 4 | `builder.py` | **无需改动**：占位符与 LIMIT 已由 Driver 提供。仅当方言特殊（MSSQL 的 `OFFSET n ROWS FETCH NEXT m ROWS ONLY`、旧 MySQL 的分页）才需覆写 `limit_sql()` | 视方言 |
| 5 | `pyproject.toml` | 增加可选依赖组 `[project.optional-dependencies] postgres = ["psycopg[binary]"]` | 必须 |
| 6 | `test_drivers.py` | 复用现有测试套件做驱动级参数化（markers：`@pytest.mark.skipif(driver_unavailable)`） | 建议 |

**新增驱动的方言差异清单**（即子类通常需要覆写的点）：

| 能力 | SQLite 现状 | 差异点 |
|---|---|---|
| 占位符 | `?`（qmark） | MySQL `pymysql` 为 `%s`；PG `psycopg` 为 `%s`；`sqlite3` 也支持 `:name` |
| 标识符引用 | 反引号 `` `col` `` | PG/MSSQL 用双引号 `"col"`；MySQL 用反引号 |
| 自增主键 | `cur.lastrowid` | PG 需 `INSERT ... RETURNING id` 或 `SELECT lastval()` |
| 分页 | `LIMIT n OFFSET m` | PG/MySQL 同；MSSQL 需 `OFFSET m ROWS FETCH NEXT n ROWS ONLY` |
| 表结构探测 | `PRAGMA table_info(t)` | PG 查 `information_schema.columns`；MySQL 用 `SHOW COLUMNS` |
| 事务 | `BEGIN` / `SAVEPOINT` | 通用；PG 需注意 `psycopg` 的自动提交语义（`autocommit=True`） |
| 布尔/日期绑定 | SQLite 存 0/1 与字符串 | PG 有原生 bool/date，驱动已处理，ORM 层无感 |

**关键约束**：项目当前定位是"仅依赖标准库"。引入 MySQL/PG 支持后必须改为**可选依赖**（`pip install tinkpyorm[mysql]`），核心包保持零依赖；`drivers/__init__.py` 用惰性 import，缺依赖时在**实例化驱动时**抛 `DriverNotAvailable`，而非 import 时。

### 6.2 非关系型（MongoDB / Redis）

这两类**不适合**也不应该走 Query/Builder/Model 抽象——没有表、没有 JOIN、没有通用 SQL 方言。建议定位：

| 能力 | SQL 驱动 | Mongo/Redis 驱动 |
|---|---|---|
| 连接管理、命名注册、生命周期 | 支持 | **支持**（纳入统一 Manager） |
| 事务 | 支持 | 不支持（Redis 有 MULTI，语义不同，不在 ORM 层实现） |
| Query / Builder / Model / 关联 | 支持 | **不支持**，调用抛 `UnsupportedOperation` |
| 访问方式 | ORM 全链路 | `Db.connection("redis").driver.raw_connection` → 原生客户端 |

理由：强行把 Mongo 塞进 SQL Builder 会产生大量"假支持"（能构造 SQL 但语义错误），得不偿失。统一的价值应落在**配置与连接管理**上：应用侧一份 `DATABASES` 管住所有存储，ORM 只服务关系型部分。

```python
redis = Db.connection("redis").driver.raw_connection   # redis.Redis 实例
redis.set("k", "v", ex=60)
```

---

## 7. 使用示例

### 7.1 应用侧：只定义一次

```python
# core/database.py —— 全局唯一的配置定义处
from pathlib import Path

DATA_DIR = Path.home() / ".freevault"
DATA_DIR.mkdir(parents=True, exist_ok=True)

DATABASES = {
    "default": {
        "type": "sqlite",
        "database": str(DATA_DIR / "vault.db"),
        "prefix": "fv_",
        "options": {"journal_mode": "WAL", "timeout": 5.0},
        "sql_log_enabled": __debug__,          # 生产自动关闭日志
    },
    "log": {
        "type": "sqlite",
        "database": str(DATA_DIR / "log.db"),
        "options": {"journal_mode": "WAL"},
        "sql_log_enabled": False,
    },
    # v0.4.0 起可选：
    # "mysql":  "mysql://root:pwd@127.0.0.1:3306/app?charset=utf8mb4",
}
```

### 7.2 启动入口：注册一次

```python
# main.py
import tinkpyorm
from core.database import DATABASES

tinkpyorm.configure(DATABASES, default="default")   # 唯一的一次性调用
```

### 7.3 任意模块：直接使用，无需配置

```python
# feature/user/service.py —— 不再出现任何 set_config
from tinkpyorm import Db, Model

Db.table("user").where("status", 1).find()          # default 连接，自动加 fv_ 前缀
Db.connect("log").table("event").insert({"msg": "login"})   # 命名连接

class User(Model):
    __table__ = "user"

User.where("email", "a@b.c").find()                 # 走 default

class AuditLog(Model):
    __connection__ = "log"                          # 模型绑定命名连接（保持现有能力）
    __table__ = "event"
```

### 7.4 环境变量与文件（部署态覆盖）

```python
# 方式 A：env（优先级最高）
#   TINKPYORM_DEFAULT_TYPE=sqlite
#   TINKPYORM_DEFAULT_DATABASE=/var/lib/app/vault.db
tinkpyorm.configure(DATABASES)   # 同名 key 被 env 覆盖

# 方式 B：配置文件
tinkpyorm.configure(Config.from_file("config/database.toml"))

# 方式 C：DSN 直写
tinkpyorm.configure({"default": "sqlite:///~/.freevault/vault.db"})
```

---

## 8. 平滑迁移策略

### 8.1 兼容性承诺

| 现有 API | v0.3.0 状态 | 说明 |
|---|---|---|
| `Db.set_config(config, name)` | **保留**，内部转 `manager.add()` | 返回 `Connection` 的契约不变；新增去重（同名重复注册先关闭旧连接） |
| `Db.get_connection(name)` | 保留 | 语义不变，内部走 manager |
| `Db.table/name/raw/query/execute/transaction` | 保留 | 语义完全不变 |
| `Model.__connection__` | 保留 | 继续支持连接名或 Connection 对象 |
| `Connection` 公开方法 | 保留 | `query/execute/insert/transaction/table_fields/日志` 签名不变，内部委派 Driver |
| `connection.get_default_connection()` | 保留但标记废弃 | v0.5.0 移除 |

### 8.2 行为变更（需在 CHANGELOG 显著标注）

1. **未配置时的回退行为**：v0.3.0 起默认 `strict=False`，保持"静默建 `:memory:`"以兼容现有调用方，但发出 `DeprecationWarning`；v0.4.0 起默认 `strict=True`，未配置将抛 `ConfigError("未注册数据库连接: xxx")`。
   - 迁移期开关：`tinkpyorm.configure(..., strict=False)` 或环境变量 `TINKPYORM_STRICT=0`。
2. **`type` 非法值**：v0.3.0 起由"静默忽略"改为抛 `ConfigError`（只影响本来就跑不通的配置）。
3. **未知配置键**：由"透传 sqlite3.connect 并 TypeError"改为"归入 options"。仅当键名与 sqlite3 参数冲突时行为有变。
4. **`_prefix` 归属连接**：多连接场景下表前缀不再互相覆盖（原行为本身就是 bug）。

### 8.3 FreeVault 侧迁移步骤

| 步骤 | 动作 | 风险 |
|---|---|---|
| 1 | 升级 `libs/tinkpyorm` 至 v0.3.0（同步流程照旧：先备份再覆盖） | 低 |
| 2 | 新增 `core/database.py`，把散落在各模块的 `Db.set_config(...)` 集中成 `DATABASES` | 低 |
| 3 | 在应用启动入口（`main.py`，**先于任何 Model 定义与查询**）调用 `tinkpyorm.configure(DATABASES)` | 中——入口顺序要确保 |
| 4 | 删除各模块中的 `set_config` 调用；搜索关键字 `set_config` 全量清理 | 低 |
| 5 | 回归：现有 33 项测试 + FreeVault 侧登录/加解密读写冒烟 | 低 |
| 6 | （可选）开启 `strict=True`，暴露潜在的配置顺序问题 | 中——会暴露隐藏 bug，正是目的 |

---

## 9. 实施路线

| 阶段 | 范围 | 产出 | 破坏性 | 估工 |
|---|---|---|---|---|
| **Phase 1**<br>配置中心化 | `config.py` + `manager.py` + `db.py` 改造 + `__init__.py` 导出 | 解决 D1-D5、D8-D10 | 无（兼容保留） | 0.5-1 天 |
| **Phase 2**<br>驱动抽象 | `drivers/base.py` + `drivers/sqlite.py`，`Connection` 委派化，`builder.py` 占位符/LIMIT 下沉 | 解决 D6-D7 的一半，打通扩展路径 | 无（对外 API 不变） | 1-1.5 天 |
| **Phase 3**<br>新驱动 | `drivers/mysql.py` / `postgres.py` / `nosql.py` + 可选依赖 + 驱动级测试参数化 | 真正多数据库 | 无 | 每驱动 0.5-1 天 |
| **Phase 4**<br>收口 | `strict` 默认开启、移除废弃单例、文档与示例更新 | 清理历史包袱 | 有（主版本） | 0.5 天 |

### 9.1 建议的测试增量

| 测试 | 断言 |
|---|---|
| `test_config` | DSN 解析、env 覆盖、文件加载、非法 type 报错、路径展开 |
| `test_manager` | 懒连接（注册后无文件句柄）、同名重复注册关闭旧连接、clear、命名连接隔离、prefix 隔离 |
| `test_migration` | `set_config` 旧写法仍可用且行为一致（回归护栏） |
| `test_driver_contract` | 抽象驱动跑通现有 33 项用例（把 SQLite 驱动作为黄金参考实现） |

### 9.2 风险与对策

| 风险 | 对策 |
|---|---|
| 全局单例在测试间串扰 | 提供 `tinkpyorm.clear()`；pytest fixture 自动重置 |
| 打包后相对路径漂移（FreeVault） | Config 强制 `expanduser` + 相对路径解析基准可注入 |
| 可选依赖缺失 | 惰性 import，实例化时抛 `DriverNotAvailable` 并提示安装命令 |
| 多线程事务串扰（D11） | Phase 2 起 `Connection` 增加 `thread_local` 模式（可选，默认关闭），文档说明共享语义 |

---

## 10. 附：新旧写法对照

| 场景 | 现状（v0.2.0） | 重构后（v0.3.0+） |
|---|---|---|
| 定义配置 | 每个入口文件各调一次 `Db.set_config` | `core/database.py` 定义 `DATABASES`，启动时 `configure()` 一次 |
| 取连接 | `Db.get_connection()`（未配置则静默内存库） | `Db.connection()` / `tinkpyorm.connection()`（strict 时报错） |
| 多连接 | 仅 `Model.__connection__`；门面无入口 | `Db.connect("log").table(...)` |
| 表前缀 | 类级单值，多连接串味 | 每连接独立 `prefix` |
| 换数据库 | 需要重写 `Connection` | 新增一个 `drivers/*.py` + 注册一行 |
| 接 Redis | 不支持 | `Db.connection("redis").driver.raw_connection`（仅连接管理） |
