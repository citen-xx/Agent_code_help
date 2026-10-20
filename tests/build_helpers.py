"""Helpers for offline Maven/Gradle execution tests."""


class FakeProcess:
    """Minimal Popen stand-in with programmable output."""

    def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0):
        self.pid = 4242
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr
        self.killed = False

    def communicate(self, timeout=None):
        return self._stdout, self._stderr

    def kill(self):
        self.killed = True
        self.returncode = -1


def patch_popen(
    monkeypatch,
    *,
    stdout: str = "",
    stderr: str = "",
    returncode: int = 0,
    captured: dict | None = None,
) -> dict:
    """Patch Popen and capture the exact command, cwd, and security flags."""
    captured = captured if captured is not None else {}

    def fake_popen(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return FakeProcess(stdout=stdout, stderr=stderr, returncode=returncode)

    monkeypatch.setattr("app.workspace.build._POPEN", fake_popen)
    return captured
