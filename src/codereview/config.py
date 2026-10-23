"""Validated repository policy; secrets are supplied by the controller environment."""

import hashlib
import json
import tomllib
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from codereview.models import Record


class Limit(Record):
    dollars: float = Field(gt=0)
    seconds: int = Field(gt=0)


class ModelPolicy(Record):
    coordinator: str = ""
    worker: str = ""
    allowed_models: list[str] = Field(default_factory=list)
    allowed_providers: list[str] = Field(default_factory=list)
    coordinator_monthly_dollars: float = Field(default=100, gt=0, le=100)
    max_output_tokens: int = Field(default=4096, ge=256, le=16384)


class Check(Record):
    name: str = Field(min_length=1)
    command: list[str] = Field(min_length=1)
    image: str
    timeout_seconds: int = Field(default=120, gt=0, le=1200)


class Settings(Record):
    models: ModelPolicy = Field(default_factory=ModelPolicy)
    review: Limit = Field(default_factory=lambda: Limit(dollars=2, seconds=600))
    audit: Limit = Field(default_factory=lambda: Limit(dollars=10, seconds=1800))
    fix: Limit = Field(default_factory=lambda: Limit(dollars=5, seconds=1200))
    concurrency: int = Field(default=3, ge=1, le=8)
    max_tasks: int = Field(default=8, ge=3, le=30)
    max_file_bytes: int = Field(default=150_000, ge=100, le=1_000_000)
    max_snapshot_bytes: int = Field(default=5_000_000, ge=1000, le=50_000_000)
    max_context_chars: int = Field(default=60_000, ge=1000, le=500_000)
    inline_limit: int = Field(default=5, ge=0, le=30)
    exclude: list[str] = Field(
        default_factory=lambda: [
            "*.lock",
            "package-lock.json",
            "uv.lock",
            "dist/**",
            "node_modules/**",
            "vendor/**",
            ".env*",
            "*.pem",
            "*.key",
        ]
    )
    checks: list[Check] = Field(default_factory=list)
    instructions: str = ""

    @model_validator(mode="after")
    def distinct_checks(self) -> "Settings":
        names = [check.name for check in self.checks]
        if len(set(names)) != len(names):
            raise ValueError("Validation check names must be unique for baseline comparison")
        return self

    def limit(self, operation: Literal["review", "audit", "fix"]) -> Limit:
        return getattr(self, operation)

    def digest(self) -> str:
        return hashlib.sha256(json.dumps(self.model_dump(), sort_keys=True).encode()).hexdigest()


def load_settings(root: Path, *, trusted_text: str | None = None) -> Settings:
    if trusted_text is not None:
        return Settings.model_validate(tomllib.loads(trusted_text))
    path = root / ".codereview.toml"
    if path.is_symlink():
        raise ValueError("Review configuration must not be a symlink")
    return Settings.model_validate(tomllib.loads(path.read_text()) if path.exists() else {})
