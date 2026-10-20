"""Ephemeral Docker sandbox for Maven tests.

The workspace repository is the only bind mount. The container has no
network access, dropped capabilities, CPU/memory/PID limits, and a fixed
``mvn test`` entry point. No user-provided command or Maven argument is used.
"""

import locale
import logging
import os
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path

from app.workspace.models import BuildTool, TestRunResult

logger = logging.getLogger(__name__)

MAX_OUTPUT_CHARS = 20_000
CONTAINER_WORKDIR = "/workspace"

_IMAGE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/:@-]*$")
_CPU_RE = re.compile(r"^[0-9]+(?:\.[0-9]+)?$")
_SIZE_RE = re.compile(r"^[0-9]+[bkmgBKMG]?$")

_DOCKER_ENV = {
    **os.environ,
    "DOCKER_CLI_HINTS": "false",
}

#: Injectable aliases keep offline tests isolated from unrelated subprocesses.
_POPEN = subprocess.Popen
_RUN = subprocess.run


class DockerSandboxError(RuntimeError):
    """Raised when sandbox configuration or Docker execution is invalid."""


def _validate_str(value: str, pattern: re.Pattern, label: str) -> str:
    if not value or not pattern.fullmatch(value):
        raise DockerSandboxError(f"{label} 配置非法: {value!r}")
    return value


def build_docker_run_command(
    repository_path: Path,
    *,
    image: str,
    cpus: str,
    memory: str,
    pids_limit: int,
    tmpfs_size: str,
    container_name: str,
) -> list[str]:
    """Build the fixed Docker CLI arguments for one Maven test container."""
    repo = repository_path.resolve()
    if not repo.is_dir():
        raise DockerSandboxError(f"repository 不是有效目录: {repository_path}")
    source = str(repo)
    if any(char in source for char in ",\r\n"):
        raise DockerSandboxError("Workspace 路径包含 Docker mount 不支持的字符")
    _validate_str(image, _IMAGE_RE, "Docker Maven image")
    _validate_str(cpus, _CPU_RE, "Docker CPU limit")
    _validate_str(memory, _SIZE_RE, "Docker memory limit")
    _validate_str(tmpfs_size, _SIZE_RE, "Docker tmpfs size")
    if pids_limit <= 0:
        raise DockerSandboxError("Docker PID limit 必须大于 0")

    return [
        "docker",
        "run",
        "--name",
        container_name,
        "--network",
        "none",
        "--cpus",
        cpus,
        "--memory",
        memory,
        "--memory-swap",
        memory,
        "--pids-limit",
        str(pids_limit),
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--tmpfs",
        f"/tmp:rw,exec,size={tmpfs_size}",
        "--mount",
        f"type=bind,source={source},target={CONTAINER_WORKDIR}",
        "--workdir",
        CONTAINER_WORKDIR,
        "--env",
        "CI=true",
        image,
        "mvn",
        "test",
    ]


def _docker_preflight() -> str | None:
    if shutil.which("docker") is None:
        return "Docker CLI 不可用：请确认 docker 已安装并加入 PATH"
    try:
        result = _RUN(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            capture_output=True,
            text=True,
            encoding=locale.getpreferredencoding(False) or "utf-8",
            errors="replace",
            timeout=15,
            env=_DOCKER_ENV,
            shell=False,
        )
    except FileNotFoundError:
        return "Docker CLI 不可用：请确认 docker 已安装并加入 PATH"
    except subprocess.TimeoutExpired:
        return "Docker 守护进程检查超时"
    except OSError as exc:
        return f"Docker 守护进程不可用: {exc}"
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        return f"Docker 守护进程不可用: {detail or f'exit code {result.returncode}'}"
    return None


def _cleanup_container(container_name: str) -> None:
    try:
        _RUN(
            ["docker", "rm", "-f", container_name],
            capture_output=True,
            text=True,
            encoding=locale.getpreferredencoding(False) or "utf-8",
            errors="replace",
            timeout=20,
            env=_DOCKER_ENV,
            shell=False,
        )
    except (OSError, subprocess.SubprocessError):
        logger.warning("Failed to remove sandbox container %s", container_name, exc_info=True)


def _truncate(text: str, limit: int = MAX_OUTPUT_CHARS) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    omitted = len(text) - limit
    return text[:limit] + f"\n...[输出已截断，省略 {omitted} 个字符]", True


def _decode(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode(locale.getpreferredencoding(False) or "utf-8", errors="replace")
    return value


def _result(
    *,
    exit_code: int | None,
    stdout: str,
    stderr: str,
    timed_out: bool,
    error: str | None,
    duration_seconds: float,
) -> TestRunResult:
    stdout, out_truncated = _truncate(stdout)
    stderr, err_truncated = _truncate(stderr)
    return TestRunResult(
        tool=BuildTool.MAVEN,
        command=["mvn", "test"],
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        success=not timed_out and error is None and exit_code == 0,
        timed_out=timed_out,
        truncated=out_truncated or err_truncated,
        error=error,
        duration_seconds=round(duration_seconds, 3),
    )


def run_maven_in_docker(
    repository_path: str | Path,
    *,
    timeout_seconds: float,
    image: str,
    cpus: str,
    memory: str,
    pids_limit: int,
    tmpfs_size: str,
) -> TestRunResult:
    """Run fixed ``mvn test`` in a fresh, network-isolated container."""
    started = time.monotonic()
    preflight_error = _docker_preflight()
    if preflight_error:
        return _result(
            exit_code=None,
            stdout="",
            stderr="",
            timed_out=False,
            error=preflight_error,
            duration_seconds=time.monotonic() - started,
        )

    container_name = f"java-code-agent-maven-{uuid.uuid4().hex[:12]}"
    try:
        command = build_docker_run_command(
            Path(repository_path),
            image=image,
            cpus=cpus,
            memory=memory,
            pids_limit=pids_limit,
            tmpfs_size=tmpfs_size,
            container_name=container_name,
        )
    except DockerSandboxError as exc:
        return _result(
            exit_code=None,
            stdout="",
            stderr="",
            timed_out=False,
            error=str(exc),
            duration_seconds=time.monotonic() - started,
        )

    timed_out = False
    error: str | None = None
    stdout = ""
    stderr = ""
    try:
        proc = _POPEN(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding=locale.getpreferredencoding(False) or "utf-8",
            errors="replace",
            env=_DOCKER_ENV,
            shell=False,
        )
        try:
            stdout, stderr = proc.communicate(timeout=timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            error = f"Maven Docker 测试超时（>{timeout_seconds:.0f}s），容器已清理"
            _cleanup_container(container_name)
            try:
                stdout, stderr = proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                stdout, stderr = "", ""
            stdout = _decode(exc.stdout) or stdout
            stderr = _decode(exc.stderr) or stderr
        exit_code = proc.returncode
        if not timed_out and exit_code in {125, 126, 127}:
            error = f"Docker 执行失败（exit code {exit_code}）"
    except (FileNotFoundError, PermissionError, OSError) as exc:
        exit_code = None
        error = f"无法启动 Docker: {exc}"
    finally:
        _cleanup_container(container_name)

    return _result(
        exit_code=exit_code,
        stdout=stdout or "",
        stderr=stderr or "",
        timed_out=timed_out,
        error=error,
        duration_seconds=time.monotonic() - started,
    )
