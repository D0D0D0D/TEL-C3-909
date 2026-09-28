"""AGENTIC STAR Marketplace entry adapter for TEL-C3-909."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any, cast
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

from framework.errors import ConfigError
from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from framework.secrets.base import SecretProvider
from framework.secrets.context import current_secrets
from shared.services.llm.base_llm import BaseLLM

try:
    from framework.utils.config_loader import load_agent_config
except ImportError:  # AgentCore 1.0.0 / 1.0.1 compatibility
    from framework.utils.config_loader import load_config

    def load_agent_config(agent_dir: Path) -> dict[str, Any]:
        config_path = agent_dir / "config" / "config.yaml"
        return load_config(str(config_path)) if config_path.exists() else {}


try:
    from shared.bootstrap import run_agent_marketplace
except ModuleNotFoundError as exc:  # AgentCore 1.0.0 / 1.0.1 compatibility
    if exc.name != "shared.bootstrap":
        raise
    _BOOTSTRAP_IMPORT_ERROR: ModuleNotFoundError | None = exc

    def run_agent_marketplace(*args: Any, **kwargs: Any) -> None:
        del args, kwargs
        raise RuntimeError(
            "The installed AgentCore wheel does not provide Marketplace bootstrap support"
        ) from _BOOTSTRAP_IMPORT_ERROR
else:
    _BOOTSTRAP_IMPORT_ERROR = None


try:
    from shared.secrets.env_provider import EnvProvider
except ModuleNotFoundError as exc:  # AgentCore 1.0.0 / 1.0.1 compatibility
    if exc.name != "shared.secrets.env_provider":
        raise

    class EnvProvider(SecretProvider):  # type: ignore[no-redef]
        """Minimal process-environment provider for pre-Marketplace wheels."""

        def __init__(self, namespace: str = "", agent_name: str = "") -> None:
            self._namespace = namespace
            self._agent_name = agent_name

        def get(self, key: str, default: str | None = None) -> str | None:
            return os.environ.get(key, default)


from src.entry_adapter import SCOPE_KEYS, STG_DEFAULT_SCOPE_ENVS
from src.graph.graph import MonitoringAlertFirstTriageAgent

_AGENT_DIR = Path(__file__).resolve().parent
_NAMESPACE = "tel"
_AGENT_NAME = "TEL-C3-909"
_PROVIDER_AGENT_NAME = "tel_c3_909"

_AZURE_OPENAI_SECRET_KEYS = (
    "AZURE_OPENAI_API_KEY",
    "AZURE_OPENAI_ENDPOINT",
    "AZURE_OPENAI_DEPLOYMENT",
)
_SCOPE_ENV_KEYS = tuple(STG_DEFAULT_SCOPE_ENVS[key] for key in SCOPE_KEYS)
_REQUIRED_ENV_KEYS = (*_AZURE_OPENAI_SECRET_KEYS, *_SCOPE_ENV_KEYS)
_ALLOWED_ENV_KEYS = frozenset(_REQUIRED_ENV_KEYS)


class _AllowlistedEnvironmentProvider(EnvProvider):  # type: ignore[misc]
    """Expose only this single-agent container's declared environment values."""

    def __init__(self) -> None:
        super().__init__(namespace=_NAMESPACE, agent_name=_PROVIDER_AGENT_NAME)

    def get(self, key: str, default: str | None = None) -> str | None:
        if key not in _ALLOWED_ENV_KEYS:
            return default
        return super().get(key, default)


def _validate_scope_declarations() -> None:
    """Reject drift between the entry-adapter's data-driven scope declarations."""

    if not isinstance(SCOPE_KEYS, tuple) or not SCOPE_KEYS:
        raise RuntimeError("Marketplace SCOPE_KEYS must be a non-empty tuple")
    if not isinstance(STG_DEFAULT_SCOPE_ENVS, Mapping):
        raise RuntimeError("Marketplace STG_DEFAULT_SCOPE_ENVS must be a mapping")
    if set(STG_DEFAULT_SCOPE_ENVS) != set(SCOPE_KEYS):
        raise RuntimeError("Marketplace scope environment declarations do not match SCOPE_KEYS")
    if len(set(SCOPE_KEYS)) != len(SCOPE_KEYS):
        raise RuntimeError("Marketplace SCOPE_KEYS must not contain duplicates")
    for key in SCOPE_KEYS:
        env_name = STG_DEFAULT_SCOPE_ENVS[key]
        if (
            not isinstance(key, str)
            or not key
            or any(character.isspace() for character in key)
            or not key.isidentifier()
            or not isinstance(env_name, str)
            or not env_name
            or any(character.isspace() for character in env_name)
        ):
            raise RuntimeError("Marketplace scope declarations must contain clean key and environment names")


def _require_clean(provider: SecretProvider, key: str) -> str:
    """Resolve one required deployment value without repairing bad input."""

    value = provider.require(key)
    if not value or any(character.isspace() for character in value):
        raise ValueError(f"Marketplace environment variable {key!r} must be a non-empty string without whitespace")
    return value


def _require_azure_endpoint(provider: SecretProvider) -> str:
    """Reject ``AZURE_OPENAI_ENDPOINT`` values that are not a bare Azure resource endpoint.

    ``AzureOpenAIClient`` appends its own ``/openai/v1/`` path, so anything
    beyond a scheme and host silently produces a broken request URL, and the
    failure surfaces inside ``complete()``. A node that swallows that exception
    degrades quietly while the invocation still reports ``status: success``,
    which is why this has to be a startup check. The value is reported here,
    never repaired.
    """

    endpoint = _require_clean(provider, "AZURE_OPENAI_ENDPOINT")
    parts = urlsplit(endpoint)
    try:
        port = parts.port
    except ValueError:
        port = -1
    if (
        parts.scheme not in ("http", "https")
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or port == -1
        or parts.path.strip("/")
        or parts.query
        or parts.fragment
        or any(character.isspace() for character in endpoint)
    ):
        raise ValueError(
            "Marketplace environment variable 'AZURE_OPENAI_ENDPOINT' must be a bare Azure "
            "resource endpoint -- scheme and host only, with no path (such as "
            "'/openai'), query or fragment"
        )
    return endpoint


def _scope_from_environment(provider: SecretProvider) -> dict[str, str]:
    """Build the complete Marketplace scope from entry-adapter declarations."""

    _validate_scope_declarations()
    scope: dict[str, str] = {}
    for scope_key in SCOPE_KEYS:
        env_name = STG_DEFAULT_SCOPE_ENVS[scope_key]
        value = provider.require(env_name)
        if not value or any(character.isspace() for character in value):
            raise ValueError(
                f"Marketplace scope environment variable {env_name!r} " "must be a non-empty string without whitespace"
            )
        scope[scope_key] = value
    return scope


def _tool_schema(tool: Any) -> dict[str, Any]:
    """Translate a LangChain tool into the Azure/OpenAI function-tool shape."""

    args_schema = getattr(tool, "args_schema", None)
    if args_schema is None:
        parameters: dict[str, Any] = {"type": "object", "properties": {}}
    elif hasattr(args_schema, "model_json_schema"):
        parameters = cast(dict[str, Any], args_schema.model_json_schema())
    else:
        parameters = cast(dict[str, Any], args_schema.schema())
    return {
        "type": "function",
        "function": {
            "name": str(getattr(tool, "name", getattr(tool, "__name__", "tool"))),
            "description": str(getattr(tool, "description", "")),
            "parameters": parameters,
        },
    }


class _AzureOpenAIClientCompat(BaseLLM):
    """Small Azure chat-completions client for AgentCore 1.0.0/1.0.1."""

    def __init__(self, config: dict[str, str]) -> None:
        self._config = dict(config)
        self._tools: tuple[Any, ...] = ()

    @staticmethod
    def _messages(messages: Any) -> list[dict[str, Any]]:
        if isinstance(messages, str):
            return [{"role": "user", "content": messages}]
        normalized: list[dict[str, Any]] = []
        for message in messages:
            if isinstance(message, Mapping):
                normalized.append(dict(message))
                continue
            role = getattr(message, "type", "user")
            if role == "human":
                role = "user"
            elif role == "ai":
                role = "assistant"
            normalized.append({"role": role, "content": getattr(message, "content", str(message))})
        return normalized

    def complete(self, messages: list[Any] | str) -> dict[str, Any]:
        endpoint = self._config["azure_endpoint"].rstrip("/")
        deployment = quote(self._config["azure_deployment"], safe="")
        url = f"{endpoint}/openai/deployments/{deployment}/chat/completions" "?api-version=2024-10-21"
        payload: dict[str, Any] = {"messages": self._messages(messages)}
        if self._tools:
            payload["tools"] = [_tool_schema(tool) for tool in self._tools]
        request = Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "api-key": self._config["api_key"],
                "content-type": "application/json",
            },
            method="POST",
        )
        with urlopen(request, timeout=30) as response:  # noqa: S310 - operator-owned Azure endpoint
            response_payload = json.loads(response.read().decode("utf-8"))

        choices = response_payload.get("choices", [])
        message = choices[0].get("message", {}) if choices else {}
        tool_calls = []
        for call in message.get("tool_calls", []) or []:
            function = call.get("function", {})
            arguments = function.get("arguments", {})
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except json.JSONDecodeError:
                    arguments = {}
            tool_calls.append({"name": function.get("name", ""), "args": arguments})

        result: dict[str, Any] = {
            "content": message.get("content") or "",
            "tool_calls": tool_calls,
            "model": response_payload.get("model", self._config["azure_deployment"]),
        }
        usage = response_payload.get("usage")
        if isinstance(usage, Mapping):
            result["usage"] = {
                "prompt_tokens": usage.get("prompt_tokens", 0),
                "completion_tokens": usage.get("completion_tokens", 0),
                "model": result["model"],
            }
        return result

    def stream(self, messages: list[Any] | str) -> Iterator[str]:
        content = self.complete(messages).get("content", "")
        if content:
            yield str(content)

    def bind_tools(self, tools: list[Any]) -> "_AzureOpenAIClientCompat":
        self._tools = tuple(tools)
        return self


class MarketplaceAzureLLM(BaseLLM):
    """Resolve Azure connection values and create a fresh client per operation."""

    def __init__(self, *, tools: tuple[Any, ...] = ()) -> None:
        if tools:
            self._tools = tools

    def _new_client(self) -> BaseLLM:
        # Azure support is an optional SDK extra and therefore the sole lazy
        # import. Security/context imports remain eager for runner to_thread().
        try:
            from shared.services.llm.azure_openai_client import AzureOpenAIClient
        except ModuleNotFoundError as exc:  # AgentCore 1.0.0 / 1.0.1 compatibility
            if exc.name != "shared.services.llm.azure_openai_client":
                raise
            AzureOpenAIClient = _AzureOpenAIClientCompat

        provider = current_secrets()
        client: BaseLLM = AzureOpenAIClient(
            {
                "api_key": _require_clean(provider, "AZURE_OPENAI_API_KEY"),
                "azure_endpoint": _require_azure_endpoint(provider),
                "azure_deployment": _require_clean(provider, "AZURE_OPENAI_DEPLOYMENT"),
            }
        )
        tools = getattr(self, "_tools", ())
        return client.bind_tools(list(tools)) if tools else client

    def complete(self, messages: list[Any] | str) -> dict[str, Any]:
        return cast(dict[str, Any], self._new_client().complete(messages))

    def stream(self, messages: list[Any] | str) -> Iterator[str]:
        yield from self._new_client().stream(messages)

    def bind_tools(self, tools: list[Any]) -> "MarketplaceAzureLLM":
        return type(self)(tools=tuple(tools))


class MarketplaceGraph(MonitoringAlertFirstTriageAgent):
    """Marketplace-only scope, trust, HITL, and Azure boundary."""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        marketplace_config = dict(load_agent_config(_AGENT_DIR) if config is None else config)
        if "budget_usd" not in marketplace_config:
            raise ConfigError("Marketplace Cat 3 config must declare budget_usd at construction")
        # compile() precedes provision_secrets(); nodes capture this stateless
        # facade now, while credentials are resolved only during each LLM call.
        marketplace_config["llm"] = MarketplaceAzureLLM()
        super().__init__(config=marketplace_config)

    def invoke(
        self,
        user_input: str,
        session_id: str = "",
        ctx: InvocationContext | None = None,
        input_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        # Runner conversation_history is untrusted free text outside this AG's
        # S-2 gate. Rebuild input_context solely from adapter-owned scope.
        del input_context
        marketplace_input_context = _scope_from_environment(_AllowlistedEnvironmentProvider())

        # Marketplace has neither INTERNAL trust nor a resume channel. Preserve
        # caller/trace identity while enforcing the runner contract.
        base_ctx = ctx if ctx is not None else InvocationContext(session_id=session_id or "")
        marketplace_ctx = replace(
            base_ctx,
            caller_trust_level=TrustLevel.VERIFIED_EXTERNAL,
            hitl_allowed=False,
        )
        result = cast(
            dict[str, Any],
            super().invoke(
                user_input,
                session_id=session_id,
                ctx=marketplace_ctx,
                input_context=marketplace_input_context,
            ),
        )
        if result.get("status") == AgentStatus.AWAITING_HUMAN.value:
            raise RuntimeError("Marketplace invocation cannot return AWAITING_HUMAN")
        return result

    def provision_secrets(self, provider: SecretProvider) -> None:
        # The runner provider may expose dotenv or other agents' keys. Startup
        # accepts only this adapter's allowlisted process environment.
        del provider
        environment_provider = _AllowlistedEnvironmentProvider()
        for key in _AZURE_OPENAI_SECRET_KEYS:
            _require_clean(environment_provider, key)
        _scope_from_environment(environment_provider)
        _require_azure_endpoint(environment_provider)
        super().provision_secrets(environment_provider)


def main() -> None:
    run_agent_marketplace(
        MarketplaceGraph,
        agent_name=_AGENT_NAME,
        namespace=_NAMESPACE,
        config=load_agent_config(_AGENT_DIR),
    )


if __name__ == "__main__":
    main()
