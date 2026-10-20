"""Building the model provider the model configuration describes.

Every channel builds its provider here, so the command line, the HTTP
application and the enrichment pass reason with the same one. `none` builds
nothing. The flat shape of `model.json` builds one adapter over one endpoint.
`profiles` builds one adapter per profile a role names and puts them behind a
`FallbackProvider`.

A profile's key is read from the environment variable its `api_key_env` names
and from nowhere else. A profile whose variable is unset or empty is skipped
with a warning that names the variable and never its value; when no chat
profile is left, the error names every variable that was missing. `scripted`
is not built here: it replays a script the command line names.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable, Mapping

import httpx

from oska.config import ModelConfig, ModelProfile
from oska.planner.anthropic import AnthropicProvider
from oska.planner.chain import FallbackProvider
from oska.planner.openai_compatible import OpenAICompatibleProvider
from oska.planner.provider import ModelProvider, ModelProviderError

__all__ = ["build_provider"]

Warn = Callable[[str], None]

_ADAPTERS: dict[str, type[OpenAICompatibleProvider] | type[AnthropicProvider]] = {
    "openai_compatible": OpenAICompatibleProvider,
    "anthropic": AnthropicProvider,
}


def build_provider(
    model_config: ModelConfig,
    *,
    env: Mapping[str, str] | None = None,
    warn: Warn | None = None,
    transport: httpx.BaseTransport | None = None,
) -> ModelProvider | None:
    """The provider the configuration describes, or None when it names none.

    `env` is where profile keys are read from, the process environment unless
    given; `warn` is told about each skipped profile, standard error unless
    given; `transport` is handed to every adapter.
    """
    name = model_config.provider
    if name == "none":
        return None
    if name == "scripted":
        raise ModelProviderError(
            "the scripted provider replays a script and is not built from model.json"
        )
    if name == "profiles":
        return _profiles(
            model_config, os.environ if env is None else env, warn or _stderr, transport
        )
    return _ADAPTERS[name].from_config(model_config, transport=transport)


def _profiles(
    model_config: ModelConfig,
    env: Mapping[str, str],
    warn: Warn,
    transport: httpx.BaseTransport | None,
) -> FallbackProvider:
    roles = model_config.roles
    if roles is None or not roles.chat:
        raise ModelProviderError("provider profiles needs roles.chat to name at least one profile")
    built: dict[str, ModelProvider] = {}
    missing: dict[str, str] = {}
    for name in dict.fromkeys([*roles.chat, *roles.embeddings]):
        profile = model_config.profiles[name]
        key = _key(profile, env)
        if key is None:
            missing[name] = profile.api_key_env
            warn(
                f"warning: model profile {name} is skipped: the variable {profile.api_key_env} "
                "is not set"
            )
            continue
        built[name] = _ADAPTERS[profile.adapter].from_profile(
            profile, key or None, transport=transport
        )
    chat = [(name, built[name]) for name in roles.chat if name in built]
    if not chat:
        variables = ", ".join(sorted({missing[name] for name in roles.chat if name in missing}))
        raise ModelProviderError(
            f"no chat profile is usable: set {variables}" if variables
            else "no chat profile is usable"
        )
    embeddings = [(name, built[name]) for name in roles.embeddings if name in built]
    return FallbackProvider(chat, embeddings)


def _key(profile: ModelProfile, env: Mapping[str, str]) -> str | None:
    """The profile's key, "" when it names no variable, None when its variable is empty."""
    if not profile.api_key_env:
        return ""
    value = str(env.get(profile.api_key_env, "")).strip()
    return value or None


def _stderr(message: str) -> None:
    print(message, file=sys.stderr)
