"""Vendored helpers the Tenki backend needs but hermes-agent core does not ship.

Each of these was written for the original in-core Tenki backend (hermes-agent
PR #64190) and is generic enough to live upstream; until the corresponding
upstream PRs land, the plugin carries its own copies so it runs against a
stock hermes-agent checkout. Everything here is stdlib-only.

Contents:

* ``_windows_replace_file_write_through`` / ``_atomic_save_json_durable`` —
  crash-durable atomic JSON commit (candidate home:
  ``tools/environments/base.py``).
* ``_rewrite_sudo_command_words`` (+ its shell tokenizer) — rewrite real,
  unquoted ``sudo`` command words only (candidate home:
  ``tools/terminal_tool.py``, which today ships the same tokenizer inside
  ``_rewrite_real_sudo_invocations``).
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Callable


def _windows_replace_file_write_through(source: str, destination: Path) -> None:
    """Atomically replace *destination* with Windows write-through semantics."""
    import ctypes
    from ctypes import wintypes

    move_file_ex = ctypes.WinDLL(
        "kernel32",
        use_last_error=True,
    ).MoveFileExW
    move_file_ex.argtypes = (
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.DWORD,
    )
    move_file_ex.restype = wintypes.BOOL
    flags = 0x1 | 0x8  # MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH
    if move_file_ex(
        os.path.abspath(source),
        os.path.abspath(destination),
        flags,
    ):
        return
    error_code = ctypes.get_last_error()
    raise OSError(
        error_code,
        ctypes.FormatError(error_code),
        str(destination),
    )


def _atomic_save_json_durable(
    path: Path,
    data: dict,
    *,
    subject: str,
    store_label: str,
    commit_uncertain_error: Callable[[str], BaseException],
    platform: str,
    replace_write_through: Callable[[str, Path], None],
) -> None:
    """Write *data* to *path* as JSON with a crash-durable atomic commit.

    Mechanism only; the caller supplies the policy. Contents are fsynced before
    the replace, and on POSIX the containing directory is fsynced afterwards so
    the rename itself survives a host crash. A failure of either durability
    step is an *uncertain commit*: the visible file is already the new one, but
    a crash may roll the directory entry back. That case raises
    *commit_uncertain_error* so a caller with recovery state can keep both the
    old and the new referents alive.

    Every policy input is required rather than defaulted: a caller that reaches
    for this primitive is one with recovery state to protect, and silently
    inheriting a generic uncertain-commit error or this module's own platform
    detection would hide exactly the substitutions such a caller depends on.
    *platform* and *replace_write_through* in particular let it supply its own
    (testable) platform detection and Windows replace implementation.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as tmp:
            json.dump(data, tmp, indent=2)
            tmp.flush()
            os.fsync(tmp.fileno())
            temp_path = tmp.name
        if platform == "nt":
            try:
                replace_write_through(temp_path, path)
            except OSError as exc:
                raise commit_uncertain_error(
                    f"could not durably replace {subject} file {path}"
                ) from exc
            return
        if platform != "posix":
            raise RuntimeError(
                f"{store_label} cannot prove rename durability on "
                f"platform {platform!r}"
            )

        os.replace(temp_path, path)
        # The file contents were fsynced before replace; fsync the containing
        # directory too so the pointer rename itself survives a host crash.
        # A POSIX failure is an *uncertain commit*: the visible file is new,
        # but a crash may roll the directory entry back. The caller must keep
        # every referent of both the old and the new state in that case.
        dir_fd: int | None = None
        try:
            flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            dir_fd = os.open(path.parent, flags)
            os.fsync(dir_fd)
        except OSError as exc:
            raise commit_uncertain_error(
                f"could not fsync {subject} directory {path.parent}"
            ) from exc
        finally:
            if dir_fd is not None:
                try:
                    os.close(dir_fd)
                except OSError:
                    pass
    finally:
        if temp_path and os.path.exists(temp_path):
            try:
                os.unlink(temp_path)
            except OSError:
                pass


def _looks_like_env_assignment(token: str) -> bool:
    """Return True when *token* is a leading shell environment assignment."""
    if "=" not in token or token.startswith("="):
        return False
    name, _value = token.split("=", 1)
    return bool(re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", name))


def _read_shell_token(command: str, start: int) -> tuple[str, int]:
    """Read one shell token, preserving quotes/escapes, starting at *start*."""
    i = start
    n = len(command)

    while i < n:
        ch = command[i]
        if ch.isspace() or ch in ";|&()":
            break
        if ch == "'":
            i += 1
            while i < n and command[i] != "'":
                i += 1
            if i < n:
                i += 1
            continue
        if ch == '"':
            i += 1
            while i < n:
                inner = command[i]
                if inner == "\\" and i + 1 < n:
                    i += 2
                    continue
                if inner == '"':
                    i += 1
                    break
                i += 1
            continue
        if ch == "\\" and i + 1 < n:
            i += 2
            continue
        i += 1

    return command[start:i], i


def _rewrite_sudo_command_words(command: str, replacement: str) -> tuple[str, int]:
    """Replace real unquoted sudo command words with *replacement*.

    Only command words are rewritten, so plain text mentions of sudo (arguments,
    quoted strings, comments) are left alone. Returns the rewritten command and
    the number of sudo command words replaced.
    """
    out: list[str] = []
    i = 0
    n = len(command)
    command_start = True
    sudo_count = 0

    while i < n:
        ch = command[i]

        if ch.isspace():
            out.append(ch)
            if ch == "\n":
                command_start = True
            i += 1
            continue

        if ch == "#" and command_start:
            comment_end = command.find("\n", i)
            if comment_end == -1:
                out.append(command[i:])
                break
            out.append(command[i:comment_end])
            i = comment_end
            continue

        if command.startswith("&&", i) or command.startswith("||", i) or command.startswith(";;", i):
            out.append(command[i:i + 2])
            i += 2
            command_start = True
            continue

        if ch in ";|&(":
            out.append(ch)
            i += 1
            command_start = True
            continue

        if ch == ")":
            out.append(ch)
            i += 1
            command_start = False
            continue

        token, next_i = _read_shell_token(command, i)
        if command_start and token == "sudo":
            out.append(replacement)
            sudo_count += 1
        else:
            out.append(token)

        if command_start and _looks_like_env_assignment(token):
            command_start = True
        else:
            command_start = False
        i = next_i

    return "".join(out), sudo_count
