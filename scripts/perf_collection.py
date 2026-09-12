"""Collection 性能基准（P2，独立脚本，不进 unittest）。

目的：量化"字段提升（生成列）+ 索引"对文档集合路径查询的收益，
为 PERFORMANCE.md 提供第十三节数据。直接运行，输出对比表，不断言。

用法：
    python scripts/perf_collection.py            # 默认 20000 行
    python scripts/perf_collection.py 50000     # 指定行数
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tinkpyorm import Db


def bench(label, fn, repeat=3):
    best = None
    for _ in range(repeat):
        t0 = time.perf_counter()
        fn()
        dt = time.perf_counter() - t0
        best = dt if best is None else min(best, dt)
    print(f"  {label:<28} {best * 1000:8.2f} ms")
    return best


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 20000
    Db.close()
    Db.set_config({"database": ":memory:", "sql_log_enabled": False})

    # 预置数据：一半命中高选择性条件（city），其余随机
    print(f"预置 {n} 行文档 ...")
    docs = Db.collection("bench")
    payload = [{"name": f"u{i}", "age": i % 100,
                "profile": {"city": "北京" if i % 5 == 0 else "上海"}}
               for i in range(n)]
    docs.insert_all(payload)
    print(f"  总行数: {docs.count()}")

    city = "北京"
    print("\n未提升（纯 json_extract 路径扫描）:")
    bench("全表扫描 count", lambda: docs.count())
    bench("路径等值查询", lambda: docs.where("profile.city", "=", city).count())

    print("\n提升列 + 索引:")
    docs.promote("profile.city")
    docs.ensure_index("profile.city")
    bench("全表扫描 count", lambda: docs.count())
    bench("列等值查询（走索引）", lambda: docs.where("profile.city", "=", city).count())

    # 表达式索引（未提升字段）对照
    print("\n表达式索引（未提升字段 age）:")
    docs.ensure_index("age")
    bench("表达式索引范围查询", lambda: docs.where("age", ">=", 90).count())

    print("\n（注：:memory: 无 fsync，纯 CPU/编译开销对比；"
          "生产落盘场景见 PERFORMANCE.md §12 的 fsync 影响）")


if __name__ == "__main__":
    main()
