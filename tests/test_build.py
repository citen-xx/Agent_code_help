"""Controlled build detection and test execution tests — no real Maven/Gradle."""

import os
import subprocess
from pathlib import Path

import pytest

from app.workspace import build
from app.workspace.build import (
    BuildError,
    detect_build_projects,
    run_workspace_tests,
    select_build_project,
)
from app.workspace.models import BuildTool
from tests.build_helpers import patch_popen


def _write(path: Path, text: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def maven_repo(tmp_path):
    repo = tmp_path / "maven"
    _write(repo / "pom.xml", "<project/>")
    _write(repo / "src/main/java/Main.java", "public class Main {}")
    return repo


@pytest.fixture
def gradle_repo(tmp_path):
    repo = tmp_path / "gradle"
    _write(repo / "build.gradle", "plugins { id 'java' }")
    _write(repo / "src/main/java/Main.java", "public class Main {}")
    return repo


def test_detect_maven_project(maven_repo):
    projects = detect_build_projects(maven_repo)
    assert len(projects) == 1
    assert projects[0].tool == BuildTool.MAVEN
    assert projects[0].build_files == ["pom.xml"]
    assert projects[0].command[-1] == "test"


def test_detect_gradle_project(gradle_repo):
    projects = detect_build_projects(gradle_repo)
    assert len(projects) == 1
    assert projects[0].tool == BuildTool.GRADLE
    assert projects[0].command[-2:] == ["--no-daemon", "test"]


def test_detect_multiple_build_tools(maven_repo):
    _write(maven_repo / "build.gradle", "plugins { id 'java' }")
    projects = detect_build_projects(maven_repo)
    assert {project.tool for project in projects} == {
        BuildTool.MAVEN,
        BuildTool.GRADLE,
    }
    with pytest.raises(BuildError, match="多个构建工具"):
        select_build_project(projects, "auto")
    assert select_build_project(projects, "gradle").tool == BuildTool.GRADLE


def test_detect_no_build_project(tmp_path):
    assert detect_build_projects(tmp_path) == []
    with pytest.raises(BuildError, match="未检测到"):
        select_build_project([], "auto")


def test_project_tool_mismatch(maven_repo):
    projects = detect_build_projects(maven_repo)
    with pytest.raises(BuildError, match="不是 gradle"):
        select_build_project(projects, "gradle")


def test_wrapper_is_preferred(maven_repo):
    wrapper_name = "mvnw.cmd" if os.name == "nt" else "mvnw"
    wrapper = maven_repo / wrapper_name
    _write(wrapper, "wrapper")
    project = detect_build_projects(maven_repo)[0]
    assert project.uses_wrapper is True
    assert project.command[0] == str(wrapper)
    assert project.command[1:] == ["test"]


def test_successful_maven_run(maven_repo, monkeypatch):
    captured = patch_popen(
        monkeypatch,
        stdout="Tests run: 3, Failures: 0",
        stderr="",
        returncode=0,
    )

    result = run_workspace_tests(maven_repo, "maven", timeout_seconds=30)

    assert result.success is True
    assert result.exit_code == 0
    assert "Tests run: 3" in result.stdout
    assert result.stderr == ""
    assert result.timed_out is False
    assert captured["command"][-1] == "test"
    assert Path(captured["kwargs"]["cwd"]) == maven_repo
    assert captured["kwargs"]["shell"] is False


def test_failed_test_run_returns_exit_code_and_output(maven_repo, monkeypatch):
    patch_popen(
        monkeypatch,
        stdout="Tests run: 1, Failures: 1",
        stderr="AssertionFailedError",
        returncode=1,
    )

    result = run_workspace_tests(maven_repo, "maven", timeout_seconds=30)

    assert result.success is False
    assert result.exit_code == 1
    assert "Failures: 1" in result.stdout
    assert "AssertionFailedError" in result.stderr
    assert result.error is None


def test_large_output_is_truncated(maven_repo, monkeypatch):
    patch_popen(monkeypatch, stdout="x" * (build.MAX_OUTPUT_CHARS + 500))

    result = run_workspace_tests(maven_repo, "maven", timeout_seconds=30)

    assert result.truncated is True
    assert "输出已截断" in result.stdout
    assert result.stdout.startswith("x" * build.MAX_OUTPUT_CHARS)


def test_timeout_terminates_process(maven_repo, monkeypatch):
    class TimeoutProcess:
        pid = 4243
        returncode = -1

        def __init__(self):
            self.communicate_calls = 0
            self.killed = False

        def communicate(self, timeout=None):
            self.communicate_calls += 1
            if self.communicate_calls == 1:
                raise subprocess.TimeoutExpired(
                    "mvn test",
                    timeout,
                    output="partial stdout",
                    stderr="partial stderr",
                )
            return "partial stdout", "partial stderr"

        def kill(self):
            self.killed = True

    process = TimeoutProcess()
    terminated = []
    monkeypatch.setattr(build, "_POPEN", lambda *args, **kwargs: process)
    monkeypatch.setattr(
        build, "_terminate_process_tree", lambda proc: terminated.append(proc)
    )

    result = run_workspace_tests(maven_repo, "maven", timeout_seconds=1)

    assert terminated == [process]
    assert result.timed_out is True
    assert result.success is False
    assert result.exit_code == -1
    assert "超时" in result.error
    assert "partial stdout" in result.stdout
    assert "partial stderr" in result.stderr


def test_missing_executable_returns_clear_error(maven_repo, monkeypatch):
    def missing(*args, **kwargs):
        raise FileNotFoundError("mvn not found")

    monkeypatch.setattr(build, "_POPEN", missing)

    result = run_workspace_tests(maven_repo, "maven")

    assert result.success is False
    assert result.exit_code is None
    assert "无法启动" in result.error


def test_no_project_returns_structured_error(tmp_path):
    result = run_workspace_tests(tmp_path, "auto")
    assert result.success is False
    assert result.exit_code is None
    assert "未检测到" in result.error
