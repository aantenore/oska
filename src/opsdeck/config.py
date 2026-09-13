"""Settings and the typed contents of the JSON configuration files.

Every value is external: paths and the provider selection come from the
environment (prefix `OPSDECK_`), thresholds and copy come from `runtime.json`,
provider access comes from `model.json`. Relative paths resolve against the
current working directory.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ENV_PREFIX = "OPSDECK_"
ProviderName = Literal["openai_compatible", "scripted", "none"]

_PATH_SEPARATORS = (os.pathsep, ",")


class Settings(BaseModel):
    """Where this installation keeps its catalog, configuration and state."""

    model_config = ConfigDict(protected_namespaces=())

    skills_roots: list[Path] = Field(
        default_factory=lambda: [Path("examples/ops/skills"), Path("state/skills")]
    )
    runtime_config: Path = Path("configs/runtime.json")
    model_config_path: Path = Path("configs/model.json")
    connectors_config: Path = Path("examples/ops/configs/connectors.json")
    schemas_dir: Path = Path("schemas")
    scenarios_dir: Path = Path("examples/ops/scenarios")
    state_dir: Path = Path("state")
    provider: ProviderName | None = None

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        """Build settings from the environment, falling back to the defaults."""
        source = os.environ if env is None else env
        values: dict[str, object] = {}
        roots = _read(source, "SKILLS_ROOTS")
        if roots:
            values["skills_roots"] = _split_paths(roots)
        for field, name in (
            ("runtime_config", "RUNTIME_CONFIG"),
            ("model_config_path", "MODEL_CONFIG"),
            ("connectors_config", "CONNECTORS_CONFIG"),
            ("schemas_dir", "SCHEMAS_DIR"),
            ("scenarios_dir", "SCENARIOS_DIR"),
            ("state_dir", "STATE_DIR"),
        ):
            raw = _read(source, name)
            if raw:
                values[field] = Path(raw)
        provider = _read(source, "PROVIDER")
        if provider:
            values["provider"] = provider
        return cls.model_validate(values)


class DiscoveryConfig(BaseModel):
    """How the catalog is ranked against a request."""

    min_score: float = 0.2
    tie_margin: float = 0.05
    limit: int = 5
    exclude_types: list[str] = Field(default_factory=lambda: ["composite"])


class RouterRules(BaseModel):
    """Tokens the rule layer recognises without a model."""

    approve_tokens: list[str] = Field(default_factory=list)
    reject_tokens: list[str] = Field(default_factory=list)
    cancel_tokens: list[str] = Field(default_factory=list)


class FastPathRoutes(BaseModel):
    """Utterances that route without a model."""

    greeting: list[str] = Field(default_factory=list)


class FastPathConfig(BaseModel):
    """Trigger matching applied before the model router."""

    enabled: bool = True
    threshold: float = 0.75
    routes: FastPathRoutes = Field(default_factory=FastPathRoutes)


class RouterConfig(BaseModel):
    """Routing layers."""

    rules: RouterRules = Field(default_factory=RouterRules)
    fast_path: FastPathConfig = Field(default_factory=FastPathConfig)


class LedgerConfig(BaseModel):
    """Where write receipts are stored."""

    path: Path = Path("state/ledger.sqlite")


class AuditConfig(BaseModel):
    """Where audit events are appended."""

    path: Path = Path("state/audit.jsonl")


class SessionsConfig(BaseModel):
    """Where session state is kept between turns."""

    path: Path = Path("state/sessions.sqlite")


class ReplyConfig(BaseModel):
    """Reply defaults.

    `conversation_window` is how many of the most recent messages are handed to
    the model that phrases a reply.
    """

    default_language: str = "en"
    conversation_window: int = 6


class ApiConfig(BaseModel):
    """Where the HTTP application listens when it is served."""

    host: str = "127.0.0.1"
    port: int = 8000


class LearningConfig(BaseModel):
    """Where approved proposals are published."""

    publish_root: Path = Path("state/skills")


class RuntimeConfig(BaseModel):
    """Contents of `runtime.json`."""

    discovery: DiscoveryConfig = Field(default_factory=DiscoveryConfig)
    router: RouterConfig = Field(default_factory=RouterConfig)
    ledger: LedgerConfig = Field(default_factory=LedgerConfig)
    audit: AuditConfig = Field(default_factory=AuditConfig)
    sessions: SessionsConfig = Field(default_factory=SessionsConfig)
    reply: ReplyConfig = Field(default_factory=ReplyConfig)
    learning: LearningConfig = Field(default_factory=LearningConfig)
    api: ApiConfig = Field(default_factory=ApiConfig)


class ModelConfig(BaseModel):
    """Contents of `model.json`, after environment overrides."""

    model_config = ConfigDict(protected_namespaces=())

    provider: ProviderName = "none"
    base_url: str | None = None
    api_key: str | None = None
    chat_model: str = ""
    embedding_model: str = ""
    timeout_seconds: float = 30.0


def state_path(state_dir: Path, configured: Path) -> Path:
    """Where a configured state path lands: under the state directory, by its name."""
    configured = Path(configured)
    if configured.is_absolute():
        return configured
    return Path(state_dir) / configured.name


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    """Read settings from the environment."""
    return Settings.from_env(env)


def load_runtime_config(
    path: Path | None = None, *, settings: Settings | None = None
) -> RuntimeConfig:
    """Read `runtime.json`."""
    target = path or (settings or load_settings()).runtime_config
    return RuntimeConfig.model_validate(_read_json(target))


def load_model_config(
    path: Path | None = None,
    *,
    settings: Settings | None = None,
    env: Mapping[str, str] | None = None,
) -> ModelConfig:
    """Read `model.json` and apply the environment overrides."""
    resolved = settings or load_settings(env)
    target = path or resolved.model_config_path
    values = dict(_read_json(target))
    source = os.environ if env is None else env
    for field, name in (
        ("base_url", "MODEL_BASE_URL"),
        ("api_key", "MODEL_API_KEY"),
        ("chat_model", "MODEL_CHAT"),
        ("embedding_model", "MODEL_EMBEDDING"),
    ):
        raw = _read(source, name)
        if raw:
            values[field] = raw
    if resolved.provider:
        values["provider"] = resolved.provider
    return ModelConfig.model_validate(values)


def _read(source: Mapping[str, str], name: str) -> str:
    return str(source.get(f"{ENV_PREFIX}{name}", "")).strip()


def _split_paths(raw: str) -> list[Path]:
    parts = [raw]
    for separator in _PATH_SEPARATORS:
        parts = [piece for part in parts for piece in part.split(separator)]
    return [Path(part.strip()) for part in parts if part.strip()]


def _read_json(path: Path) -> dict:
    with Path(path).open(encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a JSON object")
    return data
