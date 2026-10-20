"""Unit tests for the read-only repository tools."""

import pytest

from app.tools.repo import ToolError, list_files, read_file, resolve_inside, search_code


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "Main.java").write_text("public class Main {}\n", encoding="utf-8")
    (tmp_path / "src" / "OrderService.java").write_text(
        "public class OrderService {\n"
        "    public Order createOrder() { return new Order(); }\n"
        "}\n",
        encoding="utf-8",
    )
    (tmp_path / "README.md").write_text("# demo repo\n", encoding="utf-8")
    return tmp_path


def test_list_files_lists_repo_root(repo):
    out = list_files(repo)
    assert "- src/" in out
    assert "- README.md" in out
    assert "OrderService.java" not in out  # only the root listing


def test_list_files_lists_subdirectory(repo):
    out = list_files(repo, "src")
    assert "- src/Main.java" in out
    assert "- src/OrderService.java" in out


def test_list_files_missing_path_raises(repo):
    with pytest.raises(ToolError):
        list_files(repo, "nope")


def test_read_file_returns_content(repo):
    assert read_file(repo, "src/Main.java") == "public class Main {}\n"


def test_read_file_missing_raises(repo):
    with pytest.raises(ToolError):
        read_file(repo, "src/Missing.java")


def test_read_file_directory_raises(repo):
    with pytest.raises(ToolError):
        read_file(repo, "src")


def test_path_traversal_blocked(repo, tmp_path):
    outside = tmp_path.parent / "secret_outside_repo.txt"
    outside.write_text("secret", encoding="utf-8")
    with pytest.raises(ToolError):
        read_file(repo, "../secret_outside_repo.txt")
    with pytest.raises(ToolError):
        read_file(repo, "src/../../secret_outside_repo.txt")
    with pytest.raises(ToolError):
        read_file(repo, str(outside))  # absolute path outside the repo
    with pytest.raises(ToolError):
        list_files(repo, "..")


def test_parent_refs_inside_repo_are_allowed(repo):
    assert read_file(repo, "src/../src/Main.java") == "public class Main {}\n"


def test_resolve_inside_blocks_traversal(repo):
    assert resolve_inside(repo, "src") == (repo / "src").resolve()
    assert resolve_inside(repo, ".") == repo.resolve()
    with pytest.raises(ToolError):
        resolve_inside(repo, "..")
    with pytest.raises(ToolError):
        resolve_inside(repo, "src/../..")


def test_search_code_finds_matches(repo):
    out = search_code(repo, "createOrder")
    assert "Found 1 match" in out
    assert "src/OrderService.java:2:" in out


def test_search_code_case_insensitive(repo):
    out = search_code(repo, "CREATEORDER")
    assert "src/OrderService.java:2:" in out


def test_search_code_no_matches(repo):
    out = search_code(repo, "nonexistentSymbol")
    assert "未找到" in out


def test_search_code_file_pattern(repo):
    assert "OrderService.java" in search_code(repo, "createOrder", file_pattern="*.java")
    assert "未找到" in search_code(repo, "createOrder", file_pattern="*.md")


def test_search_code_empty_query_raises(repo):
    with pytest.raises(ToolError):
        search_code(repo, "   ")
