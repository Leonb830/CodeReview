import json
import subprocess

import httpx
from typer.testing import CliRunner

from codereview import __version__
from codereview.cli import app
from codereview.providers import OpenRouterClient


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


def test_doctor_defaults_to_local_presence_checks_only(tmp_path, monkeypatch):
    def unexpected_client(*args, **kwargs):
        raise AssertionError("Default doctor must not contact OpenRouter")

    monkeypatch.setattr("codereview.providers.OpenRouterClient", unexpected_client)
    monkeypatch.setenv("OPENROUTER_COORDINATOR_KEY", "not-a-real-key")
    result = CliRunner().invoke(app, ["doctor", "--root", str(tmp_path)])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["coordinator_key_configured"] is True
    assert "not verified" in payload["note"]
    assert "online" not in payload
    assert "not-a-real-key" not in result.output
    assert not list(tmp_path.iterdir())


def prepare_online_doctor(tmp_path, monkeypatch, handler):
    (tmp_path / ".codereview.toml").write_text(
        '[models]\ncoordinator = "test/coordinator"\nworker = "test/worker"\n'
        'allowed_models = ["test/coordinator", "test/worker"]\n'
        'allowed_providers = ["test-provider"]\n'
    )
    monkeypatch.setenv("OPENROUTER_COORDINATOR_KEY", "secret-coordinator")
    monkeypatch.setenv("OPENROUTER_WORKER_KEY", "secret-worker")

    def client(settings):
        result = OpenRouterClient(
            settings, http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
        )
        result._owns_http = True
        return result

    def unexpected_snapshot(*args, **kwargs):
        raise AssertionError("Online doctor must not capture source")

    monkeypatch.setattr("codereview.providers.OpenRouterClient", client)
    monkeypatch.setattr("codereview.snapshots.capture_snapshot", unexpected_snapshot)


def test_online_doctor_verifies_metadata_without_capturing_source_or_generating(
    tmp_path, monkeypatch
):
    requests = []

    def respond(request):
        requests.append(request)
        assert request.method == "GET"
        assert not request.content
        if request.url.path.endswith("/key"):
            return httpx.Response(
                200,
                json={
                    "data": {
                        "limit": 100,
                        "limit_remaining": 90,
                        "limit_reset": "monthly",
                        "include_byok_in_limit": True,
                        "is_management_key": False,
                    }
                },
            )
        assert request.url.path.endswith("/models")
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": model,
                        "context_length": 200000,
                        "supported_parameters": ["tools", "structured_outputs"],
                        "pricing": {"prompt": "0.000001", "completion": "0.000002"},
                    }
                    for model in ("test/coordinator", "test/worker")
                ]
            },
        )

    prepare_online_doctor(tmp_path, monkeypatch, respond)
    (tmp_path / "private.ts").write_text("const sourceMustNotBeSent = true;\n")
    result = CliRunner().invoke(app, ["doctor", "--root", str(tmp_path), "--online"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["online"]["status"] == "ok"
    assert payload["online"]["models"] == {
        "coordinator": "test/coordinator",
        "worker": "test/worker",
    }
    assert len(requests) == 3
    key_requests = [item for item in requests if item.url.path.endswith("/key")]
    assert {item.headers["Authorization"] for item in key_requests} == {
        "Bearer secret-coordinator",
        "Bearer secret-worker",
    }
    assert "secret-coordinator" not in result.output
    assert "secret-worker" not in result.output
    assert "sourceMustNotBeSent" not in result.output
    assert not (tmp_path / ".git").exists()


def test_online_doctor_network_failure_is_structured_and_exits_nonzero(tmp_path, monkeypatch):
    def respond(request):
        raise httpx.ProxyError("http://private-user:private-password@private-proxy")

    prepare_online_doctor(tmp_path, monkeypatch, respond)
    result = CliRunner().invoke(app, ["doctor", "--root", str(tmp_path), "--online"])
    assert result.exit_code == 1, result.output
    payload = json.loads(result.output)
    assert payload["online"]["status"] == "failed"
    assert "ProxyError" in payload["online"]["error"]
    assert "private-user" not in result.output
    assert "private-password" not in result.output


def test_online_doctor_setup_failure_does_not_expose_proxy_credentials(tmp_path, monkeypatch):
    def invalid_client(*args, **kwargs):
        raise ValueError("invalid proxy http://private-user:private-password@proxy")

    monkeypatch.setattr("codereview.providers.OpenRouterClient", invalid_client)
    result = CliRunner().invoke(app, ["doctor", "--root", str(tmp_path), "--online"])
    assert result.exit_code == 1, result.output
    payload = json.loads(result.output)
    assert payload["online"]["status"] == "failed"
    assert "proxy and certificate" in payload["online"]["error"]
    assert "private-password" not in result.output
