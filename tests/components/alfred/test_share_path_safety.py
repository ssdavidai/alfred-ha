"""Tests for `custom_components.alfred.supervisor.safe_share_path`.

This is the security-critical surface of the Supervisor bridge. Any
share-* service trusts whatever `safe_share_path` returns is rooted
under `/share/`, so a single missed traversal path here = "Alfred can
write to /etc/passwd via an LLAT call". We test against:

- `..` traversal (relative, absolute, embedded mid-path)
- absolute paths outside /share/
- symlink escape (link points outside /share/)
- intermediate symlinks (link is inside /share/ but resolves outside)
- pure traversal inputs (`.`, `..`)
- NUL-byte injection
- empty / non-string input
- legitimate paths — must still work

We use a per-test temp directory as the share root rather than the real
`/share/`, so the suite is hermetic on macOS dev laptops + Linux CI.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from custom_components.alfred.supervisor import SharePathError, safe_share_path


@pytest.fixture
def share_root(tmp_path: Path) -> Path:
    """Stand-in for `/share/`. Has a subdir + file pre-created."""
    (tmp_path / "openwakeword").mkdir()
    (tmp_path / "openwakeword" / "existing.tflite").write_bytes(b"hello")
    return tmp_path


class TestLegitimatePaths:
    def test_bare_filename_resolves_under_root(self, share_root):
        result = safe_share_path("alfred.tflite", share_root=share_root)
        assert result == share_root / "alfred.tflite"

    def test_nested_path_resolves(self, share_root):
        result = safe_share_path("openwakeword/alfred.tflite", share_root=share_root)
        assert result == share_root / "openwakeword" / "alfred.tflite"

    def test_share_prefix_accepted(self, share_root):
        # The real share root would be /share; we monkeypatch via share_root.
        # Both "/share/foo" and "share/foo" should resolve to share_root/foo.
        result = safe_share_path("share/openwakeword/alfred.tflite", share_root=share_root)
        assert result == share_root / "openwakeword" / "alfred.tflite"

    def test_slash_share_prefix_accepted(self, share_root):
        result = safe_share_path(
            "/share/openwakeword/alfred.tflite", share_root=share_root
        )
        assert result == share_root / "openwakeword" / "alfred.tflite"

    def test_share_root_prefix_accepted(self, share_root):
        # "/<tmp_path>/openwakeword/foo" should pass since it's literally the root.
        composed = f"{share_root}/openwakeword/alfred.tflite"
        result = safe_share_path(composed, share_root=share_root)
        assert result == share_root / "openwakeword" / "alfred.tflite"

    def test_existing_file_resolves(self, share_root):
        # Must work for an existing file (used by read/delete).
        result = safe_share_path("openwakeword/existing.tflite", share_root=share_root)
        assert result == share_root / "openwakeword" / "existing.tflite"


class TestTraversalBlocked:
    def test_double_dot_relative_rejected(self, share_root):
        with pytest.raises(SharePathError) as exc:
            safe_share_path("../etc/passwd", share_root=share_root)
        assert exc.value.reason == "traversal"

    def test_double_dot_with_share_prefix_rejected(self, share_root):
        with pytest.raises(SharePathError) as exc:
            safe_share_path("share/../etc/passwd", share_root=share_root)
        assert exc.value.reason == "traversal"

    def test_double_dot_mid_path_rejected(self, share_root):
        with pytest.raises(SharePathError) as exc:
            safe_share_path("openwakeword/../../etc/passwd", share_root=share_root)
        assert exc.value.reason == "traversal"

    def test_pure_double_dot_rejected(self, share_root):
        with pytest.raises(SharePathError) as exc:
            safe_share_path("..", share_root=share_root)
        assert exc.value.reason == "traversal"

    def test_dot_rejected(self, share_root):
        with pytest.raises(SharePathError) as exc:
            safe_share_path(".", share_root=share_root)
        assert exc.value.reason == "traversal"

    def test_repeated_slashes_normalised(self, share_root):
        # POSIX-ish: multiple slashes collapse. We accept this case.
        result = safe_share_path("openwakeword//alfred.tflite", share_root=share_root)
        assert result == share_root / "openwakeword" / "alfred.tflite"


class TestAbsoluteEscapeBlocked:
    def test_absolute_etc_passwd_rejected(self, share_root):
        with pytest.raises(SharePathError) as exc:
            safe_share_path("/etc/passwd", share_root=share_root)
        assert exc.value.reason == "outside_share_root"

    def test_absolute_root_rejected(self, share_root):
        with pytest.raises(SharePathError) as exc:
            safe_share_path("/", share_root=share_root)
        assert exc.value.reason == "outside_share_root"

    def test_share_lookalike_rejected(self, share_root):
        # `/sharefoo/x` superficially starts with `/share` but isn't the
        # share root — must not be accepted.
        with pytest.raises(SharePathError) as exc:
            safe_share_path("/sharefoo/x", share_root=share_root)
        assert exc.value.reason == "outside_share_root"

    def test_bare_share_root_rejected(self, share_root):
        # Writing/reading the root itself doesn't make sense.
        with pytest.raises(SharePathError) as exc:
            safe_share_path(str(share_root), share_root=share_root)
        assert exc.value.reason == "is_share_root"

    def test_literal_slash_share_rejected(self, share_root):
        with pytest.raises(SharePathError) as exc:
            safe_share_path("/share", share_root=share_root)
        assert exc.value.reason == "is_share_root"


class TestSymlinkEscape:
    def test_symlink_target_outside_root_rejected(self, share_root, tmp_path):
        outside = tmp_path.parent / "outside_target"
        outside.mkdir(exist_ok=True)
        evil = share_root / "evil"
        evil.symlink_to(outside)
        with pytest.raises(SharePathError) as exc:
            safe_share_path("evil/foo.txt", share_root=share_root)
        assert exc.value.reason == "symlink_escape"

    def test_symlink_pointing_to_root_rejected(self, share_root):
        evil = share_root / "rootlink"
        evil.symlink_to("/")
        with pytest.raises(SharePathError) as exc:
            safe_share_path("rootlink/etc/passwd", share_root=share_root)
        assert exc.value.reason == "symlink_escape"

    def test_symlink_inside_share_root_allowed(self, share_root):
        # A symlink that stays inside the root should still work.
        target = share_root / "openwakeword"
        link = share_root / "wakelink"
        link.symlink_to(target)
        result = safe_share_path("wakelink/existing.tflite", share_root=share_root)
        # resolve(strict=False) will follow the symlink, so the resolved
        # path is under the real openwakeword dir. We only assert it's
        # rooted under share_root.
        assert str(result).startswith(str(share_root.resolve()))


class TestDefensivePathInputs:
    def test_empty_string_rejected(self, share_root):
        with pytest.raises(SharePathError) as exc:
            safe_share_path("", share_root=share_root)
        assert exc.value.reason == "empty"

    def test_nul_byte_rejected(self, share_root):
        with pytest.raises(SharePathError) as exc:
            safe_share_path("foo\x00bar", share_root=share_root)
        assert exc.value.reason == "nul_byte"

    def test_non_string_rejected(self, share_root):
        with pytest.raises(SharePathError) as exc:
            safe_share_path(None, share_root=share_root)  # type: ignore[arg-type]
        assert exc.value.reason == "empty"

    def test_whitespace_trimmed(self, share_root):
        result = safe_share_path("  alfred.tflite  ", share_root=share_root)
        assert result == share_root / "alfred.tflite"


class TestMaxBytes:
    """The size cap lives in the service handlers, not in `safe_share_path`,
    but it's part of the security surface. Sanity-check the constant."""

    def test_max_bytes_is_10mb(self):
        from custom_components.alfred.supervisor import MAX_SHARE_BYTES

        assert MAX_SHARE_BYTES == 10 * 1024 * 1024
