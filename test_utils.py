# -*- coding: utf-8 -*-
"""utils.py 独立单元测试（审查报告 K2：Raw/字段解析此前仅间接覆盖）。"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from tinkpyorm.utils import (
    Raw, raw, is_raw, to_snake, to_camel, parse_field, parse_order, UNSET,
)


def bq(s: str) -> str:
    """测试用反引号引用函数（代替方言 quote_identifier）。"""
    return f"`{s}`"


class TestRaw(unittest.TestCase):
    def test_raw_factory(self):
        r = raw("AVG(score)")
        self.assertIsInstance(r, Raw)
        self.assertTrue(is_raw(r))
        self.assertEqual(r.value, "AVG(score)")

    def test_raw_str_repr(self):
        self.assertEqual(str(raw("x + 1")), "x + 1")
        self.assertIn("x + 1", repr(raw("x + 1")))

    def test_raw_requires_string(self):
        with self.assertRaises(TypeError):
            Raw(123)

    def test_plain_values_not_raw(self):
        self.assertFalse(is_raw("text"))
        self.assertFalse(is_raw(None))


class TestNaming(unittest.TestCase):
    def test_to_snake(self):
        self.assertEqual(to_snake("whereOr"), "where_or")
        self.assertEqual(to_snake("findOrFail"), "find_or_fail")
        self.assertEqual(to_snake("already_snake"), "already_snake")

    def test_to_camel(self):
        self.assertEqual(to_camel("where_or"), "whereOr")
        self.assertEqual(to_camel("find_or_fail"), "findOrFail")

    def test_roundtrip(self):
        self.assertEqual(to_camel(to_snake("whereOr")), "whereOr")


class TestParseField(unittest.TestCase):
    def test_comma_string(self):
        self.assertEqual(parse_field("id,name", bq), ["`id`", "`name`"])

    def test_list(self):
        self.assertEqual(parse_field(["id", "name"], bq), ["`id`", "`name`"])

    def test_dict_alias(self):
        self.assertEqual(parse_field({"uid": "id"}, bq), ["`id` AS `uid`"])

    def test_raw_passthrough_unquoted(self):
        self.assertEqual(parse_field(raw("COUNT(*)"), bq), ["COUNT(*)"])

    def test_unsupported_type(self):
        with self.assertRaises(TypeError):
            parse_field(42, bq)


class TestParseOrder(unittest.TestCase):
    def test_string_with_direction(self):
        self.assertEqual(parse_order("id desc", bq), ["`id` DESC"])

    def test_string_default_asc(self):
        self.assertEqual(parse_order("id", bq), ["`id` ASC"])

    def test_list_all_asc(self):
        self.assertEqual(parse_order(["id", "name"], bq),
                         ["`id` ASC", "`name` ASC"])

    def test_dict_directions(self):
        self.assertEqual(parse_order({"id": "desc", "name": None}, bq),
                         ["`id` DESC", "`name` ASC"])

    def test_raw_passthrough(self):
        self.assertEqual(parse_order(raw("RAND()"), bq), ["RAND()"])

    def test_unsupported_type(self):
        with self.assertRaises(TypeError):
            parse_order(3.14, bq)


class TestUnset(unittest.TestCase):
    def test_sentinel_identity(self):
        self.assertIsNot(UNSET, None)
        self.assertIsNot(UNSET, False)
        # 哨兵必须为真值且全局唯一（区分"未传"与显式 None）
        self.assertTrue(UNSET)


if __name__ == "__main__":
    unittest.main()
