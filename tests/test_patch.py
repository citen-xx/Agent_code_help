"""Atomic workspace patch tests."""

import os
from pathlib import Path

import pytest

from app.workspace.models import PatchChange
from app.workspace.patch import (
    PatchError,
    apply_workspace_patch,
)


@pytest.fixture
def workspace(manager, origin_repo):
    return manager.create(origin_repo.as_uri())


def _change(path: str, old: str, new: str) -> PatchChange:
    return PatchChange(path=path, old_text=old, new_text=new)


def test_apply_patch_returns_files_and_git_diff(workspace):
    repo = Path(workspace.repository_path)
    before = (repo / "Main.java").read_text(encoding="utf-8")

    result = apply_workspace_patch(
        repo,
        [_change("Main.java", "public class Main {}", "public class Main { int fixed; }")],
    )

    assert result.success is True
    assert result.affected_files == ["Main.java"]
    assert "int fixed" in (repo / "Main.java").read_text(encoding="utf-8")
    assert "diff --git" in result.diff
    assert "+public class Main { int fixed; }" in result.diff
    assert before != (repo / "Main.java").read_text(encoding="utf-8")


def test_multiple_files_are_patched_together(workspace):
    repo = Path(workspace.repository_path)
    result = apply_workspace_patch(
        repo,
        [
            _change("Main.java", "public class Main {}", "public class Main { int a; }"),
            _change(
                "OrderService.java",
                "public class OrderService {}",
                "public class OrderService { int b; }",
            ),
        ],
    )
    assert result.affected_files == ["Main.java", "OrderService.java"]
    assert "int a" in (repo / "Main.java").read_text(encoding="utf-8")
    assert "int b" in (repo / "OrderService.java").read_text(encoding="utf-8")


def test_invalid_second_change_leaves_all_files_untouched(workspace):
    repo = Path(workspace.repository_path)
    main_before = (repo / "Main.java").read_bytes()
    service_before = (repo / "OrderService.java").read_bytes()

    with pytest.raises(PatchError, match="恰好出现 1 次"):
        apply_workspace_patch(
            repo,
            [
                _change("Main.java", "public class Main {}", "public class Main { int a; }"),
                _change("OrderService.java", "missing text", "replacement"),
            ],
        )

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

    with pytest.raises(PatchError, match="已回滚"):
        apply_workspace_patch(
            repo,
            [
                _change("Main.java", "public class Main {}", "public class Main { int a; }"),
                _change(
                    "OrderService.java",
                    "public class OrderService {}",
                    "public class OrderService { int b; }",
                ),
            ],
        )

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
    with pytest.raises(PatchError):
        apply_workspace_patch(
            Path(workspace.repository_path),
            [_change(path, "x", "y")],
        )


def test_symlink_escape_is_rejected(workspace, tmp_path):
    repo = Path(workspace.repository_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    link = repo / "linked.txt"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("当前环境不允许创建符号链接")

    with pytest.raises(PatchError, match="符号链接"):
        apply_workspace_patch(repo, [_change("linked.txt", "outside", "changed")])
    assert outside.read_text(encoding="utf-8") == "outside"


def test_traversal_does_not_modify_outside_file(workspace, tmp_path):
    repo = Path(workspace.repository_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")

    with pytest.raises(PatchError):
        apply_workspace_patch(repo, [_change("../outside.txt", "outside", "changed")])

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

    with pytest.raises(PatchError, match="硬链接"):
        apply_workspace_patch(repo, [_change("hardlink.txt", "outside", "changed")])
    assert outside.read_text(encoding="utf-8") == "outside"


def test_binary_and_non_utf8_files_are_rejected(workspace):
    repo = Path(workspace.repository_path)
    binary = repo / "Binary.java"
    binary.write_bytes(b"class Binary {}\x00")
    with pytest.raises(PatchError, match="二进制"):
        apply_workspace_patch(repo, [_change("Binary.java", "class Binary {}", "class Changed {}")])

    gbk = repo / "Gbk.java"
    gbk.write_bytes("public class 中文 {}".encode("gbk"))
    with pytest.raises(PatchError, match="UTF-8"):
        apply_workspace_patch(repo, [_change("Gbk.java", "public class", "class")])


def test_ambiguous_old_text_is_rejected(workspace):
    repo = Path(workspace.repository_path)
    target = repo / "Repeated.java"
    target.write_text("token token", encoding="utf-8")
    with pytest.raises(PatchError, match="出现 2 次"):
        apply_workspace_patch(repo, [_change("Repeated.java", "token", "changed")])


def test_duplicate_paths_are_rejected(workspace):
    repo = Path(workspace.repository_path)
    with pytest.raises(PatchError, match="不能在一次 patch 中重复"):
        apply_workspace_patch(
            repo,
            [
                _change("Main.java", "public class Main {}", "public class Main { int a; }"),
                _change("Main.java", "public class Main {}", "public class Main { int b; }"),
            ],
        )


def test_existing_utf8_bom_is_preserved(workspace):
    repo = Path(workspace.repository_path)
    target = repo / "Bom.java"
    target.write_bytes(b"\xef\xbb\xbfpublic class Bom {}\n")
    apply_workspace_patch(repo, [_change("Bom.java", "class Bom", "class BomFixed")])
    raw = target.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")
    assert b"BomFixed" in raw
