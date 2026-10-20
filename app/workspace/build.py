"""Controlled Maven/Gradle test execution.

Only the fixed ``test`` lifecycle is supported. No user-provided arguments,
shell expansion, or arbitrary working directory are accepted. The caller
must pass a repository already resolved and bound to a workspace.
"""

import locale
import logging
import os
import signal
import subprocess
import time
from pathlib import Path

from app.workspace.models import BuildProjectInfo, BuildTool, TestRunResult

logger = logging.getLogger(__name__)

DEFAULT_TEST_TIMEOUT_SECONDS = 300.0
MAX_OUTPUT_CHARS = 20_000

MAVEN_BUILD_FILES = ("pom.xml",)
GRADLE_BUILD_FILES = (
    "build.gradle",
    "build.gradle.kts",
    "settings.gradle",
    "settings.gradle.kts",
)

_BUILD_ENV = {
    **os.environ,
    "CI": "true",
    "TERM": "dumb",
    "NO_COLOR": "1",
    "GIT_TERMINAL_PROMPT": "0",
}

#: Kept as a separate alias so tests can inject a process without replacing
#: ``subprocess.Popen`` for unrelated Git operations.
_POPEN = subprocess.Popen


class BuildError(RuntimeError):
    """Raised when a build project or test command cannot be resolved."""


def _repository_root(repository_path: str | Path) -> Path:
    repo = Path(repository_path).expanduser().resolve()
    if not repo.is_dir():
        raise BuildError(f"repository 不是有效目录: {repository_path}")
    return repo


def _build_files(repo: Path, names: tuple[str, ...]) -> list[str]:
    found: list[str] = []
    for name in names:
        candidate = repo / name
        if candidate.is_symlink():
            raise BuildError(f"构建文件不能是符号链接: {name}")
        if candidate.is_file():
            found.append(name)
    return found


def _wrapper_command(repo: Path, names: tuple[str, ...]) -> list[str] | None:
    for name in names:
        candidate = repo / name
        if candidate.is_symlink():
            raise BuildError(f"构建 Wrapper 不能是符号链接: {name}")
        if candidate.is_file():
            return [str(candidate)]
    return None


def _maven_command(repo: Path) -> tuple[list[str], bool]:
    wrapper_names = ("mvnw.cmd", "mvnw") if os.name == "nt" else ("mvnw", "mvnw.cmd")
    wrapper = _wrapper_command(repo, wrapper_names)
    if wrapper:
        return [*wrapper, "test"], True
    executable = "mvn.cmd" if os.name == "nt" else "mvn"
    return [executable, "test"], False


def _gradle_command(repo: Path) -> tuple[list[str], bool]:
    wrapper_names = (
        ("gradlew.bat", "gradlew")
        if os.name == "nt"
        else ("gradlew", "gradlew.bat")
    )
    wrapper = _wrapper_command(repo, wrapper_names)
    if wrapper:
        return [*wrapper, "--no-daemon", "test"], True
    executable = "gradle.bat" if os.name == "nt" else "gradle"
    return [executable, "--no-daemon", "test"], False


def detect_build_projects(repository_path: str | Path) -> list[BuildProjectInfo]:
    """Detect Maven/Gradle projects at the repository root."""
    repo = _repository_root(repository_path)
    projects: list[BuildProjectInfo] = []

    maven_files = _build_files(repo, MAVEN_BUILD_FILES)
    if maven_files:
        command, uses_wrapper = _maven_command(repo)
        projects.append(
            BuildProjectInfo(
                tool=BuildTool.MAVEN,
                build_files=maven_files,
                command=command,
                uses_wrapper=uses_wrapper,
            )
        )

    gradle_files = _build_files(repo, GRADLE_BUILD_FILES)
    if gradle_files:
        command, uses_wrapper = _gradle_command(repo)
        projects.append(
            BuildProjectInfo(
                tool=BuildTool.GRADLE,
                build_files=gradle_files,
                command=command,
                uses_wrapper=uses_wrapper,
            )
        )
    return projects


def select_build_project(
    projects: list[BuildProjectInfo],
    requested_tool: str = "auto",
) -> BuildProjectInfo:
    """Select exactly one project, rejecting ambiguous auto-detection."""
    if requested_tool not in {"auto", BuildTool.MAVEN, BuildTool.GRADLE}:
        raise BuildError(f"不支持的构建工具: {requested_tool!r}")
    if not projects:
        raise BuildError("仓库根目录未检测到 Maven 或 Gradle 项目")
    if requested_tool == "auto":
        if len(projects) > 1:
            raise BuildError("检测到多个构建工具，请显式指定 maven 或 gradle")
        return projects[0]
    for project in projects:
        if project.tool.value == requested_tool:
            return project
    raise BuildError(f"仓库不是 {requested_tool} 项目")


def _truncate(text: str, limit: int = MAX_OUTPUT_CHARS) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    omitted = len(text) - limit
    return (
        text[:limit] + f"\n...[输出已截断，省略 {omitted} 个字符]",
        True,
    )


def _terminate_process_tree(proc: subprocess.Popen) -> None:
    """Best-effort termination of the build process and its children."""
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                capture_output=True,
                timeout=10,
                shell=False,
            )
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        logger.warning("Failed to terminate build process tree", exc_info=True)
    try:
        proc.kill()
    except OSError:
        pass


def _decode_partial(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode(locale.getpreferredencoding(False) or "utf-8", errors="replace")
    return value


def _make_result(
    *,
    tool: BuildTool | None,
    command: list[str],
    exit_code: int | None,
    stdout: str,
    stderr: str,
    timed_out: bool,
    error: str | None,
    duration_seconds: float,
) -> TestRunResult:
    stdout, stdout_truncated = _truncate(stdout)
    stderr, stderr_truncated = _truncate(stderr)
    return TestRunResult(
        tool=tool,
        command=command,
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        success=not timed_out and error is None and exit_code == 0,
        timed_out=timed_out,
        truncated=stdout_truncated or stderr_truncated,
        error=error,
        duration_seconds=round(duration_seconds, 3),
    )


def run_workspace_tests(
    repository_path: str | Path,
    tool: str = "auto",
    timeout_seconds: float = DEFAULT_TEST_TIMEOUT_SECONDS,
) -> TestRunResult:
    """Run the fixed ``test`` lifecycle inside ``repository_path``."""
    started = time.monotonic()
    selected: BuildProjectInfo | None = None
    try:
        repo = _repository_root(repository_path)
        selected = select_build_project(detect_build_projects(repo), tool)
        command = selected.command
        popen_kwargs: dict = {
            "cwd": str(repo),
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "stdin": subprocess.DEVNULL,
            "text": True,
            "encoding": locale.getpreferredencoding(False) or "utf-8",
            "errors": "replace",
            "env": _BUILD_ENV,
            "shell": False,
        }
        if os.name == "nt":
            popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            popen_kwargs["start_new_session"] = True

        proc = _POPEN(command, **popen_kwargs)
        timed_out = False
        error: str | None = None
        try:
            stdout, stderr = proc.communicate(timeout=timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            error = f"测试执行超时（>{timeout_seconds:.0f}s），进程已终止"
            _terminate_process_tree(proc)
            try:
                stdout, stderr = proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                stdout, stderr = "", ""
            stdout = _decode_partial(exc.stdout) or stdout
            stderr = _decode_partial(exc.stderr) or stderr

        return _make_result(
            tool=selected.tool,
            command=command,
            exit_code=proc.returncode,
            stdout=stdout or "",
            stderr=stderr or "",
            timed_out=timed_out,
            error=error,
            duration_seconds=time.monotonic() - started,
        )
    except BuildError as exc:
        return _make_result(
            tool=selected.tool if selected else None,
            command=selected.command if selected else [],
            exit_code=None,
            stdout="",
            stderr="",
            timed_out=False,
            error=str(exc),
            duration_seconds=time.monotonic() - started,
        )
    except (FileNotFoundError, PermissionError, OSError) as exc:
        command = selected.command if selected else []
        executable = command[0] if command else "Maven/Gradle"
        return _make_result(
            tool=selected.tool if selected else None,
            command=command,
            exit_code=None,
            stdout="",
            stderr="",
            timed_out=False,
            error=f"无法启动 {executable}: {exc}",
            duration_seconds=time.monotonic() - started,
        )
