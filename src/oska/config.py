"""Settings and the typed contents of the JSON configuration files.

Every value is external: paths and the provider selection come from the
environment (prefix `OSKA_`), thresholds and copy come from `runtime.json`,
provider access comes from `model.json`. Relative paths resolve against the
current working directory.

The runtime configuration, the model configuration, the guardrail catalog, the
phrase catalog and the schema directory ship with the package, so an
installation works in a directory that holds none of them. Each is resolved in
this order:

1. An explicit environment variable (`OSKA_RUNTIME_CONFIG`,
   `OSKA_MODEL_CONFIG`, `OSKA_GUARDRAILS_CATALOG`, `OSKA_SCHEMAS_DIR`), or a
   value given to `Settings` directly, wins and is used as given.
2. Otherwise the configured path, when it exists relative to the working
   directory: `configs/runtime.json`, `configs/model.json`, `schemas/`, and
   the catalogs `runtime.json` names under `guardrails.catalog` and
   `reply.phrases` (each also looked for beside the runtime configuration).
3. Otherwise the packaged default under `oska/defaults/`.

`configuration_sources` names which of these is in force for each.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

ENV_PREFIX = "OSKA_"
ProviderName = Literal["openai_compatible", "anthropic", "profiles", "scripted", "none"]
AdapterName = Literal["openai_compatible", "anthropic"]

_PATH_SEPARATORS = (os.pathsep, ",")

PACKAGED_DEFAULTS = Path(__file__).resolve().parent / "defaults"
"""Where the defaults shipped with the package live."""
PACKAGED_CONFIGS = PACKAGED_DEFAULTS / "configs"
PACKAGED_SCHEMAS = PACKAGED_DEFAULTS / "schemas"
PACKAGED_SOURCE = "packaged default"
"""How `configuration_sources` names a file read from the package itself."""

RUNTIME_CONFIG_PATH = Path("configs/runtime.json")
MODEL_CONFIG_PATH = Path("configs/model.json")
GUARDRAILS_CATALOG_PATH = Path("configs/guardrails.json")
PHRASES_PATH = Path("configs/phrases.json")
SCHEMAS_PATH = Path("schemas")


def _local_or_packaged(configured: Path, packaged: Path) -> Path:
    """The configured path when it exists in the working directory, else the packaged one."""
    return configured if configured.exists() else packaged


def _default_runtime_config() -> Path:
    return _local_or_packaged(RUNTIME_CONFIG_PATH, PACKAGED_CONFIGS / RUNTIME_CONFIG_PATH.name)


def _default_model_config() -> Path:
    return _local_or_packaged(MODEL_CONFIG_PATH, PACKAGED_CONFIGS / MODEL_CONFIG_PATH.name)


def _default_schemas_dir() -> Path:
    return _local_or_packaged(SCHEMAS_PATH, PACKAGED_SCHEMAS)


class Settings(BaseModel):
    """Where this installation keeps its catalog, configuration and state."""

    model_config = ConfigDict(protected_namespaces=())

    skills_roots: list[Path] = Field(
        default_factory=lambda: [Path("examples/ops/skills"), Path("state/skills")]
    )
    runtime_config: Path = Field(default_factory=_default_runtime_config)
    model_config_path: Path = Field(default_factory=_default_model_config)
    guardrails_catalog: Path | None = None
    connectors_config: Path = Path("examples/ops/configs/connectors.json")
    schemas_dir: Path = Field(default_factory=_default_schemas_dir)
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
            ("guardrails_catalog", "GUARDRAILS_CATALOG"),
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
    the model that phrases a reply. It is at least one: the window is taken as
    the last N messages, and zero would read as no limit at all.

    `phrases` names the phrase catalog: the sentences the runtime says in its
    own words, keyed by language, laid over the catalog shipped with the package.
    """

    default_language: str = "en"
    conversation_window: int = Field(default=6, ge=1)
    phrases: Path = PHRASES_PATH


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
    catalog: Path = GUARDRAILS_CATALOG_PATH


class TracingConfig(BaseModel):
    """Diagnostic traces, kept apart from the audit record and off by default."""

    enabled: bool = False
    path: Path = Path("state/traces.jsonl")


class TurnConfig(BaseModel):
    """What one turn may spend.

    `max_model_calls` is the budget a turn may not exceed; work that needs more
    belongs at publish time or in a scheduled job. Zero is a turn that never
    calls the model and answers from the catalog and the rules alone.
    """

    max_model_calls: int = Field(default=4, ge=0)


class EnrichmentConfig(BaseModel):
    """Routing examples written at publish time, never during a turn."""

    enabled: bool = False
    per_language: int = Field(default=3, ge=1)
    cap_per_route: int = Field(default=24, ge=1)
    filename: str = "generated-examples.json"


class ImprovementConfig(BaseModel):
    """Bounds a scheduled improvement job may work inside.

    `bounds` names the tunables the job may move and the interval it may move
    them in. Anything absent from it is not tunable.
    """

    learned_root: Path = Path("state/learned")
    report_path: Path = Path("state/improvement.json")
    max_phrases_per_skill: int = Field(default=24, ge=1)
    cluster_threshold: float = Field(default=0.8, gt=0, le=1)
    cluster_min_size: int = Field(default=3, ge=1)
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

    busy_timeout_seconds: float = Field(default=DEFAULT_BUSY_TIMEOUT_SECONDS, gt=0)
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

    Every cost has a floor, so a value that would mean nothing, or would read
    as no limit, is refused where the configuration is read, a tenant fragment
    included: a timeout and a response size are above zero, the breaker opens
    after at least one failure, and retries, backoff and recovery may be zero
    and never negative.
    """

    timeout_seconds: float = Field(default=30.0, gt=0)
    failures_before_open: int = Field(default=3, ge=1)
    recovery_seconds: float = Field(default=30.0, ge=0)
    retries: int = Field(default=0, ge=0)
    backoff_seconds: float = Field(default=0.5, ge=0)
    retry_statuses: list[int] = Field(default_factory=lambda: [502, 503, 504])
    max_response_bytes: int = Field(default=1_000_000, ge=1)


class RetentionConfig(BaseModel):
    """How long the record of a conversation is kept.

    `days` is the window; zero keeps everything. Deletion on request is always
    available regardless of the window.
    """

    days: int = Field(default=0, ge=0)


class IdentityConfig(BaseModel):
    """How a caller's identity is read.

    `mode` is `proxy` when an authenticated proxy sets headers the runtime
    trusts, and `token` when the caller presents a signed session token the
    runtime verifies itself. `key_env` names where the verification key lives.

    `required` refuses every call that carries no identity, except the health
    and metrics reads. It is off by default, so a single-user deployment keeps
    answering without one, and on by default in `token` mode: a deployment that
    verifies tokens and still answers a call without one has verified nothing.
    Saying `required: false` explicitly is the only way to turn it off there.

    `tenant_header` names a header the proxy sets to say which tenant a call
    belongs to. It is read in `proxy` mode only, and only when it is named: a
    header the runtime reads without being told to is one any client can set.
    Once it is named it is the only place the tenant is read from: a `tenant`
    in the attributes header is not, because the proxy is known to own one
    header and not every key of the other.

    `reviewer_role` is the role a principal carries in its `roles` attribute
    (the `roles` claim of a token, or the `roles` entry of the proxy's
    attributes header) to read proposals and change requests and to move a
    proposal. A call with an identity that lacks it is refused those routes.

    `single_user` says that whoever reaches this runtime without an identity is
    its one user, and may do everything that user may: read and move proposals,
    read change requests, and name who moved a proposal in the call itself,
    recorded as stated. It is off by default. Off, a call without an identity,
    where none is required, may still converse, but the reviewer's routes need
    a principal that carries the reviewer role: no identity is never worth more
    than an identity that lacks the role. A deployment in `token` mode verifies
    who each caller is, so it has more than one user, and saying `single_user`
    there is refused.

    `proxy_secret_env` names a variable holding a secret the proxy sends in the
    `proxy_secret_header` header of every call it forwards. When it is named, a
    call carrying an identity or a tenant header without that secret is refused,
    so a caller that reaches the port around the proxy cannot claim to be
    anyone. It applies to `proxy` mode only.
    """

    mode: Literal["proxy", "token"] = "proxy"
    required: bool = False
    key_env: str = "OSKA_SESSION_TOKEN_KEY"
    required_claims: list[str] = Field(default_factory=lambda: ["sub", "sid", "exp"])
    tenant_claim: str = "tenant"
    tenant_header: str = ""
    leeway_seconds: int = 30
    reviewer_role: str = "reviewer"
    single_user: bool = False
    proxy_secret_env: str = ""
    proxy_secret_header: str = "X-Proxy-Secret"

    @model_validator(mode="after")
    def _proxy_secret_is_for_the_proxy(self) -> IdentityConfig:
        """A secret the proxy sends means nothing where tokens are verified instead."""
        if self.proxy_secret_env.strip() and self.mode != "proxy":
            raise ValueError("identity.proxy_secret_env applies to proxy mode only")
        return self

    @model_validator(mode="after")
    def _token_mode_requires_identity(self) -> IdentityConfig:
        """In `token` mode an identity is required unless the configuration says otherwise."""
        if self.mode == "token" and "required" not in self.model_fields_set:
            self.required = True
        return self

    @model_validator(mode="after")
    def _single_user_verifies_nobody(self) -> IdentityConfig:
        """A deployment that verifies tokens is not the deployment of one user."""
        if self.single_user and self.mode == "token":
            raise ValueError(
                "identity.single_user is for a deployment whose one user carries no "
                "identity, and token mode verifies each caller's; set one or the other"
            )
        return self


class EvaluationConfig(BaseModel):
    """What a catalog change must reach before it is accepted."""

    min_precision: float = 1.0
    min_recall: float = 1.0
    require_sets: bool = False


class MetricsConfig(BaseModel):
    """Whether the runtime exposes numbers a monitoring system can scrape.

    `cache_seconds` is how long a computed report is served again before the
    audit trail is read anew; zero reads it on every scrape, and it is never
    negative.

    The report names every tenant a platform serves and what each one does, so
    where an identity is required it is a reviewer's read like the proposals
    are. `public` opens it to any caller anyway, for a deployment whose
    scraper reaches the runtime over a private network and presents no
    identity; it is off by default, and turning it on is a statement about
    that network, not about the runtime.
    """

    enabled: bool = True
    cache_seconds: float = Field(default=15.0, ge=0)
    public: bool = False


class PlatformConfig(BaseModel):
    """How many tenant and domain runtimes one process keeps warm at once; at least one."""

    cache_size: int = Field(default=16, ge=1)


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


class ModelProfile(BaseModel):
    """One model endpoint `model.json` names, and the protocol it speaks.

    The key is never written here: `api_key_env` names the environment
    variable that holds it, and a profile whose variable is unset or empty is
    skipped when the provider is built. A profile that names no variable is
    called without a key. `max_tokens` bounds an answer; the chat-completions
    adapter sends it only when it is set, the messages adapter always sends
    one and uses its own default when it is not. `headers` are sent as written
    with every call of the profile.
    """

    model_config = ConfigDict(protected_namespaces=(), extra="forbid")

    adapter: AdapterName
    base_url: str = Field(min_length=1)
    api_key_env: str = ""
    chat_model: str = ""
    embedding_model: str = ""
    timeout_seconds: float = Field(default=30.0, gt=0)
    max_tokens: int | None = Field(default=None, gt=0)
    headers: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _no_key_in_the_file(cls, data: object) -> object:
        if isinstance(data, Mapping) and "api_key" in data:
            raise ValueError(
                "a profile takes its key from the environment variable api_key_env names, "
                "never from the file"
            )
        return data


class ModelRoles(BaseModel):
    """Which profiles answer which kind of call, in the order they are tried."""

    model_config = ConfigDict(extra="forbid")

    chat: list[str] = Field(default_factory=list)
    embeddings: list[str] = Field(default_factory=list)


class ModelConfig(BaseModel):
    """Contents of `model.json`, after environment overrides.

    The flat keys describe one endpoint, which `openai_compatible` or
    `anthropic` calls. `provider: "profiles"` reads `profiles` and `roles`
    instead: completions go to the chat profiles in order and move to the next
    only on a failure another profile might not have, and embeddings go to the
    embeddings profiles the same way. `api_key` in the file is kept for the
    flat shape and is deprecated; `OSKA_MODEL_API_KEY` is where the key goes.
    """

    model_config = ConfigDict(protected_namespaces=())

    provider: ProviderName = "none"
    base_url: str | None = None
    api_key: str | None = None
    chat_model: str = ""
    embedding_model: str = ""
    timeout_seconds: float = 30.0
    profiles: dict[str, ModelProfile] = Field(default_factory=dict)
    roles: ModelRoles | None = None

    @model_validator(mode="after")
    def _roles_name_declared_profiles(self) -> ModelConfig:
        if self.provider == "profiles" and (self.roles is None or not self.roles.chat):
            raise ValueError("provider profiles needs roles.chat to name at least one profile")
        if self.roles is None:
            return self
        for role, names in (("chat", self.roles.chat), ("embeddings", self.roles.embeddings)):
            unknown = [name for name in names if name not in self.profiles]
            if unknown:
                raise ValueError(
                    f"roles.{role} names profiles that are not declared: {', '.join(unknown)}"
                )
        for name in self.roles.chat:
            if not self.profiles[name].chat_model:
                raise ValueError(f"profile {name} is in roles.chat but declares no chat_model")
        for name in self.roles.embeddings:
            if not self.profiles[name].embedding_model:
                raise ValueError(
                    f"profile {name} is in roles.embeddings but declares no embedding_model"
                )
        return self


def state_path(state_dir: Path, configured: Path) -> Path:
    """Where a configured state path lands: under the state directory, by its name."""
    configured = Path(configured)
    if configured.is_absolute():
        return configured
    return Path(state_dir) / configured.name


def guardrail_catalog_path(config: GuardrailsConfig, settings: Settings) -> Path:
    """Where the guardrail catalog is read from.

    `OSKA_GUARDRAILS_CATALOG` wins when it is set. Otherwise a relative path
    is read from the working directory, and from the directory of the runtime
    configuration when it is not there, so a deployment that keeps its
    configuration together does not have to say where it is twice. When the
    runtime configuration names the default catalog and neither holds it, the
    packaged catalog is read. A catalog named anywhere else is used as named,
    present or not.
    """
    if settings.guardrails_catalog is not None:
        return Path(settings.guardrails_catalog)
    configured = Path(config.catalog)
    if configured.is_absolute() or configured.exists():
        return configured
    beside = Path(settings.runtime_config).parent / configured.name
    if beside.exists():
        return beside
    if configured == GUARDRAILS_CATALOG_PATH:
        return PACKAGED_CONFIGS / GUARDRAILS_CATALOG_PATH.name
    return configured


def phrases_path(config: ReplyConfig, settings: Settings) -> Path:
    """Where the phrase catalog is read from.

    A relative path is read from the working directory, and from the directory
    of the runtime configuration when it is not there. When the runtime
    configuration names the default catalog and neither holds it, the packaged
    catalog is read. A catalog named anywhere else is used as named, present or
    not. Whatever is read is laid over the packaged catalog, phrase by phrase.
    """
    configured = Path(config.phrases)
    if configured.is_absolute() or configured.exists():
        return configured
    beside = Path(settings.runtime_config).parent / configured.name
    if beside.exists():
        return beside
    if configured == PHRASES_PATH:
        return PACKAGED_CONFIGS / PHRASES_PATH.name
    return configured


def configuration_sources(settings: Settings, runtime: RuntimeConfig) -> dict[str, str]:
    """Which file is in force for each shipped default: a path, or the packaged default."""
    return {
        "runtime_config": _source(settings.runtime_config),
        "model_config": _source(settings.model_config_path),
        "guardrails_catalog": _source(guardrail_catalog_path(runtime.guardrails, settings)),
        "phrases": _source(phrases_path(runtime.reply, settings)),
        "schemas_dir": _source(settings.schemas_dir),
    }


def _source(path: Path) -> str:
    resolved = Path(path).resolve()
    if resolved.is_relative_to(PACKAGED_DEFAULTS):
        return PACKAGED_SOURCE
    return str(path)


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
