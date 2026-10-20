"""Docker Maven sandbox tests, fully offline via a fake Docker CLI."""

import subprocess
from pathlib import Path

import pytest

from app.workspace import docker_sandbox
from app.workspace.docker_sandbox import (
    build_docker_run_command,
    run_maven_in_docker,
)
from tests.build_helpers import FakeProcess


@pytest.fixture
def repo(tmp_path):
    repo = tmp_path / "repository"
    repo.mkdir()
    (repo / "pom.xml").write_text("<project/>", encoding="utf-8")
    return repo


def _run(monkeypatch, repo, *, stdout="", stderr="", returncode=0, timeout=30):
    monkeypatch.setattr("app.workspace.docker_sandbox._docker_preflight", lambda: None)
    process = FakeProcess(stdout=stdout, stderr=stderr, returncode=returncode)
    monkeypatch.setattr(
        "app.workspace.docker_sandbox._POPEN",
        lambda *args, **kwargs: process,
    )
    cleaned: list[str] = []
    monkeypatch.setattr(
        "app.workspace.docker_sandbox._cleanup_container",
        lambda name: cleaned.append(name),
    )
    result = run_maven_in_docker(
        repo,
        timeout_seconds=timeout,
        image="maven:3.9-eclipse-temurin-17",
        cpus="0.5",
        memory="512m",
        pids_limit=64,
        tmpfs_size="128m",
    )
    return result, cleaned


def test_build_command_has_security_and_resource_limits(repo):
    command = build_docker_run_command(
        repo,
        image="maven:3.9-eclipse-temurin-17",
        cpus="0.5",
        memory="512m",
        pids_limit=64,
        tmpfs_size="128m",
        container_name="java-code-agent-maven-test",
    )
    assert command[0:2] == ["docker", "run"]
    assert command[-2:] == ["mvn", "test"]
    assert command[command.index("--network") + 1] == "none"
    assert command[command.index("--cpus") + 1] == "0.5"
    assert command[command.index("--memory") + 1] == "512m"
    assert command[command.index("--pids-limit") + 1] == "64"
    assert command[command.index("--workdir") + 1] == "/workspace"
    assert "exec" in command[command.index("--tmpfs") + 1]
    assert command.count("--mount") == 1
    assert f"source={repo.resolve()}" in command[command.index("--mount") + 1]
    assert "target=/workspace" in command[command.index("--mount") + 1]
    assert "--privileged" not in command
    assert "docker.sock" not in "\n".join(command)


def test_maven_success(repo, monkeypatch):
    result, cleaned = _run(
        monkeypatch,
        repo,
        stdout="Tests run: 2, Failures: 0",
        returncode=0,
    )
    assert result.success is True
    assert result.exit_code == 0
    assert result.stdout == "Tests run: 2, Failures: 0"
    assert result.tool == "maven"
    assert result.command == ["mvn", "test"]
    assert cleaned


def test_maven_failure(repo, monkeypatch):
    result, cleaned = _run(
        monkeypatch,
        repo,
        stdout="Tests run: 1, Failures: 1",
        stderr="assertion failed",
        returncode=1,
    )
    assert result.success is False
    assert result.exit_code == 1
    assert result.stdout == "Tests run: 1, Failures: 1"
    assert result.stderr == "assertion failed"
    assert result.error is None
    assert cleaned


def test_timeout_returns_partial_output_and_cleans_container(repo, monkeypatch):
    class TimeoutProcess:
        pid = 5252
        returncode = -1

        def __init__(self):
            self.calls = 0

        def communicate(self, timeout=None):
            self.calls += 1
            if self.calls == 1:
                raise subprocess.TimeoutExpired(
                    "docker run",
                    timeout,
                    output="partial stdout",
                    stderr="partial stderr",
                )
            return "partial stdout", "partial stderr"

        def kill(self):
            pass

    process = TimeoutProcess()
    cleaned: list[str] = []
    monkeypatch.setattr("app.workspace.docker_sandbox._docker_preflight", lambda: None)
    monkeypatch.setattr(
        "app.workspace.docker_sandbox._POPEN",
        lambda *args, **kwargs: process,
    )
    monkeypatch.setattr(
        "app.workspace.docker_sandbox._cleanup_container",
        lambda name: cleaned.append(name),
    )

    result = run_maven_in_docker(
        repo,
        timeout_seconds=1,
        image="maven:3.9-eclipse-temurin-17",
        cpus="1",
        memory="1g",
        pids_limit=32,
        tmpfs_size="64m",
    )

    assert result.timed_out is True
    assert result.success is False
    assert "超时" in result.error
    assert result.stdout == "partial stdout"
    assert result.stderr == "partial stderr"
    assert cleaned


def test_docker_unavailable_is_structured(repo, monkeypatch):
    monkeypatch.setattr(docker_sandbox.shutil, "which", lambda _: None)
    result = run_maven_in_docker(
        repo,
        timeout_seconds=10,
        image="maven:3.9-eclipse-temurin-17",
        cpus="1",
        memory="1g",
        pids_limit=32,
        tmpfs_size="64m",
    )
    assert result.success is False
    assert result.exit_code is None
    assert "Docker CLI 不可用" in result.error


def test_docker_daemon_unavailable_is_structured(repo, monkeypatch):
    class Result:
        returncode = 1
        stdout = ""
        stderr = "Cannot connect to the Docker daemon"

    monkeypatch.setattr(docker_sandbox.shutil, "which", lambda _: "docker")
    monkeypatch.setattr(docker_sandbox, "_RUN", lambda *args, **kwargs: Result())
    result = run_maven_in_docker(
        repo,
        timeout_seconds=10,
        image="maven:3.9-eclipse-temurin-17",
        cpus="1",
        memory="1g",
        pids_limit=32,
        tmpfs_size="64m",
    )
    assert result.success is False
    assert result.exit_code is None
    assert "守护进程不可用" in result.error
