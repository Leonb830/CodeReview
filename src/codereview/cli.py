"""Local and GitHub entry points over the same review and repair engine."""

import asyncio
import json
import os
import shutil
import subprocess
from collections.abc import Coroutine
from pathlib import Path
from typing import Any, Literal, TypeVar

import typer

from codereview.config import load_settings
from codereview.models import ReviewResult
from codereview.reporting import render_fix, render_review
from codereview.runs import RunStore

app = typer.Typer(no_args_is_help=True, help="Review code and create requested isolated fixes.")
T = TypeVar("T")


def run_async(coroutine: Coroutine[Any, Any, T]) -> T:
    try:
        return asyncio.run(coroutine)
    except (ValueError, OSError, RuntimeError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from None


def state_directory(root: Path, explicit: Path | None) -> Path:
    if explicit is not None:
        return explicit.resolve()
    result = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--git-common-dir"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise ValueError("Select a Git repository with --root")
    directory = Path(result.stdout.strip())
    return (directory if directory.is_absolute() else root / directory).resolve() / "codereview"


def write_output(text: str, output: Path | None) -> None:
    if output is None:
        typer.echo(text)
    else:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text)
        typer.echo(f"Report saved to {output}")


@app.command()
def init(
    root: Path = typer.Option(Path("."), help="Repository directory"),
    coordinator_model: str = typer.Option(
        "", help="OpenRouter model ID for planning/consolidation"
    ),
    worker_model: str = typer.Option(
        "", help="OpenRouter model ID for inspection/verification/fixes"
    ),
    provider: list[str] | None = typer.Option(
        None, help="Allowed OpenRouter provider slug; repeat for multiple providers"
    ),
) -> None:
    """Create repository policy. Keep credentials in environment variables."""
    path = root.resolve() / ".codereview.toml"
    models = [item for item in (coordinator_model, worker_model) if item]
    text = f"""# Model IDs must support tools and structured outputs on OpenRouter.
# Secrets: OPENROUTER_COORDINATOR_KEY and OPENROUTER_WORKER_KEY (different keys).
concurrency = 3
inline_limit = 5

[models]
coordinator = {json.dumps(coordinator_model)}
worker = {json.dumps(worker_model)}
allowed_models = {json.dumps(list(dict.fromkeys(models)))}
allowed_providers = {json.dumps(provider or [])} # Constrain upstream routing; at least one is required.
coordinator_monthly_dollars = 100

[review]
dollars = 2
seconds = 600

[audit]
dollars = 10
seconds = 1800

[fix]
dollars = 5
seconds = 1200

# Validation uses trusted images with dependencies already installed, offline.
# Add checks for your repository; missing checks leave fixes as drafts.
# [[checks]]
# name = "python-tests"
# image = "your-registry/your-python-test-image@sha256:..."
# command = ["python", "-m", "pytest", "-q"]
# timeout_seconds = 120
"""
    try:
        with path.open("x") as handle:
            handle.write(text)
    except FileExistsError:
        raise typer.BadParameter(f"{path} already exists; edit it explicitly") from None
    typer.echo(
        f"Created {path}. Configure model IDs, keys, provider allowlists and validation checks."
    )


@app.command()
def doctor(root: Path = typer.Option(Path("."))) -> None:
    """Report local setup without sending code or making paid model calls."""
    settings = load_settings(root.resolve())
    checks = {
        "git": bool(shutil.which("git")),
        "docker_cli": bool(shutil.which("docker")),
        "coordinator_model": settings.models.coordinator or "not configured",
        "worker_model": settings.models.worker or "not configured",
        "allowed_providers": settings.models.allowed_providers,
        "coordinator_key_configured": bool(os.environ.get("OPENROUTER_COORDINATOR_KEY")),
        "worker_key_configured": bool(os.environ.get("OPENROUTER_WORKER_KEY")),
        "validation_checks": [check.name for check in settings.checks],
        "note": "Online runs verify model capabilities/prices and the shared coordinator key's monthly limit before model calls.",
    }
    typer.echo(json.dumps(checks, indent=2))


async def perform_review(
    root: Path,
    mode: Literal["working", "staged", "branch", "audit"],
    base: str | None,
    state_dir: Path | None,
    offline: bool,
) -> ReviewResult:
    from codereview.snapshots import capture_snapshot

    root = root.resolve()
    settings = load_settings(root)
    operation: Literal["review", "audit"] = "audit" if mode == "audit" else "review"
    from codereview.budgets import Budget

    limit = settings.limit(operation)
    budget = Budget(limit.dollars, limit.seconds, settings.models.coordinator_monthly_dollars)
    snapshot = capture_snapshot(root, mode=mode, base=base, settings=settings)
    if offline:
        result = ReviewResult(
            snapshot=snapshot,
            operation=operation,
            status="partial",
            summary=f"Captured {len(snapshot.files)} files and {len(snapshot.changed_paths)} changed paths.",
            gaps=[
                "Offline scope inspection only; no model review or verification was run.",
                *snapshot.excluded,
            ],
            config_digest=settings.digest(),
        )
    else:
        from codereview.engine import review as run_review
        from codereview.providers import OpenRouterClient

        async with OpenRouterClient(settings, operation=operation, budget=budget) as client:
            result = await run_review(snapshot, settings, client, operation=operation)
    RunStore(state_directory(root, state_dir)).save_review(result)
    return result


@app.command()
def review(
    root: Path = typer.Option(Path(".")),
    staged: bool = typer.Option(False, help="Review only staged changes"),
    base: str | None = typer.Option(None, help="Compare the current branch against a base ref"),
    offline: bool = typer.Option(False, help="Inspect scope without making model calls"),
    state_dir: Path | None = typer.Option(None),
    output: Path | None = typer.Option(None),
    json_output: bool = typer.Option(
        False, "--json", help="Emit the complete structured run record"
    ),
) -> None:
    """Review working changes, staged changes, or a branch comparison."""
    if staged and base:
        raise typer.BadParameter("Choose --staged or --base, not both")
    mode: Literal["working", "staged", "branch"] = (
        "staged" if staged else "branch" if base else "working"
    )
    result = run_async(perform_review(root, mode, base, state_dir, offline))
    write_output(result.model_dump_json(indent=2) if json_output else render_review(result), output)
    raise typer.Exit(0 if result.status == "complete" else 2)


@app.command()
def audit(
    root: Path = typer.Option(Path(".")),
    offline: bool = typer.Option(False),
    state_dir: Path | None = typer.Option(None),
    output: Path | None = typer.Option(None),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Audit the repository with the separate audit budget."""
    result = run_async(perform_review(root, "audit", None, state_dir, offline))
    write_output(result.model_dump_json(indent=2) if json_output else render_review(result), output)
    raise typer.Exit(0 if result.status == "complete" else 2)


@app.command()
def fix(
    run_id: str = typer.Argument(..., help="Saved review run ID"),
    findings: list[str] | None = typer.Argument(None, help="Finding IDs to fix"),
    all_findings: bool = typer.Option(
        False, "--all", help="Explicitly fix all compatible findings"
    ),
    root: Path = typer.Option(Path(".")),
    state_dir: Path | None = typer.Option(None),
    destination: Path | None = typer.Option(None),
    output: Path | None = typer.Option(None),
) -> None:
    """Create selected repairs in isolation; never alter the original checkout."""
    if not findings and not all_findings:
        raise typer.BadParameter("Select finding IDs or explicitly use --all")
    if findings and all_findings:
        raise typer.BadParameter("Choose finding IDs or --all, not both")

    async def execute() -> Any:
        from codereview.fixes import fix as create_fix
        from codereview.providers import OpenRouterClient

        store = RunStore(state_directory(root.resolve(), state_dir))
        saved = store.load_review(run_id)
        if Path(saved.snapshot.root).resolve() != root.resolve():
            raise ValueError(
                "Saved review belongs to another repository; select its original --root"
            )
        settings = load_settings(root.resolve())
        async with OpenRouterClient(settings, operation="fix") as client:
            result = await create_fix(
                saved,
                findings or [],
                settings,
                client,
                destination=destination,
                all_findings=all_findings,
            )
        store.save_fix(result)
        return result

    result = run_async(execute())
    write_output(render_fix(result), output)
    if result.worktree:
        typer.echo(f"Isolated workspace: {result.worktree}")
    raise typer.Exit(0 if result.status == "validated" else 2)


@app.command()
def runs(root: Path = typer.Option(Path(".")), state_dir: Path | None = typer.Option(None)) -> None:
    """List saved local review IDs."""
    typer.echo(
        json.dumps(RunStore(state_directory(root.resolve(), state_dir)).list_reviews(), indent=2)
    )


@app.command()
def show(
    run_id: str, root: Path = typer.Option(Path(".")), state_dir: Path | None = typer.Option(None)
) -> None:
    """Render a stored review without another model call."""
    result = RunStore(state_directory(root.resolve(), state_dir)).load_review(run_id)
    typer.echo(render_review(result))


@app.command("github-event")
def github_event(
    repo_root: Path = typer.Option(...),
    engine_root: Path = typer.Option(...),
    output: Path = typer.Option(Path(".review-output")),
) -> None:
    """Process a trusted GitHub Actions event; used by the supplied workflows."""
    from codereview.github import run_event

    run_async(run_event(repo_root.resolve(), engine_root.resolve(), output.resolve()))


if __name__ == "__main__":
    app()
