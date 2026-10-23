"""Run trusted check commands against disposable, credential-free source copies."""

import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Literal
from uuid import uuid4

from codereview.config import Check
from codereview.models import CheckResult
from codereview.snapshots import _safety_reason

_IMAGE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/:@-]*\Z")
_LOG_LIMIT = 64_000
_PRIVATE_DIRS = {".git", ".codereview", ".ssh", ".aws", ".azure", ".config"}


def _source_copy(source: Path, target: Path) -> None:
    def excluded(directory: str, names: list[str]) -> set[str]:
        return {
            name
            for name in names
            if name in _PRIVATE_DIRS
            or name.startswith(".env")
            or name.endswith((".pem", ".key"))
            or _safety_reason(name) == "potential secret file"
            or (Path(directory) / name).is_symlink()
        }

    shutil.copytree(source, target, ignore=excluded)


def _execute(command: list[str], seconds: float, env: dict[str, str]) -> tuple[int, str, bool]:
    """Drain output continuously while retaining a bounded diagnostic prefix."""
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
    collected = bytearray()
    truncated = False

    def drain() -> None:
        nonlocal truncated
        assert process.stdout is not None
        while chunk := process.stdout.read(8192):
            available = max(0, _LOG_LIMIT - len(collected))
            collected.extend(chunk[:available])
            truncated = truncated or len(chunk) > available

    reader = threading.Thread(target=drain, daemon=True)
    reader.start()
    timed_out = False
    try:
        process.wait(timeout=max(0.01, seconds))
    except subprocess.TimeoutExpired:
        timed_out = True
        process.kill()
        process.wait()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        reader.join(timeout=2)
        if process.stdout is not None:
            process.stdout.close()
    output = collected.decode("utf-8", errors="replace")
    if truncated:
        output += "\n[output truncated]"
    return process.returncode, output, timed_out


def validate(
    root: Path,
    checks: list[Check],
    deadline: float | None = None,
    *,
    docker: str = "docker",
) -> list[CheckResult]:
    """Run each check in Docker; absence or timeout is never treated as a pass.

    ``deadline`` is a monotonic absolute deadline for the whole fix operation.
    Images must already contain their check dependencies: validation has no network.
    Each check receives a fresh source copy, so test side effects cannot edit a fix.
    """
    if not checks:
        return [
            CheckResult(name="validation", status="unavailable", output="No checks configured.")
        ]
    binary = shutil.which(docker)
    if binary is None:
        return [
            CheckResult(
                name=check.name,
                status="unavailable",
                output="Docker is unavailable; no host fallback was run.",
            )
            for check in checks
        ]
    results: list[CheckResult] = []
    for check in checks:
        remaining = float(check.timeout_seconds)
        if deadline is not None:
            remaining = min(remaining, deadline - time.monotonic())
        if remaining <= 0:
            results.append(
                CheckResult(
                    name=check.name, status="timed_out", output="Operation deadline reached."
                )
            )
            continue
        if not _IMAGE.fullmatch(check.image) or any("\0" in part for part in check.command):
            results.append(
                CheckResult(
                    name=check.name,
                    status="unavailable",
                    output="Invalid validation image or command.",
                )
            )
            continue
        container = f"codereview-check-{uuid4().hex}"
        with tempfile.TemporaryDirectory(prefix="codereview-check-") as directory:
            temporary = Path(directory)
            workspace = temporary / "source"
            _source_copy(root, workspace)
            if deadline is not None:
                remaining = min(remaining, deadline - time.monotonic())
            if remaining <= 0:
                results.append(
                    CheckResult(
                        name=check.name,
                        status="timed_out",
                        output="Operation deadline reached while preparing validation source.",
                    )
                )
                continue
            # The Docker client gets no model keys, GitHub token, or user Docker config.
            env = {
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "HOME": directory,
                "DOCKER_CONFIG": str(temporary / "docker"),
            }
            command = [
                binary,
                "run",
                "--rm",
                "--name",
                container,
                "--network",
                "none",
                "--read-only",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
                "--pids-limit",
                "128",
                "--memory",
                "1g",
                "--cpus",
                "1",
                "--user",
                f"{os.getuid()}:{os.getgid()}",
                "--tmpfs",
                "/tmp:rw,nosuid,nodev,size=256m",
                "--volume",
                f"{workspace}:/workspace:rw",
                "--workdir",
                "/workspace",
                "--entrypoint",
                check.command[0],
                check.image,
                *check.command[1:],
            ]
            try:
                code, output, timed_out = _execute(command, remaining, env)
                status: Literal["timed_out", "passed", "failed", "unavailable"] = (
                    "timed_out" if timed_out else "passed" if code == 0 else "failed"
                )
                if code in {125, 126, 127} and not timed_out:
                    status = "unavailable"
                results.append(
                    CheckResult(name=check.name, status=status, exit_code=code, output=output)
                )
            except OSError as error:
                results.append(
                    CheckResult(name=check.name, status="unavailable", output=str(error))
                )
            finally:
                # Killing the Docker CLI does not itself stop its daemon-owned container.
                try:
                    subprocess.run(
                        [binary, "rm", "--force", container],
                        env=env,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=10,
                        check=False,
                    )
                except (OSError, subprocess.TimeoutExpired):
                    pass
    return results
