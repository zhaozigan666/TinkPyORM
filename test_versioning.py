# -*- coding: utf-8 -*-
"""versioning.py 独立单元测试（审查报告 K2：版本门禁此前仅间接覆盖）。

重点：支持范围边界——"越界即报错、不就近猜测"是本模块的刻意行为。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from tinkpyorm.exceptions import InvalidArgumentException
from tinkpyorm.versioning import (
    Version, parse_version,
    DEFAULT_MYSQL_VERSION, DEFAULT_MONGO_VERSION, DEFAULT_REDIS_VERSION,
)


class TestDefaults(unittest.TestCase):
    def test_mysql_default(self):
        self.assertEqual(parse_version(None), Version(5, 7, 0))
        self.assertEqual(str(parse_version("")), DEFAULT_MYSQL_VERSION)

    def test_mongo_default(self):
        self.assertEqual(parse_version(None, product="mongodb"),
                         Version(4, 0, 0))

    def test_redis_default(self):
        self.assertEqual(parse_version(None, product="redis"),
                         Version(7, 0, 0))

    def test_default_override(self):
        self.assertEqual(parse_version(None, default="8.0"),
                         Version(8, 0, 0))


class TestAcceptedForms(unittest.TestCase):
    def test_mysql_forms(self):
        for v, expect in [("5.7", (5, 7, 0)), ("5.7.44", (5, 7, 44)),
                          ("8.0.36", (8, 0, 36)), (8.0, (8, 0, 0)),
                          ((9, 0), (9, 0, 0)), ([8, 4], (8, 4, 0))]:
            self.assertEqual(tuple(parse_version(v)), expect, msg=repr(v))

    def test_distro_suffix_truncated(self):
        v = parse_version("8.0.36-0ubuntu0.22.04.1")
        self.assertEqual(tuple(v), (8, 0, 36))

    def test_mysql_boundary_lines(self):
        for v in ("5.7", "8.0", "8.4", "9.1"):
            parse_version(v)  # 不应抛
        for v in ("8.0", "8.2", "8.4"):
            parse_version(v)

    def test_mongo_lines(self):
        for v in ("4.0", "4.2", "4.4", "5.0", "6.0", "7.0", "8.0"):
            parse_version(v, product="mongodb")

    def test_redis_lines(self):
        for v in ("4.0", "5.0", "6.0", "6.2", "7.0", "7.2", "7.4", "8.10.2"):
            parse_version(v, product="redis")


class TestRejectedRanges(unittest.TestCase):
    def test_mysql_out_of_range(self):
        for v in ("5.6", "6.0", "7.4", "8.5", "10.0"):
            with self.assertRaises(InvalidArgumentException, msg=v):
                parse_version(v)

    def test_mongo_odd_dev_minor_rejected(self):
        with self.assertRaises(InvalidArgumentException):
            parse_version("4.1", product="mongodb")

    def test_mongo_out_of_range(self):
        for v in ("3.6", "9.0"):
            with self.assertRaises(InvalidArgumentException, msg=v):
                parse_version(v, product="mongodb")

    def test_redis_release_line_exact(self):
        # 6.4 / 7.6 不是发布线，必须拒绝（6.2/7.2/7.4 才是）
        for v in ("6.4", "7.6", "5.2"):
            with self.assertRaises(InvalidArgumentException, msg=v):
                parse_version(v, product="redis")

    def test_bool_rejected(self):
        with self.assertRaises(InvalidArgumentException):
            parse_version(True)

    def test_garbage_rejected(self):
        for v in ("abc", "8.x", (1, 2, 3, 4), ()):
            with self.assertRaises(InvalidArgumentException, msg=repr(v)):
                parse_version(v)

    def test_unsupported_type_rejected(self):
        with self.assertRaises(InvalidArgumentException):
            parse_version({"a": 1})


class TestVersionObject(unittest.TestCase):
    def test_str_and_compare(self):
        v = parse_version("8.0.36")
        self.assertEqual(str(v), "8.0.36")
        self.assertTrue(v.at_least("5.7"))
        self.assertTrue(v.at_least((8, 0)))
        self.assertFalse(v.at_least("8.4"))

    def test_tuple_comparison_semantics(self):
        self.assertGreater(parse_version("8.0"), parse_version("5.7.44"))


if __name__ == "__main__":
    unittest.main()
