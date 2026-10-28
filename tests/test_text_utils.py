"""``text_utils`` 的语义对照：三个取值档各守各的口径，不互相顶替。"""

from __future__ import annotations

import json

import pytest

from customer_profile import text_utils as tut


class TestAsPayload:
    def test_object_and_list_pass_through(self):
        assert tut.as_payload({"a": 1}) == {"a": 1}
        assert tut.as_payload([1, 2]) == [1, 2]

    def test_json_string_is_parsed(self):
        assert tut.as_payload('{"a": 1}') == {"a": 1}
        assert tut.as_payload("[1, 2]") == [1, 2]

    @pytest.mark.parametrize("value", [None, "", "   ", "不是 JSON", 3, True])
    def test_unusable_input_gives_empty_dict(self, value):
        assert tut.as_payload(value) == {}


class TestAsText:
    def test_string_is_stripped(self):
        assert tut.as_text("  教师 ") == "教师"

    def test_none_is_empty(self):
        assert tut.as_text(None) == ""

    def test_bool_serialises_as_json(self):
        """与 ``as_display_text`` 的分野：这里布尔进 JSON，给取值比较用。"""
        assert tut.as_text(True) == "true"

    def test_list_serialises_as_json(self):
        assert tut.as_text(["a", "b"]) == json.dumps(["a", "b"], ensure_ascii=False)


class TestAsDisplayText:
    def test_bool_is_dropped(self):
        assert tut.as_display_text(True) == ""

    def test_numbers_render_decimal(self):
        assert tut.as_display_text(3) == "3"
        assert tut.as_display_text(0.75) == "0.75"

    def test_list_joins_with_dunhao(self):
        assert tut.as_display_text(["通勤", "代步"]) == "通勤、代步"

    def test_none_is_empty(self):
        assert tut.as_display_text(None) == ""


class TestAsFlatList:
    def test_pipe_string_splits(self):
        assert tut.as_flat_list("通勤||代步") == ["通勤", "代步"]

    def test_nested_lists_are_flattened(self):
        assert tut.as_flat_list([["a", "b"], "c"]) == ["a", "b", "c"]

    @pytest.mark.parametrize("value", [None, "", []])
    def test_empty_values_give_empty_list(self, value):
        assert tut.as_flat_list(value) == []

    def test_dict_becomes_single_item(self):
        """``{}`` 不是空值：字典本身就是一个取值（历史行为，保持不变）。"""
        assert tut.as_flat_list({"a": 1}) == [{"a": 1}]
        assert tut.as_flat_list({}) == [{}]


class TestAsJsonList:
    def test_json_array_string_is_parsed(self):
        assert tut.as_json_list('["a", "b"]') == ["a", "b"]

    def test_plain_string_stays_single_item(self):
        assert tut.as_json_list("通勤||代步") == ["通勤||代步"]

    def test_json_scalar_wrapped_in_list(self):
        assert tut.as_json_list("42") == [42]

    def test_tuple_becomes_list(self):
        assert tut.as_json_list(("a",)) == ["a"]

    @pytest.mark.parametrize("value", [None, "", "   "])
    def test_empty_values_give_empty_list(self, value):
        assert tut.as_json_list(value) == []


class TestStripCodeFence:
    """围栏剥离：各处原实现的并集，既有输入行为不变。"""

    def test_json_fence(self):
        assert tut.strip_code_fence('```json\n{"a": 1}\n```') == '{"a": 1}'

    def test_plain_fence(self):
        assert tut.strip_code_fence('```\n{"a": 1}\n```') == '{"a": 1}'

    def test_uppercase_and_other_language_tag(self):
        assert tut.strip_code_fence('```JSON\n{"a": 1}\n```') == '{"a": 1}'
        assert tut.strip_code_fence('```python\nx = 1\n```') == "x = 1"

    def test_unfenced_text_untouched(self):
        assert tut.strip_code_fence('{"a": 1}') == '{"a": 1}'

    def test_fence_in_the_middle_is_left_alone(self):
        """中段围栏是 ``evidence_subagent._extract_fenced_block`` 的职责，这里不剥。"""
        text = '说明文字\n```json\n{"a": 1}\n```'
        assert tut.strip_code_fence(text) == text

    @pytest.mark.parametrize("value", [None, {"a": 1}])
    def test_non_string_passes_through(self, value):
        assert tut.strip_code_fence(value) == value
