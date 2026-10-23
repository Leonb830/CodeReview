import stat
import time
from pathlib import Path

from codereview.config import Check
from codereview.execution import validate


def executable(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "docker-test"
    path.write_text("#!/usr/bin/env python3\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def test_missing_docker_is_an_explicit_validation_gap(tmp_path):
    (tmp_path / "app.py").write_text("print('ok')\n")
    checks = [Check(name="tests", command=["python", "-m", "pytest"], image="python:3.12")]
    result = validate(tmp_path, checks, docker="/definitely/missing/docker")
    assert result[0].status == "unavailable"
    assert "Docker" in result[0].output


def test_check_sees_disposable_source_without_controller_credentials(tmp_path, monkeypatch):
    source = tmp_path / "repo"
    source.mkdir()
    (source / "app.py").write_text("original\n")
    (source / ".env").write_text("SECRET=not-for-tests")
    (source / ".npmrc").write_text("//registry.npmjs.org/:_authToken=private")
    (source / "credentials.json").write_text('{"private_key": "secret"}')
    (source / ".git").mkdir()
    (source / ".git" / "config").write_text("credential=private")
    monkeypatch.setenv("OPENROUTER_WORKER_KEY", "must-not-leak")
    binary = executable(
        tmp_path,
        """import json, os, pathlib, sys
if sys.argv[1] == 'run':
    source = pathlib.Path(sys.argv[sys.argv.index('--volume') + 1].split(':')[0])
    print(json.dumps({'secret': os.environ.get('OPENROUTER_WORKER_KEY'),
        'env_file': (source / '.env').exists(), 'git': (source / '.git').exists(),
        'npmrc': (source / '.npmrc').exists(), 'credentials': (source / 'credentials.json').exists(),
        'network': sys.argv[sys.argv.index('--network') + 1]}))
    (source / 'app.py').write_text('changed by check')
""",
    )
    result = validate(
        source,
        [Check(name="tests", command=["python", "-m", "pytest"], image="python:3.12")],
        docker=str(binary),
    )
    assert result[0].status == "passed"
    assert '"secret": null' in result[0].output
    assert '"env_file": false' in result[0].output
    assert '"git": false' in result[0].output
    assert '"npmrc": false' in result[0].output
    assert '"credentials": false' in result[0].output
    assert '"network": "none"' in result[0].output
    assert (source / "app.py").read_text() == "original\n"


def test_deadline_does_not_start_a_check(tmp_path):
    binary = executable(tmp_path, "raise AssertionError('must not start')\n")
    result = validate(
        tmp_path,
        [Check(name="tests", command=["test"], image="python:3.12")],
        deadline=time.monotonic() - 1,
        docker=str(binary),
    )
    assert result[0].status == "timed_out"


def test_copying_source_counts_towards_operation_deadline(tmp_path, monkeypatch):
    from codereview import execution

    binary = executable(tmp_path, "raise AssertionError('must not start')\n")
    clock = [10.0]
    monkeypatch.setattr(execution.time, "monotonic", lambda: clock[0])

    def copy_crossing_deadline(source, target):
        target.mkdir()
        clock[0] = 12.0

    monkeypatch.setattr(execution, "_source_copy", copy_crossing_deadline)
    result = validate(
        tmp_path,
        [Check(name="tests", command=["test"], image="python:3.12")],
        deadline=11.0,
        docker=str(binary),
    )
    assert result[0].status == "timed_out"
    assert result[0].exit_code is None


def test_check_output_is_bounded(tmp_path):
    binary = executable(tmp_path, "import sys\nif sys.argv[1] == 'run': print('x' * 100000)\n")
    result = validate(
        tmp_path, [Check(name="tests", command=["test"], image="python:3.12")], docker=str(binary)
    )
    assert result[0].status == "passed"
    assert len(result[0].output) < 65_000
    assert result[0].output.endswith("[output truncated]")


def test_container_check_timeout_is_not_a_validation_pass(tmp_path):
    binary = executable(tmp_path, "import sys, time\nif sys.argv[1] == 'run': time.sleep(5)\n")
    result = validate(
        tmp_path,
        [Check(name="tests", command=["test"], image="python:3.12")],
        deadline=time.monotonic() + 0.1,
        docker=str(binary),
    )
    assert result[0].status == "timed_out"
