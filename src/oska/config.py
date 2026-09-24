"""Settings and the typed contents of the JSON configuration files.

Every value is external: paths and the provider selection come from the
environment (prefix `OSKA_`), thresholds and copy come from `runtime.json`,
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

ENV_PREFIX = "OSKA_"
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
    languages: list[str] = Field(default_factory=lambda: ["en"])
    exclude_types: list[str] = Field(default_factory=lambda: ["composite"])


class RouterRules(BaseModel):
    """Tokens the rule layer recognizes without a model."""

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


class GuardrailsConfig(BaseModel):
    """Where the guardrail catalog lives, and whether guardrails run at all."""

    enabled: bool = True
    catalog: Path = Path("configs/guardrails.json")


class TracingConfig(BaseModel):
    """Diagnostic traces, kept apart from the audit record and off by default."""

    enabled: bool = False
    path: Path = Path("state/traces.jsonl")


class TurnConfig(BaseModel):
    """What one turn may spend.

    `max_model_calls` is the budget a turn may not exceed; work that needs more
    belongs at publish time or in a scheduled job.
    """

    max_model_calls: int = 4


class EnrichmentConfig(BaseModel):
    """Routing examples written at publish time, never during a turn."""

    enabled: bool = False
    per_language: int = 3
    cap_per_route: int = 24
    filename: str = "generated-examples.json"


class ImprovementConfig(BaseModel):
    """Bounds a scheduled improvement job may work inside.

    `bounds` names the tunables the job may move and the interval it may move
    them in. Anything absent from it is not tunable.
    """

    learned_root: Path = Path("state/learned")
    report_path: Path = Path("state/improvement.json")
    max_phrases_per_skill: int = 24
    cluster_threshold: float = 0.8
    cluster_min_size: int = 3
    bounds: dict[str, tuple[float, float]] = Field(
        default_factory=lambda: {"min_score": (0.1, 0.5), "tie_margin": (0.02, 0.15)}
    )


DEFAULT_BUSY_TIMEOUT_SECONDS = 10.0
"""How long a connection waits for another to finish before it gives up."""


class StorageConfig(BaseModel):
    """What the files a session and its receipts live in are allowed to cost.

    Two turns of the same process may write at once; `busy_timeout_seconds` is
    how long one waits for the other rather than failing the turn.
    """

    busy_timeout_seconds: float = DEFAULT_BUSY_TIMEOUT_SECONDS
    backend: Literal["sqlite", "postgres"] = "sqlite"
    url_env: str = "OSKA_DATABASE_URL"


class SigningConfig(BaseModel):
    """How a catalog revision is signed and checked.

    `signer` names a registered signer; empty means revisions are not signed.
    `key_env` names the environment variable that holds the key: the key itself
    never appears in configuration. `required` refuses to serve an unsigned or
    badly signed revision.
    """

    signer: str = ""
    key_env: str = "OSKA_REVISION_KEY"
    required: bool = False


class CatalogConfig(BaseModel):
    """Where built catalog revisions are kept, and which one is served.

    `revision` names the revision the runtime serves; empty means the newest
    one the store holds. `tenant` is the catalog a deployment serves when it
    serves more than one.
    """

    mode: Literal["directory", "revision"] = "directory"
    revisions_root: Path = Path("state/revisions")
    revision: str = ""
    tenant: str = "default"
    pin_sessions: bool = True
    signing: SigningConfig = Field(default_factory=SigningConfig)


class ChangeRequestConfig(BaseModel):
    """Where a proposed catalog change is written for a person to review."""

    root: Path = Path("state/changes")


class ResilienceConfig(BaseModel):
    """What a connector is allowed to cost before a capability is called unavailable.

    `failures_before_open` is how many consecutive failures open the breaker,
    `recovery_seconds` how long it stays open before one attempt is let through.
    """

    timeout_seconds: float = 30.0
    failures_before_open: int = 3
    recovery_seconds: float = 30.0
    retries: int = 0
    backoff_seconds: float = 0.5
    retry_statuses: list[int] = Field(default_factory=lambda: [502, 503, 504])
    max_response_bytes: int = 1_000_000


class RetentionConfig(BaseModel):
    """How long the record of a conversation is kept.

    `days` is the window; zero keeps everything. Deletion on request is always
    available regardless of the window.
    """

    days: int = 0


class IdentityConfig(BaseModel):
    """How a caller's identity is read.

    `mode` is `proxy` when an authenticated proxy sets headers the runtime
    trusts, and `token` when the caller presents a signed session token the
    runtime verifies itself. `key_env` names where the verification key lives.
    """

    mode: Literal["proxy", "token"] = "proxy"
    key_env: str = "OSKA_SESSION_TOKEN_KEY"
    required_claims: list[str] = Field(default_factory=lambda: ["sub", "sid", "exp"])
    tenant_claim: str = "tenant"
    leeway_seconds: int = 30


class EvaluationConfig(BaseModel):
    """What a catalog change must reach before it is accepted."""

    min_precision: float = 1.0
    min_recall: float = 1.0
    require_sets: bool = False


class MetricsConfig(BaseModel):
    """Whether the runtime exposes numbers a monitoring system can scrape."""

    enabled: bool = True


class PlatformConfig(BaseModel):
    """How many tenant and domain runtimes one process keeps warm at once."""

    cache_size: int = 16


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
    guardrails: GuardrailsConfig = Field(default_factory=GuardrailsConfig)
    tracing: TracingConfig = Field(default_factory=TracingConfig)
    turn: TurnConfig = Field(default_factory=TurnConfig)
    enrichment: EnrichmentConfig = Field(default_factory=EnrichmentConfig)
    improvement: ImprovementConfig = Field(default_factory=ImprovementConfig)
    catalog: CatalogConfig = Field(default_factory=CatalogConfig)
    changes: ChangeRequestConfig = Field(default_factory=ChangeRequestConfig)
    resilience: ResilienceConfig = Field(default_factory=ResilienceConfig)
    retention: RetentionConfig = Field(default_factory=RetentionConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    identity: IdentityConfig = Field(default_factory=IdentityConfig)
    evaluation: EvaluationConfig = Field(default_factory=EvaluationConfig)
    metrics: MetricsConfig = Field(default_factory=MetricsConfig)
    platform: PlatformConfig = Field(default_factory=PlatformConfig)


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


def guardrail_catalog_path(config: GuardrailsConfig, settings: Settings) -> Path:
    """Where the guardrail catalog is read from.

    A relative path is read from the working directory, and from the directory
    of the runtime configuration when it is not there, so a deployment that
    keeps its configuration together does not have to say where it is twice.
    """
    configured = Path(config.catalog)
    if configured.is_absolute() or configured.exists():
        return configured
    beside = Path(settings.runtime_config).parent / configured.name
    return beside if beside.exists() else configured


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
