"""Path normalisation and volume-scoped cache keys."""

from __future__ import annotations

import os
import unicodedata

import pytest

from ssdaudit.paths import (
    long_path,
    match_key,
    strip_long_path,
    unicode_form,
    volume_relative_key,
)

windows_only = pytest.mark.skipif(os.name != "nt", reason="Windows path semantics")


class TestMatchKey:
    def test_case_is_folded_away(self):
        assert match_key("Photos/Beach.JPG") == match_key("photos/beach.jpg")

    def test_separators_are_normalised(self):
        assert match_key("a\\b\\c.txt") == match_key("a/b/c.txt")

    def test_decomposed_and_composed_names_match(self):
        name = "Zażółć.txt"
        assert match_key(unicodedata.normalize("NFC", name)) == match_key(
            unicodedata.normalize("NFD", name)
        )

    def test_genuinely_different_names_still_differ(self):
        assert match_key("a.txt") != match_key("b.txt")


class TestUnicodeForm:
    def test_identifies_composed_and_decomposed(self):
        name = "café.txt"
        assert unicode_form(unicodedata.normalize("NFC", name)) == "NFC"
        assert unicode_form(unicodedata.normalize("NFD", name)) == "NFD"

    def test_ascii_is_reported_as_composed(self):
        assert unicode_form("plain.txt") == "NFC"


@windows_only
class TestLongPath:
    def test_prefix_is_added_to_absolute_paths(self):
        assert long_path("D:\\data\\file.txt") == "\\\\?\\D:\\data\\file.txt"

    def test_prefixing_is_idempotent(self):
        once = long_path("D:\\data")
        assert long_path(once) == once

    def test_unc_paths_use_the_unc_form(self):
        assert long_path("\\\\server\\share\\f.txt") == "\\\\?\\UNC\\server\\share\\f.txt"

    def test_relative_components_are_resolved_first(self):
        """The \\\\?\\ prefix disables OS path parsing, so '..' must be gone already."""
        assert ".." not in long_path("D:\\a\\b\\..\\c.txt")

    def test_round_trips_back_to_a_normal_path(self):
        assert strip_long_path(long_path("D:\\data\\file.txt")) == "D:\\data\\file.txt"


class TestVolumeRelativeKey:
    def test_same_relative_path_under_different_roots_differs(self):
        """Two scan roots on one drive routinely share relative paths.

        Keying the cache on the scope-relative path alone would let one root's
        digest overwrite the other's.
        """
        left = volume_relative_key(os.path.abspath("folder-a"), "notes.txt")
        right = volume_relative_key(os.path.abspath("folder-b"), "notes.txt")
        assert left != right

    def test_the_same_file_reached_two_ways_gets_one_key(self):
        root = os.path.abspath("data")
        direct = volume_relative_key(root, "sub/file.txt")
        nested = volume_relative_key(os.path.join(root, "sub"), "file.txt")
        assert direct == nested
