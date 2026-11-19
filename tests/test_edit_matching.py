from asf.broker import _numbered, _unnumber, loose_replace, nearest

SRC = """class A:
    def f(self):
        if x:
            return 1
        return 2

    def g(self):
        return 3
"""


def test_line_numbers_copied_from_read_file_are_recognised_and_removed():
    old = "    2      def f(self):\n    3          if x:"
    assert _numbered(old) and _unnumber(old) == "    def f(self):\n        if x:"
    assert not _numbered("x = 1\n  12  not a gutter on every line\ny")


def test_loose_match_ignores_indentation_and_reindents_the_new_text():
    out = loose_replace(SRC, "if x:\n    return 1", "if x and y:\n    return 10")
    assert "        if x and y:\n            return 10\n        return 2" in out


def test_loose_match_refuses_when_more_than_one_place_fits():
    assert loose_replace("a\n  b\nc\n    b\n", "b", "z") is None
    assert loose_replace(SRC, "nothing like this", "z") is None


def test_a_miss_points_at_the_closest_text_with_line_numbers():
    hint = nearest(SRC, "def g(self):\n    return 4")
    assert "closest text" in hint and "    7      def g(self):" in hint
