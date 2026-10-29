import subprocess

from typer.testing import CliRunner

from codereview import __version__
from codereview.cli import app


def test_version_is_available_without_a_repository_or_credentials(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(app, ["--version"])
    assert result.exit_code == 0, result.output
    assert result.output.strip() == __version__
    assert not list(tmp_path.iterdir())


def test_init_writes_model_choices_without_credentials(tmp_path):
    result = CliRunner().invoke(
        app,
        [
            "init",
            "--root",
            str(tmp_path),
            "--coordinator-model",
            "vendor/strong",
            "--worker-model",
            "vendor/small",
            "--provider",
            "example-provider",
        ],
    )
    assert result.exit_code == 0, result.output
    text = (tmp_path / ".codereview.toml").read_text()
    assert 'coordinator = "vendor/strong"' in text
    assert 'worker = "vendor/small"' in text
    assert 'allowed_providers = ["example-provider"]' in text
    assert "API_KEY=" not in text
    again = CliRunner().invoke(app, ["init", "--root", str(tmp_path)])
    assert again.exit_code != 0


def test_offline_review_reports_scope_without_claiming_a_model_review(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(tmp_path),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "--allow-empty",
            "-qm",
            "initial",
        ],
        check=True,
    )
    (tmp_path / "app.py").write_text("print('hello')\n")
    result = CliRunner().invoke(app, ["review", "--root", str(tmp_path), "--offline"])
    assert result.exit_code == 2, result.output
    assert "partial" in result.output
    assert "Offline" in result.output
    assert (tmp_path / "app.py").read_text() == "print('hello')\n"
