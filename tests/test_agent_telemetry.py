"""Passive tool telemetry tests for Phase 2.5 failure analysis."""

import pytest

from app.agent.graph import _classify_patch_error
from app.agent.telemetry import PatchErrorCategory


@pytest.mark.parametrize(
    ("message", "category"),
    [
        (
            "old_text 在 A.java 中出现 0 次",
            PatchErrorCategory.OLD_TEXT_NOT_FOUND,
        ),
        (
            "old_text 在 A.java 中出现 2 次",
            PatchErrorCategory.OLD_TEXT_MULTIPLE_MATCHES,
        ),
        (
            "1 validation error for ApplyPatchRequest",
            PatchErrorCategory.INVALID_PATCH_FORMAT,
        ),
        (
            "同一路径不能在一次 patch 中重复",
            PatchErrorCategory.INVALID_PATCH_FORMAT,
        ),
        (
            "patch 目标不是已存在的普通文件",
            PatchErrorCategory.TARGET_FILE_NOT_FOUND,
        ),
        (
            "patch 目标越出 repository",
            PatchErrorCategory.PATH_VALIDATION_FAILED,
        ),
        (
            "只允许修改 UTF-8 文本文件",
            PatchErrorCategory.ENCODING_OR_NEWLINE_MISMATCH,
        ),
        (
            "Permission denied",
            PatchErrorCategory.PERMISSION_OR_WORKSPACE_ERROR,
        ),
        ("执行超时", PatchErrorCategory.TIMEOUT),
        ("unexpected failure", PatchErrorCategory.OTHER),
    ],
)
def test_patch_error_classification(message, category):
    assert _classify_patch_error(message) == category
