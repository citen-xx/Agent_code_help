"""Atomic, workspace-confined text patch application.

Only existing UTF-8 text files can be modified. Every change is validated and
prepared before any target file is replaced; a failed multi-file commit rolls
back files that were already replaced.
"""

import difflib
import logging
import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path

from app.workspace import git
from app.workspace.models import (
    PatchFailureCategory,
    PatchFile,
    PatchResult,
)

logger = logging.getLogger(__name__)

MAX_PATCH_FILE_BYTES = 2 * 1024 * 1024
MAX_PATCH_DIFF_CHARS = 20_000

_UTF8_BOM = b"\xef\xbb\xbf"


class PatchError(RuntimeError):
    """Raised when a patch is unsafe, invalid, or cannot be committed."""

    def __init__(
        self,
        category: PatchFailureCategory,
        message: str,
        *,
        file: str | None = None,
        replacement_index: int | None = None,
    ):
        super().__init__(message)
        self.category = category
        self.file = file
        self.replacement_index = replacement_index


@dataclass
class _PlannedFile:
    relative_path: str
    target: Path
    original_bytes: bytes
    new_bytes: bytes
    mode: int
    original_text: str
    new_text: str
    replacement_count: int


def _validate_path(repo: Path, raw_path: str) -> tuple[str, Path]:
    if not raw_path.strip():
        raise PatchError(
            PatchFailureCategory.INVALID_PATCH_FORMAT,
            "patch 文件路径不能为空",
        )
    if "\x00" in raw_path:
        raise PatchError(
            PatchFailureCategory.PATH_VALIDATION_FAILED,
            "patch 文件路径包含非法字符",
        )
    if raw_path.startswith(("/", "\\")):
        raise PatchError(
            PatchFailureCategory.PATH_VALIDATION_FAILED,
            f"只允许 repository 相对路径: {raw_path!r}",
            file=raw_path,
        )

    relative = Path(raw_path)
    if relative.is_absolute() or relative.drive:
        raise PatchError(
            PatchFailureCategory.PATH_VALIDATION_FAILED,
            f"只允许 repository 相对路径: {raw_path!r}",
            file=raw_path,
        )
    parts = relative.parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise PatchError(
            PatchFailureCategory.PATH_VALIDATION_FAILED,
            f"patch 文件路径非法: {raw_path!r}",
            file=raw_path,
        )
    if any(part.lower() == ".git" for part in parts):
        raise PatchError(
            PatchFailureCategory.PATH_VALIDATION_FAILED,
            "禁止修改 .git 目录",
            file=raw_path,
        )

    candidate = repo
    for part in parts:
        candidate = candidate / part
        if candidate.is_symlink():
            raise PatchError(
                PatchFailureCategory.PATH_VALIDATION_FAILED,
                f"patch 不允许经过符号链接: {raw_path!r}",
                file=raw_path,
            )
    if not candidate.is_file():
        raise PatchError(
            PatchFailureCategory.TARGET_FILE_NOT_FOUND,
            f"patch 目标不是已存在的普通文件: {raw_path!r}",
            file=raw_path,
        )

    resolved = candidate.resolve(strict=True)
    try:
        inside_path = resolved.relative_to(repo)
    except ValueError:
        raise PatchError(
            PatchFailureCategory.PATH_VALIDATION_FAILED,
            f"patch 目标越出 repository: {raw_path!r}",
            file=raw_path,
        ) from None

    file_stat = resolved.stat()
    if file_stat.st_nlink > 1:
        raise PatchError(
            PatchFailureCategory.PATH_VALIDATION_FAILED,
            f"patch 拒绝修改硬链接文件: {raw_path!r}",
            file=raw_path,
        )
    if file_stat.st_size > MAX_PATCH_FILE_BYTES:
        raise PatchError(
            PatchFailureCategory.INVALID_PATCH_FORMAT,
            f"patch 文件过大（>{MAX_PATCH_FILE_BYTES} bytes）: {raw_path!r}",
            file=raw_path,
        )
    return inside_path.as_posix(), resolved


def _decode_utf8(raw: bytes, path: str) -> tuple[str, bool]:
    if b"\x00" in raw:
        raise PatchError(
            PatchFailureCategory.ENCODING_ERROR,
            f"拒绝修改二进制文件: {path}",
            file=path,
        )
    has_bom = raw.startswith(_UTF8_BOM)
    try:
        text = raw.decode("utf-8-sig" if has_bom else "utf-8")
    except UnicodeDecodeError as exc:
        raise PatchError(
            PatchFailureCategory.ENCODING_ERROR,
            f"只允许修改 UTF-8 文本文件: {path}",
            file=path,
        ) from exc
    return text, has_bom


def _encode_utf8(text: str, has_bom: bool, path: str) -> bytes:
    if "\x00" in text:
        raise PatchError(
            PatchFailureCategory.ENCODING_ERROR,
            f"patch 结果包含二进制内容: {path}",
            file=path,
        )
    encoded = text.encode("utf-8-sig" if has_bom else "utf-8")
    if len(encoded) > MAX_PATCH_FILE_BYTES:
        raise PatchError(
            PatchFailureCategory.INVALID_PATCH_FORMAT,
            f"patch 结果文件过大（>{MAX_PATCH_FILE_BYTES} bytes）: {path}",
            file=path,
        )
    return encoded


def _plan_file(
    repo: Path,
    file_patch: PatchFile,
    validated_path: tuple[str, Path] | None = None,
) -> _PlannedFile:
    relative_path, target = validated_path or _validate_path(repo, file_patch.path)
    try:
        raw = target.read_bytes()
    except OSError as exc:
        raise PatchError(
            PatchFailureCategory.WRITE_FAILED,
            f"读取文件失败: {exc}",
            file=relative_path,
        ) from exc
    text, has_bom = _decode_utf8(raw, relative_path)
    updated = text
    for index, replacement in enumerate(file_patch.replacements, start=1):
        occurrences = updated.count(replacement.old_text)
        if occurrences == 0:
            raise PatchError(
                PatchFailureCategory.OLD_TEXT_NOT_FOUND,
                f"replacement {index}: old_text 在 {relative_path} 中出现 0 次",
                file=relative_path,
                replacement_index=index,
            )
        if occurrences != 1:
            raise PatchError(
                PatchFailureCategory.OLD_TEXT_MULTIPLE_MATCHES,
                f"replacement {index}: old_text 在 {relative_path} 中出现 "
                f"{occurrences} 次，必须恰好出现 1 次",
                file=relative_path,
                replacement_index=index,
            )
        updated = updated.replace(replacement.old_text, replacement.new_text, 1)
    return _PlannedFile(
        relative_path=relative_path,
        target=target,
        original_bytes=raw,
        new_bytes=_encode_utf8(updated, has_bom, relative_path),
        mode=stat.S_IMODE(target.stat().st_mode),
        original_text=text,
        new_text=updated,
        replacement_count=len(file_patch.replacements),
    )


def _prepare_temp_file(plan: _PlannedFile) -> Path:
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{plan.target.name}.patch-",
        suffix=".tmp",
        dir=plan.target.parent,
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(plan.new_bytes)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_path, plan.mode)
        return temp_path
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise PatchError(
            PatchFailureCategory.WRITE_FAILED,
            f"无法准备临时文件: {plan.relative_path}",
            file=plan.relative_path,
        )


def _restore_file(plan: _PlannedFile) -> None:
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{plan.target.name}.rollback-",
        suffix=".tmp",
        dir=plan.target.parent,
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(plan.original_bytes)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_path, plan.mode)
        os.replace(temp_path, plan.target)
    finally:
        temp_path.unlink(missing_ok=True)


def _manual_diff(plans: list[_PlannedFile]) -> str:
    chunks: list[str] = []
    for plan in plans:
        old_lines = plan.original_text.splitlines(keepends=True)
        new_lines = plan.new_text.splitlines(keepends=True)
        chunks.extend(
            difflib.unified_diff(
                old_lines,
                new_lines,
                fromfile=f"a/{plan.relative_path}",
                tofile=f"b/{plan.relative_path}",
                lineterm="\n",
            )
        )
    return "".join(chunks)


def _truncate_diff(diff: str) -> tuple[str, bool]:
    if len(diff) <= MAX_PATCH_DIFF_CHARS:
        return diff, False
    omitted = len(diff) - MAX_PATCH_DIFF_CHARS
    return (
        diff[:MAX_PATCH_DIFF_CHARS] + f"\n...[git diff 已截断，省略 {omitted} 个字符]",
        True,
    )


def apply_workspace_patch(
    repository_path: str | Path,
    files: list[PatchFile],
) -> PatchResult:
    """Validate and atomically apply grouped sequential replacements."""
    repo = Path(repository_path).expanduser().resolve()
    if not repo.is_dir():
        raise PatchError(
            PatchFailureCategory.PATH_VALIDATION_FAILED,
            f"repository 不是有效目录: {repository_path}",
        )
    if not files:
        raise PatchError(
            PatchFailureCategory.INVALID_PATCH_FORMAT,
            "patch 至少需要一个文件条目",
        )

    plans: list[_PlannedFile] = []
    seen_paths: set[str] = set()
    for file_patch in files:
        validated_path = _validate_path(repo, file_patch.path)
        relative_path = validated_path[0]
        if relative_path in seen_paths:
            raise PatchError(
                PatchFailureCategory.INVALID_PATCH_FORMAT,
                f"同一路径只能出现一次，多个 replacement 必须放在同一 file entry: "
                f"{relative_path}",
                file=relative_path,
            )
        seen_paths.add(relative_path)
        plan = _plan_file(repo, file_patch, validated_path)
        plans.append(plan)

    temp_files: dict[Path, Path] = {}
    try:
        for plan in plans:
            temp_files[plan.target] = _prepare_temp_file(plan)
    except Exception as exc:
        for temp_path in temp_files.values():
            temp_path.unlink(missing_ok=True)
        if isinstance(exc, PatchError):
            raise
        raise PatchError(
            PatchFailureCategory.WRITE_FAILED,
            f"Patch 准备失败，未修改任何文件: {exc}",
        ) from exc

    replaced: list[_PlannedFile] = []
    try:
        for plan in plans:
            os.replace(temp_files[plan.target], plan.target)
            replaced.append(plan)
    except Exception as exc:
        rollback_errors: list[str] = []
        for plan in reversed(replaced):
            try:
                _restore_file(plan)
            except Exception as rollback_exc:
                rollback_errors.append(f"{plan.relative_path}: {rollback_exc}")
        for target, temp_path in temp_files.items():
            if target not in {plan.target for plan in replaced}:
                temp_path.unlink(missing_ok=True)
        if rollback_errors:
            raise PatchError(
                PatchFailureCategory.WRITE_FAILED,
                "Patch 提交失败且回滚不完整: " + "; ".join(rollback_errors),
            ) from exc
        raise PatchError(
            PatchFailureCategory.WRITE_FAILED,
            f"Patch 提交失败，已回滚全部修改: {exc}",
        ) from exc

    try:
        diff = git.diff(repo)
    except git.GitError:
        logger.warning("Failed to obtain git diff after patch", exc_info=True)
        diff = ""
    if not diff.strip():
        diff = _manual_diff(plans)
    diff, truncated = _truncate_diff(diff)
    return PatchResult(
        affected_files=[plan.relative_path for plan in plans],
        files_requested=len(files),
        replacements_requested=sum(
            len(file_patch.replacements) for file_patch in files
        ),
        successful_replacements=sum(
            plan.replacement_count for plan in plans
        ),
        diff=diff,
        diff_truncated=truncated,
    )
