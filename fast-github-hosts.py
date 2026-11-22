#!/usr/bin/env python3
"""下载 HelloGitHub hosts 并更新系统 hosts 文件中的专属区块。"""

import argparse
import collections
import contextlib
import difflib
import errno
import fcntl
import ipaddress
import logging
import os
import re
import shutil
import stat
import sys
import tempfile
import time
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

# hosts 下载地址
HOSTS_URL = "https://raw.hellogithub.com/hosts"
# 默认目标 hosts 文件
HOSTS_FILE = "/etc/hosts"
# 本地日志文件
LOG_FILE = "/var/log/fast-github.log"
# 全局锁文件
LOCK_FILE = "/run/fast-github.lock"
# 单次下载超时秒数
DOWNLOAD_TIMEOUT_SECONDS = 30
# 最大下载尝试次数
DOWNLOAD_ATTEMPTS = 3
# 重试退避基数秒数
RETRY_BASE_SECONDS = 2
# Retry-After 最大等待秒数
MAX_RETRY_DELAY_SECONDS = 10
# 下载请求 User-Agent
USER_AGENT = "fast-github/1.0"
# 下载大小上限（5 MiB）
MAX_DOWNLOAD_BYTES = 5 * 1024 * 1024
# dry-run diff 最大行数
MAX_DIFF_LINES = 200
# dry-run diff 最大字节数
MAX_DIFF_BYTES = 64 * 1024
# 区块起始标记
START_MARKER = "##### FastGitHub Start #####"
# 区块结束标记
END_MARKER = "##### FastGitHub End #####"
# 需要拒绝的行分隔符
LINE_SEPARATOR_CHARACTERS = ("\x85", "\u2028", "\u2029")
# 主机名单个标签的格式
HOSTNAME_LABEL_PATTERN = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$"
)
# 更新结果：变化/条目数/模式/diff
UpdateResult = collections.namedtuple(
    "UpdateResult", ["changed", "entries", "mode", "diff"]
)


# 安全地打开日志文件，拒绝符号链接和权限异常的文件。
def open_log_stream(log_path):
    # O_NONBLOCK 避免日志路径被替换成 FIFO 时 open 永久阻塞。
    flags = (
            os.O_WRONLY
            | os.O_APPEND
            | os.O_CREAT
            | os.O_NOFOLLOW
            | os.O_NONBLOCK
    )
    file_descriptor = os.open(log_path, flags, 0o600)
    try:
        log_stream = os.fdopen(file_descriptor, "a", encoding="utf-8")
    except Exception:
        try:
            os.close(file_descriptor)
        except OSError:
            pass
        raise
    try:
        file_info = os.fstat(log_stream.fileno())
        if (
                not stat.S_ISREG(file_info.st_mode)
                or file_info.st_uid != os.geteuid()
                or file_info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        ):
            raise OSError(
                "log file must be a regular file owned by the service user "
                "and not writable by other users"
            )
    except (OSError, ValueError):
        log_stream.close()
        raise
    return log_stream


# 配置 journal/stderr 输出，并在 root 下追加本地日志文件。
def configure_logging():
    logger = logging.getLogger("fast-github")
    logger.setLevel(logging.INFO)
    for handler in logger.handlers[:]:
        logger.removeHandler(handler)
        handler.close()

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(message)s"
    )
    console_handler = logging.StreamHandler(sys.stderr)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    if os.geteuid() != 0:
        # 非 root 运行不应创建或占用 root 服务的共享日志文件。
        logger.info("Not running as root; file logging disabled")
        return logger

    try:
        log_stream = open_log_stream(LOG_FILE)
    except OSError as error:
        # 本地日志文件不可用时仍保留 stderr 输出，便于 systemd 收集日志。
        logger.error("Cannot open log file %s: %s", LOG_FILE, error)
        return logger

    file_handler = logging.StreamHandler(log_stream)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    return logger


# 检查目标 hosts 文件是否可写；目录只读时后续会回退到原地写。
def check_hosts_writable(hosts_path):
    if not os.path.isfile(hosts_path):
        raise OSError("hosts file does not exist")

    directory = os.path.dirname(os.path.realpath(hosts_path))
    if not os.path.isdir(directory):
        raise OSError("hosts file directory does not exist")

    # 先确认文件本身可写；目录不可写不再视为失败，原地写仍可完成。
    # O_NONBLOCK 避免文件在检查期间被替换成 FIFO 时阻塞。
    try:
        descriptor = os.open(
            hosts_path, os.O_WRONLY | os.O_NONBLOCK
        )
    except OSError as error:
        raise PermissionError(
            "hosts file is not writable: %s" % error
        ) from error
    try:
        file_info = os.fstat(descriptor)
        if not stat.S_ISREG(file_info.st_mode):
            raise OSError("hosts file is not a regular file")
    finally:
        os.close(descriptor)


# 检查目标 hosts 文件是否存在且为可读普通文件。
def check_hosts_readable(hosts_path):
    descriptor = os.open(
        hosts_path, os.O_RDONLY | os.O_NONBLOCK
    )
    try:
        file_info = os.fstat(descriptor)
        if not stat.S_ISREG(file_info.st_mode):
            raise OSError(
                "hosts file does not exist or is not a regular file"
            )
    finally:
        os.close(descriptor)


# 解析 Retry-After 响应头中的秒数。
def _retry_after_seconds(error):
    headers = getattr(error, "headers", None)
    if headers is None:
        headers = getattr(error, "hdrs", None)
    if headers is None:
        return None
    value = headers.get("Retry-After")
    if not isinstance(value, str):
        return None
    try:
        seconds = int(value.strip())
    except ValueError:
        return None
    if seconds < 0:
        return None
    return seconds


# 计算重试等待时间，过长时返回 None 以放弃重试。
def _retry_delay(error, attempt):
    retry_after = _retry_after_seconds(error)
    if retry_after is None:
        return RETRY_BASE_SECONDS * attempt
    if retry_after > MAX_RETRY_DELAY_SECONDS:
        # 等待太久会超过 systemd 的启动超时；留给下一轮定时任务。
        return None
    return retry_after


# 下载 hosts 内容，处理重试和 Retry-After。
def download_hosts(url):
    request = Request(url, headers={"User-Agent": USER_AGENT})
    last_error = None
    for attempt in range(1, DOWNLOAD_ATTEMPTS + 1):
        try:
            return _download_once(request)
        except HTTPError as error:
            last_error = error
            error.close()
            # 4xx（限流和超时除外）通常重试也没有意义。
            if error.code not in (408, 429) and error.code < 500:
                raise
            delay = _retry_delay(error, attempt)
            if delay is None:
                raise
        except (URLError, HTTPException, OSError) as error:
            last_error = error
            delay = RETRY_BASE_SECONDS * attempt
        if attempt < DOWNLOAD_ATTEMPTS:
            time.sleep(delay)
    if last_error is not None:
        raise last_error
    raise ValueError("download was not attempted")


# 执行单次下载，校验大小、重定向和编码。
def _download_once(request):
    with urlopen(request, timeout=DOWNLOAD_TIMEOUT_SECONDS) as response:
        final_url = response.geturl()
        if not final_url.lower().startswith("https://"):
            raise ValueError(
                "download was redirected to a non-HTTPS URL: %s" % final_url
            )
        content = response.read(MAX_DOWNLOAD_BYTES + 1)
    if len(content) > MAX_DOWNLOAD_BYTES:
        raise ValueError(
            "downloaded hosts file exceeds %d bytes" % MAX_DOWNLOAD_BYTES
        )
    # utf-8-sig 同时兼容带 BOM 的响应；严格的 UTF-8 校验由调用方处理。
    return content.decode("utf-8-sig")


# 校验下载内容，只保留合法的 hosts 条目。
def validate_hosts_content(content):
    """解析、过滤并校验下载内容，返回要写入区块的主机条目。

    所有注释（整行注释和行内 ``# ...`` 注释）以及空行都会被丢弃，
    返回值只包含 "IP 主机名..." 形式的条目。
    """
    if content.startswith("\ufeff"):
        content = content[1:]
    # 只按 LF 切行，避免 str.splitlines() 把 \x0b、\u2028 等当成换行。
    normalized = content.replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    if not lines:
        raise ValueError("downloaded hosts file is empty")

    entries = []
    for line_number, line in enumerate(lines, start=1):
        stripped_line = line.strip()
        if not stripped_line or stripped_line.startswith("#"):
            continue

        # 行内注释同样丢弃，只校验 # 之前的条目部分。
        entry_part = line.split("#", 1)[0]
        for character in entry_part:
            if (
                    (ord(character) < 32 and character not in "\t")
                    or ord(character) == 127
                    or character in LINE_SEPARATOR_CHARACTERS
            ):
                raise ValueError(
                    "downloaded hosts file contains an invalid control "
                    "character on line %d" % line_number
                )

        entry = entry_part.strip()
        fields = re.split(r"[ \t]+", entry)
        if len(fields) < 2:
            raise ValueError("invalid hosts entry on line %d" % line_number)
        try:
            ipaddress.ip_address(fields[0])
        except ValueError as error:
            raise ValueError(
                "invalid IP address on line %d" % line_number
            ) from error
        if any(not _is_valid_hostname(name) for name in fields[1:]):
            raise ValueError("invalid hostname on line %d" % line_number)
        entries.append(entry)

    if not entries:
        raise ValueError("downloaded hosts file contains no host entries")
    return entries


# 校验主机名格式和 IDNA 编码。
def _is_valid_hostname(hostname):
    try:
        ascii_hostname = hostname.encode("idna").decode("ascii")
    except UnicodeError:
        return False

    if ascii_hostname.endswith("."):
        ascii_hostname = ascii_hostname[:-1]
    if not ascii_hostname or len(ascii_hostname) > 253:
        return False

    return all(
        0 < len(label) <= 63 and HOSTNAME_LABEL_PATTERN.match(label)
        for label in ascii_hostname.split(".")
    )


# 返回单行文本使用的行尾符。
def _line_ending(line):
    if line.endswith("\r\n"):
        return "\r\n"
    if line.endswith("\n"):
        return "\n"
    if line.endswith("\r"):
        return "\r"
    return None


# 返回文件末尾最后一个行尾符，缺省为 LF。
def _detect_newline(lines):
    for line in reversed(lines):
        ending = _line_ending(line)
        if ending is not None:
            return ending
    return "\n"


# 解析现有标记区块，检测重复、嵌套和残缺。
def _parse_hosts_blocks(lines):
    """解析标记区块，返回 [(start, end)]；最多允许一个区块。"""
    blocks = []
    pending = None
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped == START_MARKER:
            if pending is not None:
                raise ValueError("hosts file contains invalid FastGitHub markers")
            pending = index
        elif stripped == END_MARKER:
            if pending is None:
                raise ValueError("hosts file contains invalid FastGitHub markers")
            blocks.append((pending, index))
            pending = None
        elif stripped.startswith(START_MARKER) or stripped.startswith(
                END_MARKER
        ):
            # 标记行必须完全一致，带额外文本视为损坏，避免重复追加区块。
            raise ValueError("hosts file contains invalid FastGitHub markers")

    if pending is not None or len(blocks) > 1:
        raise ValueError("hosts file contains invalid FastGitHub markers")
    return blocks


# 生成写入后的完整内容，替换或追加标记区块。
def _build_updated_content(existing_lines, blocks, incoming_lines):
    if not blocks:
        newline = _detect_newline(existing_lines)
        updated_content = "".join(existing_lines)
        if updated_content and _line_ending(existing_lines[-1]) is None:
            updated_content += newline
        block_lines = [START_MARKER] + incoming_lines + [END_MARKER]
        return updated_content + newline.join(block_lines) + newline

    start, end = blocks[0]
    block_newline = _line_ending(existing_lines[start]) or _detect_newline(
        existing_lines
    )
    end_newline = _line_ending(existing_lines[end])
    if end_newline is None:
        end_newline = ""

    return "".join(
        existing_lines[:start]
        + [START_MARKER + block_newline]
        + [line + block_newline for line in incoming_lines]
        + [END_MARKER + end_newline]
        + existing_lines[end + 1:]
    )


# 对目录执行 fsync，确保 rename 持久化。
def _fsync_directory(directory):
    flags = os.O_RDONLY | os.O_DIRECTORY
    try:
        descriptor = os.open(directory, flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        # 某些文件系统不支持对目录 fsync，忽略即可。
        pass
    finally:
        os.close(descriptor)


# 原地重写 hosts 文件，用于 bind mount、硬链接或目录只读。
def _write_hosts_in_place(hosts_path, content, expected_stat=None):
    # O_RDWR 打开后先 fstat 校验 inode，避免路径被替换后截断其他文件。
    descriptor = os.open(
        hosts_path, os.O_RDWR | os.O_NONBLOCK
    )
    try:
        file_info = os.fstat(descriptor)
        if not stat.S_ISREG(file_info.st_mode):
            raise OSError("hosts file is not a regular file")
        if expected_stat is not None and (
                file_info.st_dev, file_info.st_ino
        ) != expected_stat:
            raise OSError("hosts file changed while updating")
        hosts_file = os.fdopen(
            descriptor,
            "r+",
            encoding="utf-8",
            errors="surrogateescape",
            newline="",
        )
    except Exception:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise

    with hosts_file:
        hosts_file.truncate(0)
        hosts_file.seek(0)
        hosts_file.write(content)
        hosts_file.flush()
        os.fsync(hosts_file.fileno())


# 判断目录是否足够安全，允许原子替换而不被其他用户抢占。
def _directory_allows_atomic_replace(hosts_path, file_stat):
    directory = os.path.dirname(os.path.realpath(hosts_path))
    try:
        directory_stat = os.stat(directory)
    except OSError:
        return False
    if not stat.S_ISDIR(directory_stat.st_mode):
        return False

    euid = os.geteuid()
    if euid == 0:
        if directory_stat.st_uid != 0:
            return False
    elif directory_stat.st_uid not in (euid, 0):
        return False

    if not (directory_stat.st_mode & (stat.S_IWGRP | stat.S_IWOTH)):
        return True
    # 全局可写目录只有在 sticky 且目标属于当前用户时才不会被他人替换。
    return bool(
        directory_stat.st_mode & stat.S_ISVTX
        and file_stat.st_uid == euid
    )


# 通过临时文件原子替换 hosts 文件。
def _write_hosts_atomically(hosts_path, content, expected_stat=None):
    """写入 hosts 文件；返回 ``atomic`` 或 ``in_place``。"""
    hosts_path = os.path.realpath(hosts_path)
    original_stat = os.stat(hosts_path)
    if expected_stat is not None and (
            original_stat.st_dev, original_stat.st_ino
    ) != expected_stat:
        raise OSError("hosts file changed while updating")

    if original_stat.st_nlink > 1:
        # rename 会让其他硬链接继续指向旧 inode；原地写保留所有链接。
        _write_hosts_in_place(hosts_path, content, expected_stat)
        return "in_place"

    if not _directory_allows_atomic_replace(hosts_path, original_stat):
        # 目录可被其他用户替换时，rename 仍有竞态；退回原地写。
        _write_hosts_in_place(hosts_path, content, expected_stat)
        return "in_place"

    directory = os.path.dirname(hosts_path)
    try:
        file_descriptor, temporary_path = tempfile.mkstemp(
            prefix=".fast-github-", dir=directory
        )
    except OSError as error:
        if error.errno not in (errno.EACCES, errno.EROFS, errno.EPERM):
            raise
        # 目录不可写时无法创建临时文件，退回原地写入。
        _write_hosts_in_place(hosts_path, content, expected_stat)
        return "in_place"

    try:
        try:
            temporary_file = os.fdopen(
                file_descriptor,
                "w",
                encoding="utf-8",
                errors="surrogateescape",
                newline="",
            )
        except Exception:
            try:
                os.close(file_descriptor)
            except OSError:
                pass
            raise
        with temporary_file:
            temporary_file.write(content)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())

        temporary_stat = os.stat(temporary_path)
        if (
                temporary_stat.st_uid != original_stat.st_uid
                or temporary_stat.st_gid != original_stat.st_gid
        ):
            try:
                os.chown(
                    temporary_path,
                    original_stat.st_uid,
                    original_stat.st_gid,
                )
            except PermissionError:
                # 非 root 无法把临时文件 chown 回原属主；原地写保留属主。
                _write_hosts_in_place(
                    hosts_path, content, expected_stat
                )
                return "in_place"
        shutil.copystat(hosts_path, temporary_path)
        # 保留权限和 SELinux 等属性，同时刷新修改时间。
        os.utime(temporary_path, None)

        current_stat = os.stat(hosts_path)
        if expected_stat is not None and (
                current_stat.st_dev, current_stat.st_ino
        ) != expected_stat:
            raise OSError("hosts file changed while updating")

        try:
            os.replace(temporary_path, hosts_path)
        except OSError as error:
            if error.errno not in (errno.EBUSY, errno.EXDEV):
                raise
            _write_hosts_in_place(hosts_path, content, expected_stat)
            return "in_place"
        else:
            _fsync_directory(directory)
            return "atomic"
    finally:
        try:
            os.unlink(temporary_path)
        except FileNotFoundError:
            pass


# 以非阻塞方式打开普通文本文件，拒绝 FIFO/设备等特殊文件。
def _open_regular_text_file(hosts_path):
    descriptor = os.open(
        hosts_path, os.O_RDONLY | os.O_NONBLOCK
    )
    try:
        file_info = os.fstat(descriptor)
        if not stat.S_ISREG(file_info.st_mode):
            raise OSError("hosts file is not a regular file")
        return os.fdopen(
            descriptor,
            "r",
            encoding="utf-8",
            errors="surrogateescape",
            newline="",
        )
    except Exception:
        os.close(descriptor)
        raise


# 生成 dry-run 使用的 unified diff，并限制行数和字节数。
def _build_diff(original_content, updated_content, hosts_path):
    parts = []
    truncated = False
    for index, line in enumerate(
            difflib.unified_diff(
                original_content.splitlines(True),
                updated_content.splitlines(True),
                fromfile="%s (current)" % hosts_path,
                tofile="%s (proposed)" % hosts_path,
            )
    ):
        if index >= MAX_DIFF_LINES:
            truncated = True
            break
        parts.append(line)

    diff = "".join(parts).encode("utf-8", "backslashreplace")
    if len(diff) > MAX_DIFF_BYTES:
        diff = diff[:MAX_DIFF_BYTES]
        truncated = True
    result = diff.decode("utf-8", "ignore")
    if truncated:
        if result and not result.endswith("\n"):
            result += "\n"
        result += "... diff truncated ...\n"
    return result


# 计算并写入 hosts 更新，dry-run 时只返回 diff。
def update_hosts_file(hosts_path, content, dry_run=False):
    entries = validate_hosts_content(content)
    entry_count = len(entries)

    hosts_path = os.path.realpath(hosts_path)
    with _open_regular_text_file(hosts_path) as hosts_file:
        file_info = os.fstat(hosts_file.fileno())
        expected_stat = (file_info.st_dev, file_info.st_ino)
        existing_lines = hosts_file.readlines()
    original_content = "".join(existing_lines)

    blocks = _parse_hosts_blocks(existing_lines)
    updated_content = _build_updated_content(
        existing_lines, blocks, entries
    )

    if updated_content == original_content:
        return UpdateResult(False, entry_count, "unchanged", "")
    if dry_run:
        diff = _build_diff(original_content, updated_content, hosts_path)
        return UpdateResult(True, entry_count, "dry_run", diff)

    mode = _write_hosts_atomically(
        hosts_path, updated_content, expected_stat
    )
    return UpdateResult(True, entry_count, mode, "")


# 尝试获取排他锁，失败时允许调用方跳过。
@contextlib.contextmanager
def update_lock(logger, lock_path=None):
    """尝试获取排他锁；拿不到锁时 yield False，由调用方跳过本次运行。"""
    if lock_path is None:
        lock_path = LOCK_FILE

    try:
        lock_file = open(lock_path, "a+", encoding="utf-8")
    except OSError as error:
        logger.warning(
            "Cannot open lock file %s: %s; continuing without a lock",
            lock_path,
            error,
        )
        yield True
        return

    try:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        yield True
    finally:
        lock_file.close()


# 执行一次完整的检查、下载和更新流程。
def update_hosts_once(logger, hosts_file=None, dry_run=False):
    if hosts_file is None:
        hosts_file = HOSTS_FILE
    try:
        check_hosts_readable(hosts_file)
    except OSError as error:
        logger.error("Cannot read %s: %s", hosts_file, error)
        return 1
    if not dry_run:
        try:
            check_hosts_writable(hosts_file)
        except OSError as error:
            logger.error("Cannot write to %s: %s", hosts_file, error)
            return 1

    try:
        content = download_hosts(HOSTS_URL)
    except (
            HTTPError,
            URLError,
            HTTPException,
            OSError,
            UnicodeDecodeError,
            ValueError,
    ) as error:
        logger.error("Failed to download %s: %s", HOSTS_URL, error)
        return 1

    try:
        if dry_run:
            result = update_hosts_file(hosts_file, content, dry_run=True)
        else:
            result = update_hosts_file(hosts_file, content)
    except (OSError, UnicodeError, ValueError) as error:
        logger.error("Failed to update %s: %s", hosts_file, error)
        return 1

    if dry_run:
        if result.changed:
            logger.info(
                "Would update %s; %d host entries would be written "
                "(dry run)",
                hosts_file,
                result.entries,
            )
            if result.diff:
                logger.info(
                    "Proposed changes:\n%s", result.diff.rstrip("\n")
                )
        else:
            logger.info("%s is already up to date (dry run)", hosts_file)
        return 0

    if result.changed:
        if result.mode == "in_place":
            logger.warning(
                "Updated %s in place; the write was not atomic "
                "(bind mount, hard links, read-only/unsafe directory, "
                "or ownership fallback)",
                hosts_file,
            )
        logger.info(
            "Updated %s; wrote %d host entries inside the FastGitHub block",
            hosts_file,
            result.entries,
        )
    else:
        logger.info("%s is already up to date; nothing written", hosts_file)
    return 0


# 解析 --dry-run / --hosts-file 等命令行参数。
def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Download HelloGitHub hosts and update the hosts file."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would change without writing the hosts file.",
    )
    parser.add_argument(
        "--hosts-file",
        default=None,
        metavar="PATH",
        help="Hosts file to update (default: %s)." % HOSTS_FILE,
    )
    args = parser.parse_args(argv)
    if args.hosts_file is None:
        args.hosts_file = HOSTS_FILE
    return args


# 解析参数并驱动一次正式更新或 dry-run。
def main(argv=None):
    args = parse_args(argv)
    logger = configure_logging()
    if args.dry_run:
        return update_hosts_once(
            logger,
            hosts_file=args.hosts_file,
            dry_run=True,
        )
    with update_lock(logger) as acquired:
        if not acquired:
            logger.info(
                "Another fast-github instance is running; skipping this run"
            )
            return 0
        return update_hosts_once(logger, hosts_file=args.hosts_file)


if __name__ == "__main__":
    sys.exit(main())
