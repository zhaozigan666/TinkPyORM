# -*- coding: utf-8 -*-
"""TinkPyORM vs 原生 sqlite3 —— 全方位性能基准测试。

设计要点（公平性）：
    三档对照，用于拆解开销来源：
      A. raw-opt   : 原生最佳实践（executemany / 单事务 / sqlite3.Row 惰性取列）
      B. raw-lit   : 原生直译 ORM 语义（逐条 execute + 每条 commit + dict 转换）
      C. orm       : TinkPyORM 默认行为

    度量：time.perf_counter 中位数（含 warmup），每轮前置 setup 不计入计时。
    存储：磁盘临时文件（保留 fsync 真实开销），每场景独立库文件。
"""
from __future__ import annotations

import gc
import json
import os
import shutil
import sqlite3
import statistics
import sys
import tempfile
import time
import tracemalloc

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from tinkpyorm import Db, Model  # noqa: E402

# ---------------------------------------------------------------- 参数 ----
N_INSERT_SINGLE = 1000      # 逐条插入（自动提交）
N_BULK = 20000              # 批量插入
N_TX_ROWS = 20000           # 事务内逐条插入
N_PK_QUERY = 20000          # 主键点查次数
N_WHERE_QUERY = 200         # 条件查询次数（每次约 100 行）
N_SELECT_ALL = 50000        # 全表扫描行数
N_UPDATE = 500              # 单条更新次数（逐条 commit 极慢，取小样本）
N_DELETE = 500              # 单条删除次数
N_AGG = 2000                # 聚合次数
N_MODEL_ROWS = 20000        # 对象化行数
N_TUNE = 500                # PRAGMA 调优对比样本

REPEAT = 3
WARMUP = 1

# SQLite 单语句绑定变量上限 32766；本表 6 列 → 单批最多 5461 行
MAX_ROWS_PER_BATCH = 32766 // 6

TMPDIR = tempfile.mkdtemp(prefix="tinkpyorm_bench_")

SCHEMA = """
CREATE TABLE user (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    email TEXT,
    age INTEGER DEFAULT 0,
    status INTEGER DEFAULT 1,
    balance REAL DEFAULT 0,
    create_time TEXT
)
"""
COLS = ["name", "email", "age", "status", "balance", "create_time"]


def row(i: int) -> tuple:
    return (f"user{i}", f"u{i}@example.com", 20 + i % 50, i % 2, i * 1.5,
            "2026-09-01 00:00:00")


def dict_row(i: int) -> dict:
    return {"name": f"user{i}", "email": f"u{i}@example.com",
            "age": 20 + i % 50, "status": i % 2, "balance": i * 1.5,
            "create_time": "2026-09-01 00:00:00"}


# ------------------------------------------------------------ 基础设施 ----
def new_db(tag: str) -> str:
    """新建独立库文件并返回路径。"""
    path = os.path.join(TMPDIR, f"{tag}.db")
    if os.path.exists(path):
        os.remove(path)
    return path


def init_empty(path: str) -> None:
    """建表但不插数据。"""
    raw_seed(path, 0)


def raw_seed(path: str, n: int) -> None:
    """用原生方式快速建库并填充 n 行（不计入计时）。"""
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode = DELETE")
    conn.execute("DROP TABLE IF EXISTS user")
    conn.execute(SCHEMA)
    conn.executemany(
        f"INSERT INTO user ({','.join(COLS)}) VALUES (?,?,?,?,?,?)",
        (row(i) for i in range(n)),
    )
    conn.commit()
    conn.close()


def raw_conn(path: str) -> sqlite3.Connection:
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    return c


def orm_use(path: str) -> None:
    """让 ORM 指向指定库文件。"""
    Db.close()
    Db.set_config({"database": path})


def bench(setup, fn, repeat: int = REPEAT, warmup: int = WARMUP) -> float:
    """返回 fn 的中位耗时（秒）。setup 每轮执行但不计时。"""
    for _ in range(warmup):
        setup()
        fn()
    samples = []
    for _ in range(repeat):
        setup()
        gc.collect()
        t0 = time.perf_counter()
        fn()
        samples.append(time.perf_counter() - t0)
    return statistics.median(samples)


def bench_group(variants: dict, setup, repeat: int = REPEAT,
                warmup: int = WARMUP) -> dict:
    """交错采样：同一轮内依次跑完所有变体，并逐轮反转顺序。

    相比各变体独立连跑（bench），可抵消系统负载漂移与磁盘缓存
    预热带来的顺序偏差。setup 为统一函数，或按变体名的 dict。
    """
    names = list(variants)
    for _ in range(warmup):
        for k in names:
            _run_setup(setup, k)
            variants[k]()
    samples = {k: [] for k in names}
    for r in range(repeat):
        order = names[::-1] if r % 2 else names
        for k in order:
            _run_setup(setup, k)
            gc.collect()
            t0 = time.perf_counter()
            variants[k]()
            samples[k].append(time.perf_counter() - t0)
    return {k: statistics.median(v) for k, v in samples.items()}


def _run_setup(setup, key):
    (setup[key] if isinstance(setup, dict) else setup)()


def emit(scenario: str, timings: dict, ops: int, notes: dict) -> None:
    """打印并记录一组交错采样结果。"""
    for k, t in timings.items():
        record(scenario, k, t, ops, notes.get(k, ""))


def truncate(path: str) -> None:
    c = sqlite3.connect(path)
    c.execute("DELETE FROM user")
    c.execute("DELETE FROM sqlite_sequence WHERE name='user'")
    c.commit()
    c.close()


RESULTS: list[dict] = []


def record(scenario: str, variant: str, seconds: float, ops: int,
           note: str = "") -> None:
    RESULTS.append({
        "scenario": scenario,
        "variant": variant,
        "seconds": round(seconds, 6),
        "ops": ops,
        "ops_per_sec": round(ops / seconds, 1) if seconds > 0 else 0,
        "us_per_op": round(seconds / ops * 1e6, 3) if ops > 0 else 0,
        "note": note,
    })
    print(f"  {variant:<26} {seconds*1000:10.2f} ms   "
          f"{ops/seconds:12.0f} ops/s   {seconds/ops*1e6:9.2f} us/op")


# ------------------------------------------------------- 场景 1 逐条插入 ----
def s1_insert_single_autocommit():
    """逐条插入，每条独立事务（ORM 默认行为）。"""
    print(f"\n[S1] 逐条插入 × {N_INSERT_SINGLE}（自动提交，每条一次 COMMIT）")
    path = new_db("s1")
    init_empty(path)

    def setup():
        truncate(path)
        orm_use(path)
        Db.sql_log_clear()

    d = dict_row(0)
    emit("S1 逐条插入(autocommit)", bench_group({
        "A raw-opt (单次事务)": lambda: _raw_insert_batch(path, N_INSERT_SINGLE),
        "B raw-lit (每条提交)": lambda: _raw_insert_each_commit(path, N_INSERT_SINGLE),
        "C orm (每条提交)": lambda: _orm_insert_each(N_INSERT_SINGLE, d),
        "C+ orm (单事务包裹)": lambda: _orm_insert_each_tx(N_INSERT_SINGLE, d),
    }, setup), N_INSERT_SINGLE, {
        "A raw-opt (单次事务)": "executemany + 1 次 commit",
        "B raw-lit (每条提交)": "逐条 execute + commit + dict",
        "C orm (每条提交)": "Query.insert 默认行为",
        "C+ orm (单事务包裹)": "Db.transaction 包裹逐条 insert",
    })


def _raw_insert_batch(path, n):
    c = raw_conn(path)
    c.executemany(f"INSERT INTO user ({','.join(COLS)}) VALUES (?,?,?,?,?,?)",
                  (row(i) for i in range(n)))
    c.commit()
    c.close()


def _raw_insert_each_commit(path, n):
    c = raw_conn(path)
    sql = f"INSERT INTO user ({','.join(COLS)}) VALUES (?,?,?,?,?,?)"
    for i in range(n):
        c.execute(sql, row(i))
        c.commit()
    c.close()


def _orm_insert_all_chunked(data: list, size: int = MAX_ROWS_PER_BATCH):
    """分批 insert_all，规避 SQLite 绑定变量上限。"""
    for i in range(0, len(data), size):
        Db.table("user").insert_all(data[i:i + size])


def _orm_insert_each(n, d):
    for _ in range(n):
        Db.table("user").insert(dict(d))


def _orm_insert_each_tx(n, d):
    def body():
        for _ in range(n):
            Db.table("user").insert(dict(d))
        return None
    Db.transaction(body)


# --------------------------------------------------------- 场景 2 批量 ----
def s2_bulk_insert():
    """批量插入 20000 行。"""
    print(f"\n[S2] 批量插入 × {N_BULK}")
    path = new_db("s2")
    init_empty(path)
    data = [dict_row(i) for i in range(N_BULK)]

    def setup():
        truncate(path)
        orm_use(path)
        Db.sql_log_clear()

    emit("S2 批量插入", bench_group({
        "A raw-opt (executemany)": lambda: _raw_insert_batch(path, N_BULK),
        "C orm (自动分批)": lambda: Db.table("user").insert_all(
            [dict(x) for x in data]),
        "C2 orm (调用方手动分批)": lambda: _orm_insert_all_chunked(data),
        "C+ orm (事务内分批)": lambda: Db.transaction(
            lambda: _orm_insert_all_chunked(data)),
    }, setup), N_BULK, {
        "A raw-opt (executemany)": "单条多值语句 + 1 commit",
        "C orm (自动分批)": "修复后：ORM 内建分批，调用方无感",
        "C2 orm (调用方手动分批)": f"每批 {MAX_ROWS_PER_BATCH} 行",
        "C+ orm (事务内分批)": "显式事务 + 分批",
    })


# ----------------------------------------------------- 场景 3 事务内逐条 ----
def s3_insert_in_transaction():
    """事务内逐条插入 20000 行（隔离 fsync，纯比较抽象层开销）。"""
    print(f"\n[S3] 事务内逐条插入 × {N_TX_ROWS}（排除 fsync，纯 API 开销）")
    path = new_db("s3")
    init_empty(path)

    def setup():
        truncate(path)
        orm_use(path)
        Db.sql_log_clear()

    def raw_tx():
        c = raw_conn(path)
        sql = f"INSERT INTO user ({','.join(COLS)}) VALUES (?,?,?,?,?,?)"
        c.execute("BEGIN")
        for i in range(N_TX_ROWS):
            c.execute(sql, row(i))
        c.commit()
        c.close()

    d = dict_row(0)
    emit("S3 事务内逐条插入", bench_group({
        "A raw-opt (BEGIN+循环)": raw_tx,
        "C orm (事务内 insert)": lambda: _orm_insert_each_tx(N_TX_ROWS, d),
    }, setup), N_TX_ROWS, {
        "A raw-opt (BEGIN+循环)": "裸循环",
        "C orm (事务内 insert)": "含 SQL 构建 + sql_log",
    })


# --------------------------------------------------------- 场景 4 点查 ----
def s4_pk_lookup():
    """主键点查 20000 次。"""
    print(f"\n[S4] 主键点查 × {N_PK_QUERY}（表 10000 行）")
    path = new_db("s4")
    raw_seed(path, 10000)
    orm_use(path)

    def raw_opt():
        c = raw_conn(path)
        for i in range(N_PK_QUERY):
            c.execute("SELECT * FROM user WHERE id = ?", (i % 10000 + 1,)).fetchone()
        c.close()

    def raw_lit():
        c = raw_conn(path)
        for i in range(N_PK_QUERY):
            cur = c.execute("SELECT * FROM user WHERE id = ?", (i % 10000 + 1,))
            r = cur.fetchone()
            cur.close()
            dict(r) if r else None
        c.close()

    def orm_find():
        for i in range(N_PK_QUERY):
            Db.table("user").find(i % 10000 + 1)

    def orm_query_raw():
        for i in range(N_PK_QUERY):
            Db.query("SELECT * FROM user WHERE id = ?", (i % 10000 + 1,))

    def setup():
        Db.sql_log_clear()

    emit("S4 主键点查", bench_group({
        "A raw-opt (Row 惰性)": raw_opt,
        "B raw-lit (dict 转换)": raw_lit,
        "C orm (Query.find)": orm_find,
        "C2 orm (Db.query 裸SQL)": orm_query_raw,
    }, setup), N_PK_QUERY, {
        "A raw-opt (Row 惰性)": "sqlite3.Row，无 dict 转换",
        "B raw-lit (dict 转换)": "fetchone + dict(r)",
        "C orm (Query.find)": "含 Builder 构建 SQL",
        "C2 orm (Db.query 裸SQL)": "绕过 Builder，仅 dict 转换",
    })


# ------------------------------------------------- 场景 5 条件查询多行 ----
def s5_where_multi():
    print(f"\n[S5] 条件查询 × {N_WHERE_QUERY}（每次约 100 行，表 50000 行）")
    path = new_db("s5")
    raw_seed(path, 50000)
    orm_use(path)

    def raw_opt():
        c = raw_conn(path)
        for i in range(N_WHERE_QUERY):
            c.execute("SELECT * FROM user WHERE age = ? LIMIT 100",
                      (20 + i % 50,)).fetchall()
        c.close()

    def raw_lit():
        c = raw_conn(path)
        for i in range(N_WHERE_QUERY):
            cur = c.execute("SELECT * FROM user WHERE age = ? LIMIT 100",
                            (20 + i % 50,))
            [dict(r) for r in cur.fetchall()]
            cur.close()
        c.close()

    def orm_sel():
        for i in range(N_WHERE_QUERY):
            Db.table("user").where("age", 20 + i % 50).limit(100).select()

    def setup():
        Db.sql_log_clear()

    emit("S5 条件查询(~100行)", bench_group({
        "A raw-opt": raw_opt,
        "B raw-lit (dict)": raw_lit,
        "C orm (select)": orm_sel,
    }, setup), N_WHERE_QUERY, {})


# ------------------------------------------------------- 场景 6 全表扫描 ----
def s6_select_all():
    print(f"\n[S6] 全表扫描 {N_SELECT_ALL} 行")
    path = new_db("s6")
    raw_seed(path, N_SELECT_ALL)
    orm_use(path)

    def raw_opt():
        c = raw_conn(path)
        c.execute("SELECT * FROM user").fetchall()
        c.close()

    def raw_lit():
        c = raw_conn(path)
        cur = c.execute("SELECT * FROM user")
        [dict(r) for r in cur.fetchall()]
        cur.close()
        c.close()

    def orm_sel():
        Db.table("user").select()

    def orm_column():
        Db.table("user").column("name")

    def setup():
        Db.sql_log_clear()

    emit("S6 全表扫描", bench_group({
        "A raw-opt (Row)": raw_opt,
        "B raw-lit (dict)": raw_lit,
        "C orm (select→Collection)": orm_sel,
        "C2 orm (column 单列)": orm_column,
    }, setup), N_SELECT_ALL, {})


# ----------------------------------------------------------- 场景 7 更新 ----
def s7_update():
    print(f"\n[S7] 单条更新 × {N_UPDATE}（autocommit）")
    path = new_db("s7")
    raw_seed(path, 10000)
    orm_use(path)

    def raw_opt():
        c = raw_conn(path)
        c.execute("BEGIN")
        for i in range(N_UPDATE):
            c.execute("UPDATE user SET age = ? WHERE id = ?", (30, i % 10000 + 1))
        c.commit()
        c.close()

    def raw_lit():
        c = raw_conn(path)
        for i in range(N_UPDATE):
            c.execute("UPDATE user SET age = ? WHERE id = ?", (30, i % 10000 + 1))
            c.commit()
        c.close()

    def orm_upd():
        for i in range(N_UPDATE):
            Db.table("user").where("id", i % 10000 + 1).update({"age": 30})

    def orm_upd_tx():
        def body():
            for i in range(N_UPDATE):
                Db.table("user").where("id", i % 10000 + 1).update({"age": 30})
        Db.transaction(body)

    def setup():
        Db.sql_log_clear()

    emit("S7 单条更新", bench_group({
        "A raw-opt (单事务)": raw_opt,
        "B raw-lit (每条提交)": raw_lit,
        "C orm (每条提交)": orm_upd,
        "C+ orm (单事务)": orm_upd_tx,
    }, setup), N_UPDATE, {})


# ----------------------------------------------------------- 场景 8 删除 ----
def s8_delete():
    print(f"\n[S8] 单条删除 × {N_DELETE}（autocommit）")
    path = new_db("s8")

    def setup():
        raw_seed(path, 10000)
        orm_use(path)
        Db.sql_log_clear()

    def raw_opt():
        c = raw_conn(path)
        c.execute("BEGIN")
        for i in range(N_DELETE):
            c.execute("DELETE FROM user WHERE id = ?", (i + 1,))
        c.commit()
        c.close()

    def raw_lit():
        c = raw_conn(path)
        for i in range(N_DELETE):
            c.execute("DELETE FROM user WHERE id = ?", (i + 1,))
            c.commit()
        c.close()

    def orm_del():
        for i in range(N_DELETE):
            Db.table("user").delete(i + 1)

    def orm_del_tx():
        def body():
            for i in range(N_DELETE):
                Db.table("user").delete(i + 1)
        Db.transaction(body)

    emit("S8 单条删除", bench_group({
        "A raw-opt (单事务)": raw_opt,
        "B raw-lit (每条提交)": raw_lit,
        "C orm (每条提交)": orm_del,
        "C+ orm (单事务)": orm_del_tx,
    }, setup), N_DELETE, {})


# ----------------------------------------------------------- 场景 9 聚合 ----
def s9_aggregate():
    print(f"\n[S9] 聚合 count × {N_AGG}（表 50000 行）")
    path = new_db("s9")
    raw_seed(path, 50000)
    orm_use(path)

    def raw_opt():
        c = raw_conn(path)
        for _ in range(N_AGG):
            c.execute("SELECT COUNT(*) AS c FROM user").fetchone()[0]
        c.close()

    def orm_count():
        for _ in range(N_AGG):
            Db.table("user").count()

    def setup():
        Db.sql_log_clear()

    emit("S9 聚合 count", bench_group({
        "A raw-opt": raw_opt,
        "C orm (Query.count)": orm_count,
    }, setup), N_AGG, {})


# ------------------------------------------------------- 场景 10 对象化 ----
def s10_hydration():
    """对比 dict / sqlite3.Row / Model 实例三种承载方式的水合开销。"""
    print(f"\n[S10] 结果对象化 × {N_MODEL_ROWS} 行")
    path = new_db("s10")
    raw_seed(path, N_MODEL_ROWS)
    orm_use(path)

    class User(Model):
        __table__ = "user"
        __timestamps__ = False

    def raw_row():
        c = raw_conn(path)
        c.execute("SELECT * FROM user").fetchall()
        c.close()

    def raw_dict():
        c = raw_conn(path)
        cur = c.execute("SELECT * FROM user")
        [dict(r) for r in cur.fetchall()]
        cur.close()
        c.close()

    def orm_dict():
        Db.table("user").select()

    def orm_model():
        User.select()

    def setup():
        Db.sql_log_clear()

    emit("S10 对象化", bench_group({
        "A raw (sqlite3.Row)": raw_row,
        "B raw (dict 列表)": raw_dict,
        "C orm (dict Collection)": orm_dict,
        "D orm (Model 实例)": orm_model,
    }, setup), N_MODEL_ROWS, {"D orm (Model 实例)": "ActiveRecord 水合"})


# --------------------------------------------------------- 场景 11 内存 ----
def s11_memory():
    print(f"\n[S11] 内存峰值（查询 50000 行并持有结果）")
    path = new_db("s11")
    raw_seed(path, 50000)
    orm_use(path)

    class User(Model):
        __table__ = "user"
        __timestamps__ = False

    def measure(fn):
        gc.collect()
        tracemalloc.start()
        t0 = time.perf_counter()
        holder = fn()
        peak = tracemalloc.get_traced_memory()[1]
        tracemalloc.stop()
        dt = time.perf_counter() - t0
        del holder
        return peak, dt

    def raw_row():
        c = raw_conn(path)
        r = c.execute("SELECT * FROM user").fetchall()
        c.close()
        return r

    def raw_dict():
        c = raw_conn(path)
        cur = c.execute("SELECT * FROM user")
        r = [dict(x) for x in cur.fetchall()]
        cur.close()
        c.close()
        return r

    def orm_dict():
        return Db.table("user").select()

    def orm_model():
        return User.select()

    for label, fn in [("A raw (sqlite3.Row)", raw_row),
                      ("B raw (dict 列表)", raw_dict),
                      ("C orm (dict Collection)", orm_dict),
                      ("D orm (Model 实例)", orm_model)]:
        peak, dt = measure(fn)
        RESULTS.append({"scenario": "S11 内存峰值", "variant": label,
                        "seconds": round(dt, 6), "ops": 50000,
                        "ops_per_sec": round(50000 / dt, 1),
                        "us_per_op": round(dt / 50000 * 1e6, 3),
                        "note": f"peak={peak/1024/1024:.1f} MB"})
        print(f"  {label:<26} {peak/1024/1024:8.1f} MB   {dt*1000:8.1f} ms")


# ------------------------------------------------- 场景 12 sql_log 增长 ----
def s12_sql_log_growth():
    """SQL 日志的内存代价：修复前（无上限）vs 修复后（上限 / 关闭）。"""
    print(f"\n[S12] sql_log 内存代价（20000 次查询后）")
    path = new_db("s12")
    raw_seed(path, 10000)

    configs = [
        ("修复前 (无上限)", {"sql_log_max": None}, None),
        ("修复后 (上限 1000)", {}, None),
        ("修复后 (关闭日志)", {}, "disable"),
    ]
    for label, cfg, action in configs:
        Db.close()
        Db.set_config({"database": path, **cfg})
        if action == "disable":
            Db.sql_log_disable()
        Db.sql_log_clear()

        gc.collect()
        tracemalloc.start()
        before = tracemalloc.get_traced_memory()[0]
        for i in range(20000):
            Db.table("user").find(i % 10000 + 1)
        after = tracemalloc.get_traced_memory()[0]
        tracemalloc.stop()
        grew = (after - before) / 1024
        nlog = len(Db.get_sql_log())
        unit = "KB" if grew < 1024 else "MB"
        val = grew if grew < 1024 else grew / 1024
        print(f"  {label:<20} 日志 {nlog:>6} 条   净增内存 {val:7.1f} {unit}")
        RESULTS.append({"scenario": "S12 sql_log 内存", "variant": label,
                        "seconds": 0, "ops": 20000, "ops_per_sec": 0,
                        "us_per_op": 0,
                        "note": f"{nlog} 条 / +{val:.1f} {unit}"})


# ------------------------------------------------ 场景 13 纯 SQL 构建开销 ----
def s13_builder_overhead():
    """只构建 SQL 不执行，量化 Builder 本身的成本。"""
    print(f"\n[S13] 纯 SQL 构建开销（不执行，× 20000 次）")
    path = new_db("s13")
    raw_seed(path, 1000)
    orm_use(path)
    N = 20000

    def raw_build():
        for i in range(N):
            f"SELECT * FROM user WHERE id = {i} AND status = 1 ORDER BY id DESC LIMIT 10"

    def orm_build():
        for i in range(N):
            Db.table("user").where("id", i).where("status", 1) \
                .order("id", "desc").limit(10).fetch_sql(True).select()

    def setup():
        Db.sql_log_clear()

    emit("S13 SQL 构建", bench_group({
        "A raw (f-string)": raw_build,
        "C orm (Builder)": orm_build,
    }, setup, repeat=3, warmup=1), N,
        {"C orm (Builder)": "含 where/order/limit 解析"})


# ------------------------------------------------------ 场景 14 PRAGMA ----
def s14_pragma_tuning():
    """量化 PRAGMA 调优对 ORM 逐条写入的收益（优化建议依据）。"""
    print(f"\n[S14] PRAGMA 调优对 ORM 逐条插入的影响 × {N_TUNE}")
    variants = [
        ("默认 (journal=delete, sync=full)", []),
        ("synchronous=NORMAL", ["PRAGMA synchronous = NORMAL"]),
        ("synchronous=OFF", ["PRAGMA synchronous = OFF"]),
        ("journal=WAL + sync=NORMAL",
         ["PRAGMA journal_mode = WAL", "PRAGMA synchronous = NORMAL"]),
        ("journal=WAL + sync=OFF",
         ["PRAGMA journal_mode = WAL", "PRAGMA synchronous = OFF"]),
    ]
    def make_setup(p, pr):
        def _s():
            truncate(p)
            orm_use(p)
            for s in pr:
                Db.execute(s)
            Db.sql_log_clear()
        return _s

    d = dict_row(0)
    setups, variants_map = {}, {}
    for i, (label, pragmas) in enumerate(variants):
        p = new_db(f"s14_{i}")
        init_empty(p)
        setups[label] = make_setup(p, pragmas)
        variants_map[label] = lambda: _orm_insert_each(N_TUNE, d)

    timings = bench_group(variants_map, setups, repeat=2, warmup=1)
    for k, t in timings.items():
        record("S14 PRAGMA 调优", k, t, N_TUNE, f"{t / N_TUNE * 1000:.1f} ms/条")


# -------------------------------------------------------- 场景 15 导入 ----
def s15_import_cost():
    """导入开销（独立子进程测量，避免相互污染）。"""
    print(f"\n[S15] 模块导入开销")
    import subprocess
    py = sys.executable
    env = dict(os.environ, PYTHONPATH=HERE)

    def measure(code: str) -> float:
        samples = []
        for _ in range(5):
            t0 = time.perf_counter()
            subprocess.run([py, "-c", code], env=env,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            samples.append(time.perf_counter() - t0)
        return statistics.median(samples)

    base = measure("pass")
    raw = measure("import sqlite3")
    orm = measure("import tinkpyorm")

    print(f"  {'解释器基线':<26} {base*1000:8.1f} ms")
    print(f"  {'import sqlite3':<26} {(raw-base)*1000:8.1f} ms (净增)")
    print(f"  {'import tinkpyorm':<26} {(orm-base)*1000:8.1f} ms (净增)")
    for label, v in [("解释器基线", base), ("import sqlite3", raw - base),
                     ("import tinkpyorm", orm - base)]:
        RESULTS.append({"scenario": "S15 导入开销", "variant": label,
                        "seconds": round(v, 6), "ops": 1,
                        "ops_per_sec": 0, "us_per_op": round(v * 1e6, 1),
                        "note": "子进程冷启动"})


# ---------------------------------------------------------------- 主流程 ----
def main():
    print("=" * 78)
    print("TinkPyORM vs 原生 sqlite3 —— 性能基准")
    print(f"Python {sys.version.split()[0]}  |  SQLite {sqlite3.sqlite_version}  |  "
          f"repeat={REPEAT}  |  磁盘临时库")
    print("=" * 78)

    t0 = time.perf_counter()
    s1_insert_single_autocommit()
    s2_bulk_insert()
    s3_insert_in_transaction()
    s4_pk_lookup()
    s5_where_multi()
    s6_select_all()
    s7_update()
    s8_delete()
    s9_aggregate()
    s10_hydration()
    s11_memory()
    s12_sql_log_growth()
    s13_builder_overhead()
    s14_pragma_tuning()
    s15_import_cost()
    total = time.perf_counter() - t0

    out = os.path.join(HERE, "benchmark_results.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump({
            "meta": {
                "python": sys.version.split()[0],
                "sqlite": sqlite3.sqlite_version,
                "repeat": REPEAT,
                "total_seconds": round(total, 1),
                "generated": time.strftime("%Y-%m-%d %H:%M:%S"),
            },
            "results": RESULTS,
        }, f, ensure_ascii=False, indent=2)
    print(f"\n总耗时 {total:.1f}s  |  结果已写入 {out}")
    shutil.rmtree(TMPDIR, ignore_errors=True)


if __name__ == "__main__":
    main()
