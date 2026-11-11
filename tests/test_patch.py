"""Transactional workspace patch tests."""

import os
from pathlib import Path

import pytest

from app.workspace.models import (
    PatchFailureCategory,
    PatchFile,
    PatchReplacement,
)
from app.workspace.patch import PatchError, apply_workspace_patch


@pytest.fixture
def workspace(manager, origin_repo):
    return manager.create(origin_repo.as_uri())


def _file(path: str, *pairs: tuple[str, str]) -> PatchFile:
    return PatchFile(
        path=path,
        replacements=[
            PatchReplacement(old_text=old, new_text=new)
            for old, new in pairs
        ],
    )


def test_apply_patch_returns_files_and_git_diff(workspace):
    repo = Path(workspace.repository_path)
    before = (repo / "Main.java").read_text(encoding="utf-8")

    result = apply_workspace_patch(
        repo,
        [
            _file(
                "Main.java",
                ("public class Main {}", "public class Main { int fixed; }"),
            )
        ],
    )

    assert result.success is True
    assert result.affected_files == ["Main.java"]
    assert result.files_requested == 1
    assert result.replacements_requested == 1
    assert result.successful_replacements == 1
    assert "int fixed" in (repo / "Main.java").read_text(encoding="utf-8")
    assert "diff --git" in result.diff
    assert "+public class Main { int fixed; }" in result.diff
    assert before != (repo / "Main.java").read_text(encoding="utf-8")


def test_same_file_multiple_independent_replacements(workspace):
    repo = Path(workspace.repository_path)
    result = apply_workspace_patch(
        repo,
        [
            _file(
                "Main.java",
                ("public class Main {}", "public class Main { int a; }"),
                ("public class Main { int a; }", "public class Main { int a; int b; }"),
            )
        ],
    )
    content = (repo / "Main.java").read_text(encoding="utf-8")
    assert result.success is True
    assert result.replacements_requested == 2
    assert result.successful_replacements == 2
    assert "int a; int b;" in content


def test_sequential_replacement_can_depend_on_previous_result(workspace):
    repo = Path(workspace.repository_path)
    apply_workspace_patch(
        repo,
        [
            _file(
                "Main.java",
                ("public class Main {}", "public class Main { // marker }"),
                ("// marker", "// final"),
            )
        ],
    )
    content = (repo / "Main.java").read_text(encoding="utf-8")
    assert "// final" in content
    assert "// marker" not in content


def test_multiple_files_are_patched_together(workspace):
    repo = Path(workspace.repository_path)
    result = apply_workspace_patch(
        repo,
        [
            _file(
                "Main.java",
                ("public class Main {}", "public class Main { int a; }"),
            ),
            _file(
                "OrderService.java",
                (
                    "public class OrderService {}",
                    "public class OrderService { int b; }",
                ),
            ),
        ],
    )
    assert result.affected_files == ["Main.java", "OrderService.java"]
    assert "int a" in (repo / "Main.java").read_text(encoding="utf-8")
    assert "int b" in (repo / "OrderService.java").read_text(encoding="utf-8")


def test_second_replacement_failure_leaves_file_untouched(workspace):
    repo = Path(workspace.repository_path)
    target = repo / "Main.java"
    before = target.read_bytes()
    with pytest.raises(PatchError) as exc_info:
        apply_workspace_patch(
            repo,
            [
                _file(
                    "Main.java",
                    ("public class Main {}", "public class Main { int a; }"),
                    ("missing text", "replacement"),
                )
            ],
        )
    error = exc_info.value
    assert error.category == PatchFailureCategory.OLD_TEXT_NOT_FOUND
    assert error.file == "Main.java"
    assert error.replacement_index == 2
    assert target.read_bytes() == before


def test_multi_file_failure_leaves_all_files_untouched(workspace):
    repo = Path(workspace.repository_path)
    main_before = (repo / "Main.java").read_bytes()
    service_before = (repo / "OrderService.java").read_bytes()

    with pytest.raises(PatchError) as exc_info:
        apply_workspace_patch(
            repo,
            [
                _file(
                    "Main.java",
                    ("public class Main {}", "public class Main { int a; }"),
                ),
                _file(
                    "OrderService.java",
                    ("missing text", "replacement"),
                ),
            ],
        )

    assert exc_info.value.category == PatchFailureCategory.OLD_TEXT_NOT_FOUND
    assert exc_info.value.file == "OrderService.java"
    assert (repo / "Main.java").read_bytes() == main_before
    assert (repo / "OrderService.java").read_bytes() == service_before


def test_commit_failure_rolls_back_previous_file(workspace, monkeypatch):
    repo = Path(workspace.repository_path)
    main_before = (repo / "Main.java").read_bytes()
    service_before = (repo / "OrderService.java").read_bytes()
    real_replace = os.replace
    calls = 0

    def flaky_replace(source, target):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected replace failure")
        return real_replace(source, target)

    monkeypatch.setattr("app.workspace.patch.os.replace", flaky_replace)

    with pytest.raises(PatchError) as exc_info:
        apply_workspace_patch(
            repo,
            [
                _file(
                    "Main.java",
                    ("public class Main {}", "public class Main { int a; }"),
                ),
                _file(
                    "OrderService.java",
                    (
                        "public class OrderService {}",
                        "public class OrderService { int b; }",
                    ),
                ),
            ],
        )

    assert exc_info.value.category == PatchFailureCategory.WRITE_FAILED
    assert "已回滚" in str(exc_info.value)
    assert (repo / "Main.java").read_bytes() == main_before
    assert (repo / "OrderService.java").read_bytes() == service_before


@pytest.mark.parametrize(
    "path",
    [
        "../outside.txt",
        "..\\outside.txt",
        "/etc/passwd",
        "C:\\Windows\\win.ini",
        ".git/config",
    ],
)
def test_path_escape_and_git_metadata_are_rejected(workspace, path):
    with pytest.raises(PatchError) as exc_info:
        apply_workspace_patch(
            Path(workspace.repository_path),
            [_file(path, ("x", "y"))],
        )
    assert exc_info.value.category == PatchFailureCategory.PATH_VALIDATION_FAILED


def test_symlink_escape_is_rejected(workspace, tmp_path):
    repo = Path(workspace.repository_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    link = repo / "linked.txt"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("当前环境不允许创建符号链接")

    with pytest.raises(PatchError) as exc_info:
        apply_workspace_patch(repo, [_file("linked.txt", ("outside", "changed"))])
    assert exc_info.value.category == PatchFailureCategory.PATH_VALIDATION_FAILED
    assert outside.read_text(encoding="utf-8") == "outside"


def test_hardlink_escape_is_rejected(workspace, tmp_path):
    repo = Path(workspace.repository_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    link = repo / "hardlink.txt"
    try:
        os.link(outside, link)
    except OSError:
        pytest.skip("当前环境不允许创建硬链接")

    with pytest.raises(PatchError) as exc_info:
        apply_workspace_patch(repo, [_file("hardlink.txt", ("outside", "changed"))])
    assert exc_info.value.category == PatchFailureCategory.PATH_VALIDATION_FAILED
    assert outside.read_text(encoding="utf-8") == "outside"


def test_binary_and_non_utf8_files_are_rejected(workspace):
    repo = Path(workspace.repository_path)
    binary = repo / "Binary.java"
    binary.write_bytes(b"class Binary {}\x00")
    with pytest.raises(PatchError) as binary_error:
        apply_workspace_patch(
            repo,
            [_file("Binary.java", ("class Binary {}", "class Changed {}"))],
        )
    assert binary_error.value.category == PatchFailureCategory.ENCODING_ERROR

    gbk = repo / "Gbk.java"
    gbk.write_bytes("public class 中文 {}".encode("gbk"))
    with pytest.raises(PatchError) as gbk_error:
        apply_workspace_patch(
            repo,
            [_file("Gbk.java", ("public class", "class"))],
        )
    assert gbk_error.value.category == PatchFailureCategory.ENCODING_ERROR


def test_ambiguous_old_text_is_rejected_with_replacement_index(workspace):
    repo = Path(workspace.repository_path)
    target = repo / "Repeated.java"
    target.write_text("token token", encoding="utf-8")
    before = target.read_bytes()
    with pytest.raises(PatchError) as exc_info:
        apply_workspace_patch(
            repo,
            [_file("Repeated.java", ("token", "changed"))],
        )
    assert (
        exc_info.value.category
        == PatchFailureCategory.OLD_TEXT_MULTIPLE_MATCHES
    )
    assert exc_info.value.replacement_index == 1
    assert target.read_bytes() == before


def test_duplicate_file_entries_are_rejected(workspace):
    repo = Path(workspace.repository_path)
    before = (repo / "Main.java").read_bytes()
    with pytest.raises(PatchError) as exc_info:
        apply_workspace_patch(
            repo,
            [
                _file("Main.java", ("public class Main {}", "public class Main { int a; }")),
                _file("Main.java", ("public class Main { int a; }", "public class Main { int b; }")),
            ],
        )
    assert exc_info.value.category == PatchFailureCategory.INVALID_PATCH_FORMAT
    assert (repo / "Main.java").read_bytes() == before


@pytest.mark.parametrize("newline", [b"\n", b"\r\n"])
def test_newline_style_is_preserved(workspace, newline):
    repo = Path(workspace.repository_path)
    target = repo / "LineEnding.java"
    target.write_bytes(b"class LineEnding {" + newline + b"    int value;" + newline + b"}" + newline)
    apply_workspace_patch(
        repo,
        [_file("LineEnding.java", ("int value;", "long value;"))],
    )
    raw = target.read_bytes()
    assert b"long value;" in raw
    if newline == b"\r\n":
        assert b"\r\n" in raw
        assert b"\n" not in raw.replace(b"\r\n", b"")
    else:
        assert b"\r\n" not in raw


def test_existing_utf8_bom_is_preserved(workspace):
    repo = Path(workspace.repository_path)
    target = repo / "Bom.java"
    target.write_bytes(b"\xef\xbb\xbfpublic class Bom {}\n")
    apply_workspace_patch(
        repo,
        [_file("Bom.java", ("class Bom", "class BomFixed"))],
    )
    raw = target.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")
    assert b"BomFixed" in raw
