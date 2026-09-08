"""
tests.test_validators
~~~~~~~~~~~~~~~~~~~~~
Unit tests for app.shared.validators — URL detection and normalisation.

Tests verify:
  - is_direct_url() detects http/https URLs including youtu.be and ?si= params
  - is_playlist_url() rejects pure playlists but accepts single-video URLs
  - normalise_query() collapses whitespace
  - validate_play_query() enforces length limits
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.shared.validators import (
    is_direct_url,
    is_playlist_url,
    normalise_query,
    validate_play_query,
)


class TestIsDirectUrl(unittest.TestCase):

    def test_https_watch_url(self) -> None:
        self.assertTrue(is_direct_url("https://www.youtube.com/watch?v=dQw4w9WgXcQ"))

    def test_http_watch_url(self) -> None:
        self.assertTrue(is_direct_url("http://www.youtube.com/watch?v=dQw4w9WgXcQ"))

    def test_youtu_be_short_url(self) -> None:
        self.assertTrue(is_direct_url("https://youtu.be/dQw4w9WgXcQ"))

    def test_youtu_be_with_si_param(self) -> None:
        """?si= tracking parameter from YouTube mobile share must not break URL detection."""
        self.assertTrue(is_direct_url("https://youtu.be/dQw4w9WgXcQ?si=abc123XYZ"))

    def test_watch_url_with_si_param(self) -> None:
        self.assertTrue(is_direct_url("https://www.youtube.com/watch?v=XSgGCUYwzvU&si=abc"))

    def test_plain_search_query(self) -> None:
        self.assertFalse(is_direct_url("phool aur"))

    def test_empty_string(self) -> None:
        self.assertFalse(is_direct_url(""))

    def test_query_starting_with_www(self) -> None:
        """www.youtube.com without scheme is NOT a direct URL."""
        self.assertFalse(is_direct_url("www.youtube.com/watch?v=test"))


class TestIsPlaylistUrl(unittest.TestCase):

    def test_explicit_playlist_endpoint(self) -> None:
        self.assertTrue(is_playlist_url("https://www.youtube.com/playlist?list=PLxxx"))

    def test_list_param_without_video(self) -> None:
        self.assertTrue(is_playlist_url("https://www.youtube.com/watch?list=PLxxx"))

    def test_single_video_with_list_param(self) -> None:
        """A URL with both v= and list= is a single video, not a playlist."""
        self.assertFalse(
            is_playlist_url("https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=PLxxx")
        )

    def test_youtu_be_with_list(self) -> None:
        """youtu.be short URL with list= is treated as single video."""
        self.assertFalse(
            is_playlist_url("https://youtu.be/dQw4w9WgXcQ?list=PLxxx")
        )

    def test_plain_watch_url(self) -> None:
        self.assertFalse(is_playlist_url("https://www.youtube.com/watch?v=dQw4w9WgXcQ"))

    def test_youtu_be_simple(self) -> None:
        self.assertFalse(is_playlist_url("https://youtu.be/dQw4w9WgXcQ"))

    def test_watch_url_with_si_param_only(self) -> None:
        """?si= without list= is not a playlist."""
        self.assertFalse(
            is_playlist_url("https://www.youtube.com/watch?v=abc&si=xyz")
        )


class TestNormaliseQuery(unittest.TestCase):

    def test_strips_leading_trailing_whitespace(self) -> None:
        self.assertEqual(normalise_query("  phool  "), "phool")

    def test_collapses_internal_spaces(self) -> None:
        self.assertEqual(normalise_query("phool   by   aur"), "phool by aur")

    def test_tabs_and_newlines_collapsed(self) -> None:
        self.assertEqual(normalise_query("phool\tby\naur"), "phool by aur")

    def test_already_normalised_unchanged(self) -> None:
        self.assertEqual(normalise_query("phool by aur"), "phool by aur")


class TestValidatePlayQuery(unittest.TestCase):

    def test_valid_query(self) -> None:
        ok, reason = validate_play_query("phool by aur")
        self.assertTrue(ok)
        self.assertEqual(reason, "")

    def test_empty_query(self) -> None:
        ok, reason = validate_play_query("")
        self.assertFalse(ok)

    def test_whitespace_only_query(self) -> None:
        ok, reason = validate_play_query("   ")
        self.assertFalse(ok)

    def test_query_at_max_length(self) -> None:
        ok, _ = validate_play_query("a" * 500)
        self.assertTrue(ok)

    def test_query_over_max_length(self) -> None:
        ok, _ = validate_play_query("a" * 501)
        self.assertFalse(ok)

    def test_valid_direct_url(self) -> None:
        ok, _ = validate_play_query("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
        self.assertTrue(ok)


if __name__ == "__main__":
    unittest.main()
