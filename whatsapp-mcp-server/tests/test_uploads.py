"""Unit tests for the /upload companion module.

Covers filename sanitation (which decides how the bridge recognises the
media type), the id/path lifecycle, and the TTL sweep. The module writes
to a real directory so these tests use a tmp_path per case.
"""

import os
import time

import pytest

import uploads


@pytest.fixture
def upload_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(uploads, "UPLOAD_DIR", str(tmp_path))
    return tmp_path


class TestSafeFilename:
    def test_strips_path_components(self):
        assert uploads.safe_filename("../../etc/passwd") == "passwd"

    def test_keeps_extension(self):
        assert uploads.safe_filename("slip.pdf") == "slip.pdf"

    def test_replaces_unsafe_characters_but_keeps_unicode(self):
        # \w is Unicode-aware so Japanese survives; ampersand becomes _
        assert uploads.safe_filename("見本-太郎.pdf") == "見本-太郎.pdf"
        assert uploads.safe_filename("foo&bar.pdf") == "foo_bar.pdf"

    def test_bare_extension_gets_a_name(self):
        assert uploads.safe_filename(".pdf") == "upload.pdf"

    def test_empty_input_falls_back(self):
        assert uploads.safe_filename("") == "upload.bin"
        assert uploads.safe_filename("....") == "upload.bin"

    def test_handles_windows_separators(self):
        # Backslashes become forward slashes before basename(), so this
        # collapses to the leaf — same as "/Users/x/file.jpg" would.
        assert uploads.safe_filename("C:\\Users\\x\\file.jpg") == "file.jpg"


class TestLifecycle:
    def test_new_upload_returns_writable_path(self, upload_dir):
        upload_id, path = uploads.new_upload("thing.pdf")
        assert len(upload_id) == 22  # token_urlsafe(16)
        assert path.endswith("thing.pdf")
        with open(path, "wb") as f:
            f.write(b"hello")
        assert uploads.resolve(upload_id) == path

    def test_resolve_rejects_malformed_id(self, upload_dir):
        assert uploads.resolve("") is None
        assert uploads.resolve("not an id") is None
        assert uploads.resolve("../../etc") is None

    def test_resolve_returns_none_when_directory_has_multiple_files(self, upload_dir):
        upload_id, path = uploads.new_upload("thing.pdf")
        with open(path, "wb") as f:
            f.write(b"a")
        # Simulate a stray second file in the same dir — resolve should
        # refuse to guess which one to send.
        with open(os.path.join(os.path.dirname(path), "sneaky.jpg"), "wb") as f:
            f.write(b"b")
        assert uploads.resolve(upload_id) is None

    def test_delete_is_idempotent_and_safe(self, upload_dir):
        upload_id, path = uploads.new_upload("x.pdf")
        with open(path, "wb") as f:
            f.write(b"a")
        uploads.delete(upload_id)
        assert uploads.resolve(upload_id) is None
        uploads.delete(upload_id)  # second time: no raise
        uploads.delete("not an id")  # malformed: no raise


class TestSweep:
    def test_sweep_removes_only_expired(self, upload_dir, monkeypatch):
        monkeypatch.setattr(uploads, "UPLOAD_TTL_SECONDS", 1)
        fresh_id, fresh_path = uploads.new_upload("fresh.pdf")
        with open(fresh_path, "wb") as f:
            f.write(b"a")
        old_id, old_path = uploads.new_upload("old.pdf")
        with open(old_path, "wb") as f:
            f.write(b"a")
        # Pretend the old upload's directory is older than the TTL.
        past = time.time() - 60
        os.utime(os.path.dirname(old_path), (past, past))

        uploads.sweep()

        assert uploads.resolve(fresh_id) == fresh_path
        assert uploads.resolve(old_id) is None

    def test_sweep_without_dir_is_a_noop(self, tmp_path, monkeypatch):
        monkeypatch.setattr(uploads, "UPLOAD_DIR", str(tmp_path / "nope"))
        uploads.sweep()  # no raise
