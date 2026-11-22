import contextlib
import errno
import importlib.util
import io
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
import uuid
from unittest import mock
from urllib.error import HTTPError, URLError

PROJECT_ROOT = os.path.dirname(os.path.dirname(__file__))
MODULE_PATH = os.path.join(PROJECT_ROOT, "fast-github.py")
SPEC = importlib.util.spec_from_file_location("fast_github", MODULE_PATH)
fast_github = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fast_github)


# 构造固定返回值的上下文管理器，供测试使用。
@contextlib.contextmanager
def _context(value):
    yield value


# 为测试提供临时文件和临时目录。
class _TempPathsMixin:
    # 创建临时文件并登记测试结束后的清理。
    def make_file(self, content):
        if not isinstance(content, bytes):
            content = content.encode("utf-8")
        with tempfile.NamedTemporaryFile(mode="wb", delete=False) as handle:
            handle.write(content)
            path = handle.name
        self.addCleanup(self._remove_file, path)
        return path

    # 创建临时目录并登记测试结束后的清理。
    def make_directory(self):
        path = os.path.join(
            tempfile.gettempdir(), "fast-github-test-" + uuid.uuid4().hex
        )
        os.makedirs(path)
        self.addCleanup(shutil.rmtree, path, True)
        return path

    # 删除临时文件，忽略文件不存在的情况。
    @staticmethod
    def _remove_file(path):
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass

    # 按 UTF-8/surrogateescape 读取文本。
    @staticmethod
    def read_text(path):
        with open(
                path,
                "r",
                encoding="utf-8",
                errors="surrogateescape",
                newline="",
        ) as handle:
            return handle.read()

    # 按字节读取文件。
    @staticmethod
    def read_bytes(path):
        with open(path, "rb") as handle:
            return handle.read()


# 测试 hosts 文件区块的合并与写入。
class UpdateTests(_TempPathsMixin, unittest.TestCase):
    # 测试替换已有的标准标记区块。
    def test_replaces_canonical_block(self):
        path = self.make_file(
            "127.0.0.1 localhost\n"
            + fast_github.START_MARKER + "\n"
            + "1.2.3.4 old.example\n"
            + fast_github.END_MARKER + "\n"
            + "192.0.2.1 custom.example\n"
        )

        result = fast_github.update_hosts_file(
            path, "5.6.7.8 new.example\n5.6.7.8 new.example\n"
        )

        self.assertTrue(result.changed)
        self.assertEqual(result.entries, 2)
        self.assertEqual(result.mode, "atomic")
        self.assertEqual(
            self.read_text(path),
            "127.0.0.1 localhost\n"
            + fast_github.START_MARKER + "\n"
            + "5.6.7.8 new.example\n"
            + "5.6.7.8 new.example\n"
            + fast_github.END_MARKER + "\n"
            + "192.0.2.1 custom.example\n",
        )

    # 测试更新后保留 CRLF 行尾。
    def test_preserves_crlf_line_endings(self):
        start = fast_github.START_MARKER.encode("ascii")
        end = fast_github.END_MARKER.encode("ascii")
        path = self.make_file(
            b"127.0.0.1 localhost\r\n"
            + start + b"\r\n"
                      b"1.2.3.4 old.example\r\n"
            + end + b"\r\n"
                    b"192.0.2.1 custom.example\r\n"
        )

        fast_github.update_hosts_file(path, "5.6.7.8 new.example\n")

        self.assertEqual(
            self.read_bytes(path),
            b"127.0.0.1 localhost\r\n"
            + start + b"\r\n"
                      b"5.6.7.8 new.example\r\n"
            + end + b"\r\n"
                    b"192.0.2.1 custom.example\r\n",
        )

    # 测试缺少标记时在文件末尾追加区块。
    def test_appends_block_when_markers_are_absent(self):
        path = self.make_file("127.0.0.1 localhost")

        fast_github.update_hosts_file(path, "5.6.7.8 new.example\n")

        self.assertEqual(
            self.read_text(path),
            "127.0.0.1 localhost\n"
            + fast_github.START_MARKER + "\n"
            + "5.6.7.8 new.example\n"
            + fast_github.END_MARKER + "\n",
        )

    # 测试空 hosts 文件会创建标记区块。
    def test_creates_block_in_empty_file(self):
        path = self.make_file(b"")

        result = fast_github.update_hosts_file(path, "5.6.7.8 new.example\n")

        self.assertTrue(result.changed)
        self.assertEqual(
            self.read_text(path),
            fast_github.START_MARKER + "\n"
            + "5.6.7.8 new.example\n"
            + fast_github.END_MARKER + "\n",
        )

    # 测试在 CRLF 文件中追加区块。
    def test_appends_block_with_existing_crlf(self):
        start = fast_github.START_MARKER.encode("ascii")
        end = fast_github.END_MARKER.encode("ascii")
        path = self.make_file(b"127.0.0.1 localhost\r\n")

        fast_github.update_hosts_file(path, "5.6.7.8 new.example\n")

        self.assertEqual(
            self.read_bytes(path),
            b"127.0.0.1 localhost\r\n"
            + start + b"\r\n"
                      b"5.6.7.8 new.example\r\n"
            + end + b"\r\n",
        )

    # 测试文件末尾无换行时保持该格式。
    def test_keeps_missing_trailing_newline_at_eof(self):
        path = self.make_file(
            fast_github.START_MARKER + "\n"
            + "1.2.3.4 old.example\n"
            + fast_github.END_MARKER
        )

        fast_github.update_hosts_file(path, "5.6.7.8 new.example\n")

        self.assertEqual(
            self.read_text(path),
            fast_github.START_MARKER + "\n"
            + "5.6.7.8 new.example\n"
            + fast_github.END_MARKER,
        )

    # 测试识别带缩进的标记行。
    def test_recognizes_indented_markers(self):
        path = self.make_file(
            "  " + fast_github.START_MARKER + "  \n"
                                              "1.2.3.4 old.example\n"
                                              "\t" + fast_github.END_MARKER + "\t\n"
        )

        fast_github.update_hosts_file(path, "5.6.7.8 new.example\n")

        self.assertEqual(
            self.read_text(path),
            fast_github.START_MARKER + "\n"
            + "5.6.7.8 new.example\n"
            + fast_github.END_MARKER + "\n",
        )

    # 测试拒绝缺失、重复、嵌套或错序的标记。
    def test_rejects_invalid_marker_layouts(self):
        invalid_files = (
            fast_github.START_MARKER + "\n1.2.3.4 old.example\n",
            fast_github.END_MARKER + "\n" + fast_github.START_MARKER + "\n",
            (
                    fast_github.START_MARKER + "\n"
                    + fast_github.START_MARKER + "\n"
                    + fast_github.END_MARKER + "\n"
            ),
            (
                    fast_github.START_MARKER + "\n"
                    + fast_github.END_MARKER + "\n"
                    + fast_github.START_MARKER + "\n"
                    + fast_github.END_MARKER + "\n"
            ),
            (
                    fast_github.START_MARKER + " extra\n"
                    + "1.2.3.4 old.example\n"
                    + fast_github.END_MARKER + " extra\n"
            ),
        )
        for original_content in invalid_files:
            with self.subTest(original_content=original_content):
                path = self.make_file(original_content)

                with self.assertRaises(ValueError):
                    fast_github.update_hosts_file(
                        path, "5.6.7.8 new.example\n"
                    )

                self.assertEqual(
                    self.read_text(path), original_content
                )

    # 测试丢弃下载内容中类似标记的注释。
    def test_drops_marker_like_comments_from_download(self):
        path = self.make_file("127.0.0.1 localhost\n")

        result = fast_github.update_hosts_file(
            path,
            fast_github.START_MARKER + " extra\n"
                                       "5.6.7.8 new.example\n",
        )

        self.assertEqual(result.entries, 1)
        self.assertEqual(
            self.read_text(path),
            "127.0.0.1 localhost\n"
            + fast_github.START_MARKER + "\n"
            + "5.6.7.8 new.example\n"
            + fast_github.END_MARKER + "\n",
        )

    # 测试拒绝非法的下载内容。
    def test_rejects_invalid_download_content(self):
        path = self.make_file("127.0.0.1 localhost\n")

        with self.assertRaises(ValueError):
            fast_github.update_hosts_file(path, "upstream error page")

        self.assertEqual(self.read_text(path), "127.0.0.1 localhost\n")

    # 测试内容未变化时不写文件并保持 mtime。
    def test_skips_write_when_content_is_unchanged(self):
        path = self.make_file("127.0.0.1 localhost\n")
        first = fast_github.update_hosts_file(path, "5.6.7.8 new.example\n")
        self.assertTrue(first.changed)
        before = os.stat(path)

        with mock.patch.object(
                fast_github, "_write_hosts_atomically"
        ) as writer:
            second = fast_github.update_hosts_file(
                path, "5.6.7.8 new.example\n"
            )

        writer.assert_not_called()
        self.assertFalse(second.changed)
        self.assertEqual(second.entries, 1)
        self.assertEqual(second.mode, "unchanged")
        self.assertEqual(os.stat(path).st_mtime_ns, before.st_mtime_ns)

    # 测试 dry-run 只返回 diff，不修改文件。
    def test_dry_run_does_not_write(self):
        path = self.make_file("127.0.0.1 localhost\n")

        result = fast_github.update_hosts_file(
            path, "5.6.7.8 new.example\n", dry_run=True
        )

        self.assertTrue(result.changed)
        self.assertEqual(result.entries, 1)
        self.assertEqual(result.mode, "dry_run")
        self.assertIn("+5.6.7.8 new.example", result.diff)
        self.assertIn("+##### FastGitHub Start #####", result.diff)
        self.assertEqual(
            self.read_text(path), "127.0.0.1 localhost\n"
        )

    # 测试 diff 超过行数上限时被截断。
    def test_diff_is_truncated(self):
        original = "127.0.0.1 localhost\n"
        updated = original + "".join(
            "1.2.3.4 host%d.example\n" % index
            for index in range(fast_github.MAX_DIFF_LINES + 50)
        )

        diff = fast_github._build_diff(
            original, updated, "/etc/hosts"
        )

        self.assertIn("diff truncated", diff)
        self.assertLessEqual(
            len(diff.splitlines()),
            fast_github.MAX_DIFF_LINES + 1,
        )

    # 测试 diff 超过字节上限时被截断。
    def test_diff_is_byte_truncated(self):
        original = "127.0.0.1 localhost\n"
        updated = (
                "1.2.3.4 "
                + "a" * (fast_github.MAX_DIFF_BYTES + 1024)
                + ".example\n"
        )

        diff = fast_github._build_diff(
            original, updated, "/etc/hosts"
        )

        self.assertIn("diff truncated", diff)
        self.assertLessEqual(
            len(diff.encode("utf-8")),
            fast_github.MAX_DIFF_BYTES
            + len("\n... diff truncated ...\n"),
        )

    # 测试仅注释变化不触发写入。
    def test_ignores_comment_only_changes(self):
        path = self.make_file("127.0.0.1 localhost\n")
        first = fast_github.update_hosts_file(
            path, "# Update time: one\n1.2.3.4 a.example\n"
        )
        self.assertTrue(first.changed)
        before = os.stat(path)

        second = fast_github.update_hosts_file(
            path, "# Update time: two\n1.2.3.4 a.example\n"
        )

        self.assertFalse(second.changed)
        self.assertEqual(os.stat(path).st_mtime_ns, before.st_mtime_ns)

    # 测试保留 hosts 中原有的非 UTF-8 字节。
    def test_preserves_non_utf8_hosts_content(self):
        path = self.make_file(b"127.0.0.1 localhost\n# caf\xe9\n")

        fast_github.update_hosts_file(path, "5.6.7.8 new.example\n")

        data = self.read_bytes(path)
        self.assertIn(b"# caf\xe9\n", data)
        self.assertIn(b"5.6.7.8 new.example\n", data)

    # 测试区块中只写入 hosts 条目。
    def test_writes_only_host_entries(self):
        path = self.make_file("127.0.0.1 localhost\n")

        result = fast_github.update_hosts_file(
            path,
            "# GitHub520 Host Start\n"
            "140.82.112.26                 alive.github.com\n"
            "20.205.243.168                api.github.com\n"
            "\n"
            "# Update time: 2026-11-22T15:53:05+08:00\n"
            "# Update url: https://raw.hellogithub.com/hosts\n"
            "# Star me: https://github.com/521xueweihan/GitHub520\n"
            "# GitHub520 Host End\n",
        )

        self.assertEqual(result.entries, 2)
        self.assertEqual(
            self.read_text(path),
            "127.0.0.1 localhost\n"
            + fast_github.START_MARKER + "\n"
            + "140.82.112.26                 alive.github.com\n"
            + "20.205.243.168                api.github.com\n"
            + fast_github.END_MARKER + "\n",
        )

    # 测试移除下载内容中的行内注释。
    def test_strips_inline_comments_from_download(self):
        path = self.make_file("127.0.0.1 localhost\n")

        result = fast_github.update_hosts_file(
            path,
            "1.2.3.4 a.example # note\n"
            "5.6.7.8 b.example#note\n",
        )

        self.assertEqual(result.entries, 2)
        self.assertEqual(
            self.read_text(path),
            "127.0.0.1 localhost\n"
            + fast_github.START_MARKER + "\n"
            + "1.2.3.4 a.example\n"
            + "5.6.7.8 b.example\n"
            + fast_github.END_MARKER + "\n",
        )

    # 测试丢弃注释和时间戳元数据。
    def test_drops_comments_when_update_metadata_is_absent(self):
        path = self.make_file("127.0.0.1 localhost\n")

        result = fast_github.update_hosts_file(
            path,
            "# GitHub520 Host Start\n"
            "1.2.3.4 a.example\n"
            "# random comment\n"
            "# GitHub520 Host End\n",
        )

        content = self.read_text(path)
        self.assertEqual(result.entries, 1)
        self.assertIn("1.2.3.4 a.example\n", content)
        self.assertNotIn("GitHub520", content)
        self.assertNotIn("random comment", content)

    # 测试仅用条目替换区块内容。
    def test_replaces_block_with_entries_only(self):
        path = self.make_file(
            "127.0.0.1 localhost\n"
            + fast_github.START_MARKER + "\n"
            + "# GitHub520 Host Start\n"
            + "1.2.3.4 old.example\n"
            + "# Star me: https://github.com/521xueweihan/GitHub520\n"
            + fast_github.END_MARKER + "\n"
        )

        result = fast_github.update_hosts_file(
            path,
            "# Update time: 2026-11-22T15:53:05+08:00\n"
            "# Update url: https://raw.hellogithub.com/hosts\n"
            "5.6.7.8 new.example\n"
            "# other comment\n",
        )

        self.assertEqual(result.entries, 1)
        self.assertEqual(
            self.read_text(path),
            "127.0.0.1 localhost\n"
            + fast_github.START_MARKER + "\n"
            + "5.6.7.8 new.example\n"
            + fast_github.END_MARKER + "\n",
        )

    # 测试接受合法的 IPv6 条目。
    def test_accepts_ipv6_entries(self):
        path = self.make_file("127.0.0.1 localhost\n")

        result = fast_github.update_hosts_file(path, "::1 localhost6\n")

        self.assertEqual(result.entries, 1)
        self.assertIn("::1 localhost6\n", self.read_text(path))

    # 测试写盘失败时保留原文件。
    def test_write_failure_preserves_original_file(self):
        directory = self.make_directory()
        hosts_path = os.path.join(directory, "hosts")
        with open(hosts_path, "wb") as hosts_file:
            hosts_file.write(b"127.0.0.1 localhost\n")

        with mock.patch.object(
                fast_github.os,
                "replace",
                side_effect=OSError(errno.EIO, "replace failed"),
        ):
            with self.assertRaises(OSError):
                fast_github.update_hosts_file(
                    hosts_path, "5.6.7.8 new.example\n"
                )

        self.assertEqual(
            self.read_text(hosts_path), "127.0.0.1 localhost\n"
        )
        self.assertEqual(os.listdir(directory), ["hosts"])

    # 测试临时文件 fdopen 失败时关闭 fd。
    def test_closes_fd_when_temporary_fdopen_fails(self):
        directory = self.make_directory()
        hosts_path = os.path.join(directory, "hosts")
        with open(hosts_path, "wb") as hosts_file:
            hosts_file.write(b"127.0.0.1 localhost\n")

        with mock.patch.object(
                fast_github.os, "fdopen", side_effect=OSError("boom")
        ), mock.patch.object(
            fast_github.os, "close", wraps=os.close
        ) as close:
            with self.assertRaises(OSError):
                fast_github._write_hosts_atomically(hosts_path, "new\n")

        close.assert_called_once()
        self.assertEqual(
            self.read_text(hosts_path), "127.0.0.1 localhost\n"
        )
        self.assertEqual(os.listdir(directory), ["hosts"])

    # 测试拒绝目录等非普通文件。
    def test_rejects_non_regular_file(self):
        with self.assertRaises(OSError):
            fast_github.update_hosts_file(
                self.make_directory(), "5.6.7.8 new.example\n"
            )

    # 测试 EBUSY 时回退为原地写入。
    def test_falls_back_to_in_place_write_on_ebusy(self):
        directory = self.make_directory()
        hosts_path = os.path.join(directory, "hosts")
        with open(hosts_path, "wb") as hosts_file:
            hosts_file.write(b"127.0.0.1 localhost\n")

        with mock.patch.object(
                fast_github.os,
                "replace",
                side_effect=OSError(errno.EBUSY, "device busy"),
        ):
            result = fast_github.update_hosts_file(
                hosts_path, "5.6.7.8 new.example\n"
            )

        self.assertTrue(result.changed)
        self.assertEqual(result.mode, "in_place")
        self.assertIn(
            "5.6.7.8 new.example\n", self.read_text(hosts_path)
        )
        self.assertEqual(os.listdir(directory), ["hosts"])

    # 测试目录不可创建临时文件时回退到原地写入。
    def test_falls_back_to_in_place_when_directory_not_writable(self):
        directory = self.make_directory()
        hosts_path = os.path.join(directory, "hosts")
        with open(hosts_path, "wb") as hosts_file:
            hosts_file.write(b"127.0.0.1 localhost\n")

        with mock.patch.object(
                fast_github.tempfile,
                "mkstemp",
                side_effect=OSError(errno.EACCES, "denied"),
        ):
            result = fast_github.update_hosts_file(
                hosts_path, "5.6.7.8 new.example\n"
            )

        self.assertEqual(result.mode, "in_place")
        self.assertIn(
            "5.6.7.8 new.example\n", self.read_text(hosts_path)
        )
        self.assertEqual(os.listdir(directory), ["hosts"])

    # 测试目录可被其他用户写入时回退到原地写入。
    def test_falls_back_to_in_place_in_unsafe_directory(self):
        directory = self.make_directory()
        os.chmod(directory, 0o777)
        hosts_path = os.path.join(directory, "hosts")
        with open(hosts_path, "wb") as hosts_file:
            hosts_file.write(b"127.0.0.1 localhost\n")

        result = fast_github.update_hosts_file(
            hosts_path, "5.6.7.8 new.example\n"
        )

        self.assertEqual(result.mode, "in_place")
        self.assertIn(
            "5.6.7.8 new.example\n", self.read_text(hosts_path)
        )
        self.assertEqual(os.listdir(directory), ["hosts"])

    # 测试 sticky 目录仍使用原子替换。
    def test_uses_atomic_in_sticky_directory(self):
        directory = self.make_directory()
        os.chmod(directory, 0o1777)
        hosts_path = os.path.join(directory, "hosts")
        with open(hosts_path, "wb") as hosts_file:
            hosts_file.write(b"127.0.0.1 localhost\n")

        result = fast_github.update_hosts_file(
            hosts_path, "5.6.7.8 new.example\n"
        )

        self.assertEqual(result.mode, "atomic")
        self.assertIn(
            "5.6.7.8 new.example\n", self.read_text(hosts_path)
        )
        self.assertEqual(os.listdir(directory), ["hosts"])

    # 测试无法 chown 临时文件时回退到原地写入。
    def test_falls_back_to_in_place_when_chown_fails(self):
        directory = self.make_directory()
        hosts_path = os.path.join(directory, "hosts")
        with open(hosts_path, "wb") as hosts_file:
            hosts_file.write(b"127.0.0.1 localhost\n")

        original_stat = os.stat(hosts_path)
        directory_stat = os.stat(directory)
        temporary_stat = mock.Mock()
        temporary_stat.st_uid = original_stat.st_uid + 1
        temporary_stat.st_gid = original_stat.st_gid

        def fake_stat(path):
            if path == hosts_path:
                return original_stat
            if path == directory:
                return directory_stat
            return temporary_stat

        with mock.patch.object(
                fast_github.os, "stat", side_effect=fake_stat
        ), mock.patch.object(
            fast_github.os,
            "chown",
            side_effect=PermissionError("denied"),
        ):
            result = fast_github.update_hosts_file(
                hosts_path, "5.6.7.8 new.example\n"
            )

        self.assertEqual(result.mode, "in_place")
        self.assertIn(
            "5.6.7.8 new.example\n", self.read_text(hosts_path)
        )
        self.assertEqual(os.listdir(directory), ["hosts"])

    # 测试写入前目标被替换时拒绝更新。
    def test_rejects_replaced_file_before_write(self):
        path = self.make_file("old\n")
        current_stat = os.stat(path)
        expected_stat = (current_stat.st_dev, current_stat.st_ino)
        replacement = self.make_file("replacement\n")
        os.replace(replacement, path)

        with self.assertRaises(OSError):
            fast_github._write_hosts_atomically(
                path, "new\n", expected_stat
            )

        self.assertEqual(self.read_text(path), "replacement\n")

        with self.assertRaises(OSError):
            fast_github._write_hosts_in_place(
                path, "new\n", expected_stat
            )

        self.assertEqual(self.read_text(path), "replacement\n")

    # 测试硬链接文件使用原地写入保留链接。
    def test_preserves_hard_links(self):
        directory = self.make_directory()
        hosts_path = os.path.join(directory, "hosts")
        link_path = os.path.join(directory, "hosts-link")
        with open(hosts_path, "wb") as hosts_file:
            hosts_file.write(b"127.0.0.1 localhost\n")
        os.link(hosts_path, link_path)

        result = fast_github.update_hosts_file(
            hosts_path, "5.6.7.8 new.example\n"
        )

        self.assertTrue(result.changed)
        self.assertEqual(result.mode, "in_place")
        self.assertEqual(
            self.read_bytes(hosts_path), self.read_bytes(link_path)
        )
        self.assertIn(
            b"5.6.7.8 new.example\n", self.read_bytes(link_path)
        )


# 测试下载内容校验和过滤。
class ValidationTests(unittest.TestCase):
    # 测试只返回合法的 hosts 条目。
    def test_returns_only_host_entries(self):
        self.assertEqual(
            fast_github.validate_hosts_content(
                "1.2.3.4 a.example\n# comment\n5.6.7.8 b.example\n"
            ),
            ["1.2.3.4 a.example", "5.6.7.8 b.example"],
        )

    # 测试移除 UTF-8 BOM。
    def test_strips_utf8_bom(self):
        self.assertEqual(
            fast_github.validate_hosts_content(
                "\ufeff1.2.3.4 a.example\n"
            ),
            ["1.2.3.4 a.example"],
        )

    # 测试 CRLF 输入被规范化为 LF。
    def test_normalizes_crlf(self):
        self.assertEqual(
            fast_github.validate_hosts_content(
                "1.2.3.4 a.example\r\n# comment\r\n"
            ),
            ["1.2.3.4 a.example"],
        )

    # 测试移除行内注释。
    def test_drops_inline_comments(self):
        self.assertEqual(
            fast_github.validate_hosts_content(
                "1.2.3.4 a.example # note\n"
                "5.6.7.8 b.example#note\n"
                "9.9.9.9 c.example\n"
            ),
            [
                "1.2.3.4 a.example",
                "5.6.7.8 b.example",
                "9.9.9.9 c.example",
            ],
        )

    # 测试丢弃类似标记的注释行。
    def test_drops_marker_like_comments(self):
        self.assertEqual(
            fast_github.validate_hosts_content(
                fast_github.START_MARKER + " extra\n"
                                           "1.2.3.4 a.example\n"
                + fast_github.END_MARKER + "\n"
            ),
            ["1.2.3.4 a.example"],
        )

    # 测试忽略注释中的控制字符。
    def test_ignores_control_characters_in_comments(self):
        self.assertEqual(
            fast_github.validate_hosts_content(
                "# comment\x0c\n"
                "1.2.3.4 a.example # note\x0c\n"
            ),
            ["1.2.3.4 a.example"],
        )

    # 测试拒绝空内容和纯注释内容。
    def test_rejects_empty_and_comment_only_content(self):
        for content in ("", "\n", "# only\n", "   \n"):
            with self.subTest(content=content):
                with self.assertRaises(ValueError):
                    fast_github.validate_hosts_content(content)

    # 测试拒绝条目中的控制字符。
    def test_rejects_control_characters(self):
        for character in (
                "\x00", "\x0b", "\x0c", "\x1c", "\x7f", "\x85", "\u2028"
        ):
            with self.subTest(character=repr(character)):
                with self.assertRaises(ValueError):
                    fast_github.validate_hosts_content(
                        "1.2.3.4 a.example" + character + "b\n"
                    )

    # 测试拒绝非法 IP、主机名和格式。
    def test_rejects_invalid_entries(self):
        for content in (
                "upstream error page\n",
                "1.2.3.4 bad/name\n",
                "999.1.1.1 a.example\n",
                "1.2.3.4\n",
                "1.2.3.4 # only a comment\n",
        ):
            with self.subTest(content=content):
                with self.assertRaises(ValueError):
                    fast_github.validate_hosts_content(content)

    # 测试主机名边界情况。
    def test_hostname_edge_cases(self):
        self.assertTrue(fast_github._is_valid_hostname("example.com."))
        self.assertTrue(
            fast_github._is_valid_hostname("a" * 63 + ".example")
        )
        self.assertFalse(
            fast_github._is_valid_hostname("a" * 64 + ".example")
        )
        self.assertFalse(
            fast_github._is_valid_hostname("under_score.example")
        )
        self.assertTrue(fast_github._is_valid_hostname("例子.测试"))


# 测试行尾符检测。
class NewlineTests(unittest.TestCase):
    # 测试返回最后一个行尾符。
    def test_returns_last_line_ending(self):
        self.assertEqual(
            fast_github._detect_newline(["a\n", "b\r\n"]), "\r\n"
        )
        self.assertEqual(
            fast_github._detect_newline(["a\r\n", "b\n"]), "\n"
        )

    # 测试没有行尾符时默认使用 LF。
    def test_defaults_to_lf(self):
        self.assertEqual(fast_github._detect_newline(["a"]), "\n")
        self.assertEqual(fast_github._detect_newline([]), "\n")

    # 测试跳过末尾没有行尾符的行。
    def test_skips_lines_without_ending(self):
        self.assertEqual(
            fast_github._detect_newline(["a\r\n", "b"]), "\r\n"
        )


# 测试下载、重试和响应校验。
class DownloadTests(unittest.TestCase):
    URL = "https://example.test/hosts"

    # 构造模拟 urlopen 的响应上下文。
    def _response(self, body, final_url=None):
        response = mock.MagicMock()
        response.geturl.return_value = final_url or self.URL
        response.read.return_value = body
        context = mock.MagicMock()
        context.__enter__.return_value = response
        return context, response

    # 构造带可选响应头的 HTTPError。
    def _http_error(self, code, headers=None):
        error = HTTPError(
            self.URL, code, "error", headers, io.BytesIO(b"")
        )
        self.addCleanup(error.close)
        return error

    # 测试下载内容按 UTF-8 BOM 解码。
    def test_decodes_utf8_bom(self):
        context, _ = self._response(b"\xef\xbb\xbf1.2.3.4 a.example\n")

        with mock.patch.object(
                fast_github, "urlopen", return_value=context
        ):
            result = fast_github.download_hosts(self.URL)

        self.assertEqual(result, "1.2.3.4 a.example\n")

    # 测试拒绝超过 5 MiB 的响应。
    def test_rejects_response_over_size_limit(self):
        context, response = self._response(
            b"x" * (fast_github.MAX_DOWNLOAD_BYTES + 1)
        )

        with mock.patch.object(
                fast_github, "urlopen", return_value=context
        ):
            with self.assertRaises(ValueError):
                fast_github.download_hosts(self.URL)

        response.read.assert_called_once_with(
            fast_github.MAX_DOWNLOAD_BYTES + 1
        )

    # 测试拒绝非 HTTPS 的最终 URL。
    def test_rejects_non_https_redirect(self):
        context, _ = self._response(
            b"1.2.3.4 a.example\n", "http://mirror.test/hosts"
        )

        with mock.patch.object(
                fast_github, "urlopen", return_value=context
        ):
            with self.assertRaises(ValueError):
                fast_github.download_hosts(self.URL)

    # 测试网络瞬时错误会重试。
    def test_retries_transient_errors(self):
        with mock.patch.object(
                fast_github,
                "_download_once",
                side_effect=[URLError("boom"), "content"],
        ), mock.patch.object(fast_github.time, "sleep") as sleep:
            result = fast_github.download_hosts(self.URL)

        self.assertEqual(result, "content")
        sleep.assert_called_once_with(fast_github.RETRY_BASE_SECONDS)

    # 测试 5xx 会重试。
    def test_retries_server_errors(self):
        error = self._http_error(503)
        with mock.patch.object(
                fast_github,
                "_download_once",
                side_effect=[error, "content"],
        ), mock.patch.object(fast_github.time, "sleep"):
            result = fast_github.download_hosts(self.URL)

        self.assertEqual(result, "content")

    # 测试重试前关闭 HTTPError 响应。
    def test_closes_http_error_before_retry(self):
        response = mock.Mock()
        error = HTTPError(
            self.URL, 503, "Unavailable", None, response
        )

        with mock.patch.object(
                fast_github,
                "_download_once",
                side_effect=[error, "content"],
        ), mock.patch.object(fast_github.time, "sleep"):
            fast_github.download_hosts(self.URL)

        response.close.assert_called_once()

    # 测试普通 4xx 不重试。
    def test_does_not_retry_client_errors(self):
        error = self._http_error(404)
        with mock.patch.object(
                fast_github, "_download_once", side_effect=error
        ), mock.patch.object(fast_github.time, "sleep") as sleep:
            with self.assertRaises(HTTPError):
                fast_github.download_hosts(self.URL)

        sleep.assert_not_called()

    # 测试重试次数用尽后抛出最后错误。
    def test_gives_up_after_all_attempts(self):
        with mock.patch.object(
                fast_github,
                "_download_once",
                side_effect=URLError("boom"),
        ) as download, mock.patch.object(fast_github.time, "sleep"):
            with self.assertRaises(URLError):
                fast_github.download_hosts(self.URL)

        self.assertEqual(download.call_count, fast_github.DOWNLOAD_ATTEMPTS)

    # 测试按 Retry-After 指定的秒数等待。
    def test_respects_retry_after(self):
        error = self._http_error(429, headers={"Retry-After": "7"})
        with mock.patch.object(
                fast_github,
                "_download_once",
                side_effect=[error, "content"],
        ), mock.patch.object(fast_github.time, "sleep") as sleep:
            result = fast_github.download_hosts(self.URL)

        self.assertEqual(result, "content")
        sleep.assert_called_once_with(7)

    # 测试 Retry-After 过长时放弃重试。
    def test_gives_up_when_retry_after_is_too_long(self):
        error = self._http_error(429, headers={"Retry-After": "999"})
        with mock.patch.object(
                fast_github, "_download_once", side_effect=error
        ), mock.patch.object(fast_github.time, "sleep") as sleep:
            with self.assertRaises(HTTPError):
                fast_github.download_hosts(self.URL)

        sleep.assert_not_called()


# 测试 hosts 文件写权限检查。
class WritableCheckTests(_TempPathsMixin, unittest.TestCase):
    # 测试接受可写文件。
    def test_accepts_writable_file(self):
        path = self.make_file("127.0.0.1 localhost\n")

        fast_github.check_hosts_writable(path)

    # 测试拒绝不存在的文件。
    def test_rejects_missing_file(self):
        with self.assertRaises(OSError):
            fast_github.check_hosts_writable(
                "/nonexistent/fast-github-hosts"
            )

    # 测试目录只读时仍接受可写文件（后续回退原地写）。
    def test_accepts_writable_file_in_read_only_directory(self):
        path = self.make_file("127.0.0.1 localhost\n")

        with mock.patch.object(
                fast_github.os, "access", return_value=False
        ):
            fast_github.check_hosts_writable(path)

    # 测试拒绝不可写文件。
    def test_rejects_unwritable_file(self):
        path = self.make_file("127.0.0.1 localhost\n")

        with mock.patch.object(
                fast_github.os,
                "open",
                side_effect=OSError(errno.EACCES, "denied"),
        ):
            with self.assertRaises(PermissionError):
                fast_github.check_hosts_writable(path)

    # 测试拒绝 FIFO，避免打开时阻塞。
    def test_rejects_fifo(self):
        path = os.path.join(self.make_directory(), "hosts.fifo")
        os.mkfifo(path)

        with self.assertRaises(OSError):
            fast_github.check_hosts_writable(path)


# 测试 hosts 文件读权限检查。
class ReadableCheckTests(_TempPathsMixin, unittest.TestCase):
    # 测试接受可读普通文件。
    def test_accepts_readable_file(self):
        path = self.make_file("127.0.0.1 localhost\n")

        fast_github.check_hosts_readable(path)

    # 测试可读检查拒绝不存在的文件。
    def test_rejects_missing_file(self):
        with self.assertRaises(OSError):
            fast_github.check_hosts_readable(
                "/nonexistent/fast-github-hosts"
            )

    # 测试拒绝目录。
    def test_rejects_directory(self):
        with self.assertRaises(OSError):
            fast_github.check_hosts_readable(self.make_directory())

    # 测试拒绝 FIFO，避免 dry-run 阻塞。
    def test_rejects_fifo(self):
        path = os.path.join(self.make_directory(), "hosts.fifo")
        os.mkfifo(path)

        with self.assertRaises(OSError):
            fast_github.check_hosts_readable(path)


# 测试单次更新流程。
class UpdateOnceTests(_TempPathsMixin, unittest.TestCase):
    CONTENT = "5.6.7.8 new.example\n"

    # 测试端到端更新及第二次运行不写文件。
    def test_update_end_to_end_and_noop(self):
        path = self.make_file("127.0.0.1 localhost\n")
        logger = mock.Mock()

        with mock.patch.object(fast_github, "HOSTS_FILE", path), \
                mock.patch.object(
                    fast_github,
                    "download_hosts",
                    return_value=self.CONTENT,
                ):
            first = fast_github.update_hosts_once(logger)
        self.assertEqual(first, 0)
        self.assertIn(self.CONTENT, self.read_text(path))
        first_stat = os.stat(path)

        with mock.patch.object(fast_github, "HOSTS_FILE", path), \
                mock.patch.object(
                    fast_github,
                    "download_hosts",
                    return_value=self.CONTENT,
                ):
            second = fast_github.update_hosts_once(logger)

        self.assertEqual(second, 0)
        self.assertEqual(os.stat(path).st_mtime_ns, first_stat.st_mtime_ns)

    # 测试目标不可写时不下载。
    def test_does_not_download_when_hosts_is_not_writable(self):
        logger = mock.Mock()
        path = self.make_file("127.0.0.1 localhost\n")
        with mock.patch.object(
                fast_github,
                "check_hosts_writable",
                side_effect=PermissionError("read-only"),
        ), mock.patch.object(fast_github, "download_hosts") as download:
            result = fast_github.update_hosts_once(
                logger, hosts_file=path
            )

        self.assertEqual(result, 1)
        download.assert_not_called()
        logger.error.assert_called_once()

    # 测试目标不可读时不下载。
    def test_does_not_download_when_hosts_is_not_readable(self):
        logger = mock.Mock()
        path = self.make_file("127.0.0.1 localhost\n")
        with mock.patch.object(
                fast_github,
                "check_hosts_readable",
                side_effect=PermissionError("unreadable"),
        ), mock.patch.object(
            fast_github, "check_hosts_writable"
        ) as writable, mock.patch.object(
            fast_github, "download_hosts"
        ) as download:
            result = fast_github.update_hosts_once(
                logger, hosts_file=path
            )

        self.assertEqual(result, 1)
        download.assert_not_called()
        writable.assert_not_called()
        logger.error.assert_called_once()

    # 测试下载失败时不更新。
    def test_does_not_update_when_download_fails(self):
        logger = mock.Mock()
        path = self.make_file("127.0.0.1 localhost\n")
        with mock.patch.object(fast_github, "check_hosts_writable"), \
                mock.patch.object(
                    fast_github,
                    "download_hosts",
                    side_effect=OSError("network unavailable"),
                ), \
                mock.patch.object(
                    fast_github, "update_hosts_file"
                ) as update:
            result = fast_github.update_hosts_once(
                logger, hosts_file=path
            )

        self.assertEqual(result, 1)
        update.assert_not_called()
        logger.error.assert_called_once()

    # 测试记录写入的条目数。
    def test_logs_updated_entry_count(self):
        logger = mock.Mock()
        path = self.make_file("127.0.0.1 localhost\n")
        with mock.patch.object(fast_github, "check_hosts_writable"), \
                mock.patch.object(
                    fast_github,
                    "download_hosts",
                    return_value=self.CONTENT,
                ), \
                mock.patch.object(
                    fast_github,
                    "update_hosts_file",
                    return_value=fast_github.UpdateResult(
                        True, 3, "atomic", ""
                    ),
                ):
            result = fast_github.update_hosts_once(
                logger, hosts_file=path
            )

        self.assertEqual(result, 0)
        args, _ = logger.info.call_args
        self.assertEqual(args[-1], 3)

    # 测试无变化时记录日志。
    def test_logs_when_nothing_changed(self):
        logger = mock.Mock()
        path = self.make_file("127.0.0.1 localhost\n")
        with mock.patch.object(fast_github, "check_hosts_writable"), \
                mock.patch.object(
                    fast_github,
                    "download_hosts",
                    return_value=self.CONTENT,
                ), \
                mock.patch.object(
                    fast_github,
                    "update_hosts_file",
                    return_value=fast_github.UpdateResult(
                        False, 1, "unchanged", ""
                    ),
                ):
            result = fast_github.update_hosts_once(
                logger, hosts_file=path
            )

        self.assertEqual(result, 0)
        logger.info.assert_called_once()

    # 测试原地写入时给出警告。
    def test_warns_when_written_in_place(self):
        logger = mock.Mock()
        path = self.make_file("127.0.0.1 localhost\n")
        with mock.patch.object(fast_github, "check_hosts_writable"), \
                mock.patch.object(
                    fast_github,
                    "download_hosts",
                    return_value=self.CONTENT,
                ), \
                mock.patch.object(
                    fast_github,
                    "update_hosts_file",
                    return_value=fast_github.UpdateResult(
                        True, 1, "in_place", ""
                    ),
                ):
            result = fast_github.update_hosts_once(
                logger, hosts_file=path
            )

        self.assertEqual(result, 0)
        logger.warning.assert_called_once()

    # 测试 dry-run 跳过写权限检查。
    def test_dry_run_skips_write_check(self):
        logger = mock.Mock()
        hosts_file = self.make_file("127.0.0.1 localhost\n")
        with mock.patch.object(
                fast_github, "check_hosts_writable"
        ) as check, mock.patch.object(
            fast_github,
            "download_hosts",
            return_value=self.CONTENT,
        ), mock.patch.object(
            fast_github,
            "update_hosts_file",
            return_value=fast_github.UpdateResult(
                True, 3, "dry_run", ""
            ),
        ) as update:
            result = fast_github.update_hosts_once(
                logger, hosts_file=hosts_file, dry_run=True
            )

        self.assertEqual(result, 0)
        check.assert_not_called()
        update.assert_called_once_with(
            hosts_file,
            self.CONTENT,
            dry_run=True,
        )
        logger.info.assert_called_once()

    # 测试 dry-run 对缺失目标不下载。
    def test_dry_run_does_not_download_for_missing_file(self):
        logger = mock.Mock()
        missing = os.path.join(self.make_directory(), "missing-hosts")

        with mock.patch.object(
                fast_github, "download_hosts"
        ) as download:
            result = fast_github.update_hosts_once(
                logger, hosts_file=missing, dry_run=True
            )

        self.assertEqual(result, 1)
        download.assert_not_called()
        logger.error.assert_called_once()

    # 测试 dry-run 输出 diff。
    def test_dry_run_logs_diff(self):
        logger = mock.Mock()
        hosts_file = self.make_file("127.0.0.1 localhost\n")
        diff = "--- current\n+++ proposed\n+1.2.3.4 a.example\n"
        with mock.patch.object(
                fast_github, "download_hosts", return_value=self.CONTENT
        ), mock.patch.object(
            fast_github,
            "update_hosts_file",
            return_value=fast_github.UpdateResult(
                True, 1, "dry_run", diff
            ),
        ):
            result = fast_github.update_hosts_once(
                logger, hosts_file=hosts_file, dry_run=True
            )

        self.assertEqual(result, 0)
        self.assertEqual(logger.info.call_count, 2)
        args, _ = logger.info.call_args
        self.assertIn("Proposed changes", args[0])
        self.assertIn("+1.2.3.4 a.example", args[1])


# 模拟 fcntl.flock 的测试替身。
class _FakeFcntl:
    LOCK_EX = 1
    LOCK_NB = 2

    # 初始化模拟 flock 的锁状态。
    def __init__(self):
        self.locked = False

    # 模拟排他锁；已锁定时抛出 EAGAIN。
    def flock(self, file_descriptor, operation):
        if self.locked:
            raise OSError(errno.EAGAIN, "locked")
        self.locked = True


# 测试文件锁行为。
class LockTests(_TempPathsMixin, unittest.TestCase):
    # 测试第二次加锁返回忙碌。
    def test_second_acquire_reports_busy(self):
        lock_path = os.path.join(
            self.make_directory(), "fast-github.lock"
        )
        logger = mock.Mock()

        with mock.patch.object(fast_github, "fcntl", _FakeFcntl()):
            with fast_github.update_lock(logger, lock_path) as first:
                self.assertTrue(first)
                with fast_github.update_lock(
                        logger, lock_path
                ) as second:
                    self.assertFalse(second)

    # 测试锁目录缺失时仍继续。
    def test_missing_lock_directory_does_not_block(self):
        lock_path = os.path.join(
            self.make_directory(), "missing", "fast-github.lock"
        )
        logger = mock.Mock()

        with mock.patch.object(fast_github, "fcntl", _FakeFcntl()):
            with fast_github.update_lock(logger, lock_path) as acquired:
                self.assertTrue(acquired)

        logger.warning.assert_called_once()

    # 测试真实 flock 的互斥性。
    def test_real_flock_is_exclusive(self):
        lock_path = os.path.join(
            self.make_directory(), "fast-github.lock"
        )
        logger = mock.Mock()

        with fast_github.update_lock(logger, lock_path) as first:
            self.assertTrue(first)
            with fast_github.update_lock(
                    logger, lock_path
            ) as second:
                self.assertFalse(second)


# 测试 main 入口、锁和参数行为。
class MainTests(_TempPathsMixin, unittest.TestCase):
    # 测试已有实例运行时跳过。
    def test_skips_when_another_instance_runs(self):
        logger = mock.Mock()
        with mock.patch.object(
                fast_github, "configure_logging", return_value=logger
        ), mock.patch.object(
            fast_github, "update_lock", return_value=_context(False)
        ), mock.patch.object(
            fast_github, "update_hosts_once"
        ) as update_once:
            result = fast_github.main([])

        self.assertEqual(result, 0)
        update_once.assert_not_called()
        logger.info.assert_called_once()

    # 测试获取锁后执行更新。
    def test_runs_update_when_lock_acquired(self):
        logger = mock.Mock()
        with mock.patch.object(
                fast_github, "configure_logging", return_value=logger
        ), mock.patch.object(
            fast_github, "update_lock", return_value=_context(True)
        ), mock.patch.object(
            fast_github, "update_hosts_once", return_value=0
        ) as update_once:
            result = fast_github.main([])

        self.assertEqual(result, 0)
        update_once.assert_called_once_with(
            logger, hosts_file=fast_github.HOSTS_FILE
        )

    # 测试 dry-run 不获取锁。
    def test_dry_run_skips_lock(self):
        logger = mock.Mock()
        with mock.patch.object(
                fast_github, "configure_logging", return_value=logger
        ), mock.patch.object(
            fast_github, "update_lock"
        ) as lock, mock.patch.object(
            fast_github, "update_hosts_once", return_value=0
        ) as update_once:
            result = fast_github.main(["--dry-run"])

        self.assertEqual(result, 0)
        lock.assert_not_called()
        update_once.assert_called_once_with(
            logger,
            hosts_file=fast_github.HOSTS_FILE,
            dry_run=True,
        )

    # 测试自定义 hosts 真实更新仍加锁。
    def test_custom_hosts_file_uses_lock(self):
        logger = mock.Mock()
        custom_path = os.path.join(
            tempfile.gettempdir(), "fast-github-custom-hosts"
        )
        with mock.patch.object(
                fast_github, "configure_logging", return_value=logger
        ), mock.patch.object(
            fast_github, "update_lock", return_value=_context(True)
        ) as lock, mock.patch.object(
            fast_github, "update_hosts_once", return_value=0
        ) as update_once:
            result = fast_github.main(["--hosts-file", custom_path])

        self.assertEqual(result, 0)
        lock.assert_called_once_with(logger)
        update_once.assert_called_once_with(
            logger,
            hosts_file=custom_path,
        )

    # 测试自定义 hosts 的 dry-run 不加锁。
    def test_custom_hosts_file_dry_run_skips_lock(self):
        logger = mock.Mock()
        custom_path = os.path.join(
            tempfile.gettempdir(), "fast-github-custom-hosts"
        )
        with mock.patch.object(
                fast_github, "configure_logging", return_value=logger
        ), mock.patch.object(
            fast_github, "update_lock"
        ) as lock, mock.patch.object(
            fast_github, "update_hosts_once", return_value=0
        ) as update_once:
            result = fast_github.main(
                ["--dry-run", "--hosts-file", custom_path]
            )

        self.assertEqual(result, 0)
        lock.assert_not_called()
        update_once.assert_called_once_with(
            logger,
            hosts_file=custom_path,
            dry_run=True,
        )

    # 测试自定义 hosts 文件的真实更新。
    def test_custom_hosts_file_real_update(self):
        path = self.make_file("127.0.0.1 localhost\n")
        logger = mock.Mock()
        content = "5.6.7.8 new.example\n"
        with mock.patch.object(
                fast_github, "configure_logging", return_value=logger
        ), mock.patch.object(
            fast_github, "update_lock", return_value=_context(True)
        ), mock.patch.object(
            fast_github, "download_hosts", return_value=content
        ):
            result = fast_github.main(["--hosts-file", path])

        self.assertEqual(result, 0)
        self.assertIn(content, self.read_text(path))


# 测试命令行参数解析。
class CommandLineTests(unittest.TestCase):
    # 测试命令行参数默认值。
    def test_defaults(self):
        args = fast_github.parse_args([])
        self.assertEqual(args.hosts_file, fast_github.HOSTS_FILE)
        self.assertFalse(args.dry_run)

    # 测试解析 --dry-run 和 --hosts-file。
    def test_dry_run_and_hosts_file(self):
        args = fast_github.parse_args(
            ["--dry-run", "--hosts-file", "/tmp/hosts.test"]
        )
        self.assertTrue(args.dry_run)
        self.assertEqual(args.hosts_file, "/tmp/hosts.test")

    # 测试 --help 正常退出并显示选项。
    def test_help_exits_zero(self):
        with mock.patch(
                "sys.stdout", new_callable=io.StringIO
        ) as stdout:
            with self.assertRaises(SystemExit) as context:
                fast_github.parse_args(["--help"])

        self.assertEqual(context.exception.code, 0)
        output = stdout.getvalue()
        self.assertIn("--dry-run", output)
        self.assertIn("--hosts-file", output)


# 测试日志文件的安全打开。
class OpenLogStreamTests(_TempPathsMixin, unittest.TestCase):
    # 测试拒绝符号链接日志文件。
    def test_rejects_symbolic_links(self):
        directory = self.make_directory()
        target_path = os.path.join(directory, "target.log")
        link_path = os.path.join(directory, "fast-github.log")
        with open(target_path, "w") as target_file:
            target_file.write("unchanged")
        os.symlink(target_path, link_path)

        with self.assertRaises(OSError):
            fast_github.open_log_stream(link_path)

        with open(target_path, "r") as target_file:
            self.assertEqual(target_file.read(), "unchanged")

    # 测试 FIFO 日志文件不会阻塞打开。
    def test_rejects_fifo_without_blocking(self):
        fifo_path = os.path.join(
            self.make_directory(), "fast-github.log"
        )
        os.mkfifo(fifo_path)
        code = (
                   "import importlib.util\n"
                   "spec = importlib.util.spec_from_file_location("
                   "'fg', %r)\n"
                   "module = importlib.util.module_from_spec(spec)\n"
                   "spec.loader.exec_module(module)\n"
                   "module.open_log_stream(%r)\n"
               ) % (fast_github.__file__, fifo_path)

        try:
            result = subprocess.run(
                [sys.executable, "-B", "-c", code],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=10,
            )
        except subprocess.TimeoutExpired:
            self.fail("open_log_stream blocked on FIFO")

        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(
            b"OSError" in result.stderr
            or b"BlockingIOError" in result.stderr
        )

    # 测试日志 fdopen 失败时关闭 fd。
    def test_closes_fd_when_fdopen_fails(self):
        path = self.make_file(b"")
        with mock.patch.object(
                fast_github.os, "fdopen", side_effect=OSError("boom")
        ), mock.patch.object(
            fast_github.os, "close", wraps=os.close
        ) as close:
            with self.assertRaises(OSError):
                fast_github.open_log_stream(path)

        close.assert_called_once()

    # 测试拒绝其他用户可写的日志文件。
    def test_rejects_world_writable_log_file(self):
        path = self.make_file(b"")
        file_info = mock.Mock()
        file_info.st_mode = stat.S_IFREG | 0o666
        file_info.st_uid = os.geteuid()

        with mock.patch.object(
                fast_github.os, "fstat", return_value=file_info
        ):
            with self.assertRaises(OSError):
                fast_github.open_log_stream(path)

    # 测试拒绝其他用户拥有的日志文件。
    def test_rejects_log_file_owned_by_another_user(self):
        path = self.make_file(b"")
        file_info = mock.Mock()
        file_info.st_mode = stat.S_IFREG | 0o600
        file_info.st_uid = os.geteuid() + 1

        with mock.patch.object(
                fast_github.os, "fstat", return_value=file_info
        ):
            with self.assertRaises(OSError):
                fast_github.open_log_stream(path)


# 测试日志配置。
class LoggingTests(_TempPathsMixin, unittest.TestCase):
    # 测试非 root 不创建文件日志。
    def test_configure_logging_skips_file_for_non_root(self):
        log_path = os.path.join(
            self.make_directory(), "fast-github.log"
        )
        with mock.patch.object(
                fast_github, "LOG_FILE", log_path
        ), mock.patch.object(
            fast_github.os, "geteuid", return_value=1000
        ), mock.patch.object(
            fast_github.sys, "stderr", new_callable=io.StringIO
        ):
            logger = fast_github.configure_logging()

        try:
            self.assertFalse(os.path.exists(log_path))
            self.assertEqual(len(logger.handlers), 1)
        finally:
            for handler in logger.handlers[:]:
                logger.removeHandler(handler)
                handler.close()

    # 测试默认日志不在全局可写的 /tmp。
    def test_log_file_is_not_in_tmp(self):
        self.assertFalse(
            os.path.realpath(fast_github.LOG_FILE).startswith("/tmp/")
        )


if __name__ == "__main__":
    unittest.main()
