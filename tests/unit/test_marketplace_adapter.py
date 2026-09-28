"""AGENTIC STAR Marketplace adapter boundary tests for TEL-C3-909."""

from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType

import pytest
from framework.errors import ConfigError
from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from framework.secrets.base import MissingSecret, NullProvider
from framework.secrets.context import bound_secrets

import cli
from src.graph.graph import MonitoringAlertFirstTriageAgent
from src.nodes.triage_finalize_node import TriageFinalizeNode


def _set_required_environment(monkeypatch, suffix: str = "one") -> dict[str, str]:
    values = {key: f"test-{suffix}-{key.lower()}" for key in cli._REQUIRED_ENV_KEYS}
    # The startup gate requires a bare Azure resource endpoint, so
    # this value cannot be a placeholder like the other secrets.
    values["AZURE_OPENAI_ENDPOINT"] = f"https://{suffix}.services.ai.azure.example"
    values["AZURE_OPENAI_ENDPOINT"] = f"https://{suffix}.azure.example"
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    return values


def test_marketplace_discards_runner_context_and_builds_declared_scope(monkeypatch):
    values = _set_required_environment(monkeypatch)
    captured = {}

    def parent_invoke(self, user_input, session_id="", ctx=None, input_context=None):
        captured.update(
            user_input=user_input,
            session_id=session_id,
            ctx=ctx,
            input_context=input_context,
        )
        return {"status": AgentStatus.SUCCESS.value}

    monkeypatch.setattr(MonitoringAlertFirstTriageAgent, "invoke", parent_invoke)
    original_ctx = InvocationContext(
        caller_id="marketplace-user",
        caller_trust_level=TrustLevel.INTERNAL,
        hitl_allowed=True,
    )
    result = cli.MarketplaceGraph().invoke(
        '{"alerts": []}',
        session_id="marketplace-session",
        ctx=original_ctx,
        input_context={
            "conversation_history": [{"role": "user", "content": "unscanned"}],
            "production_company_id": "attacker-company",
            "project_id": "attacker-project",
            "future_runner_key": "must-not-enter-state",
        },
    )

    assert result == {"status": AgentStatus.SUCCESS.value}
    assert captured["user_input"] == '{"alerts": []}'
    assert captured["input_context"] == {
        "production_company_id": values["STG_DEFAULT_PRODUCTION_COMPANY_ID"],
        "project_id": values["STG_DEFAULT_PROJECT_ID"],
    }
    assert captured["ctx"] is not original_ctx
    assert captured["ctx"].caller_id == "marketplace-user"
    assert captured["ctx"].caller_trust_level is TrustLevel.VERIFIED_EXTERNAL
    assert captured["ctx"].hitl_allowed is False


def test_scope_resolution_follows_entry_adapter_declarations(monkeypatch):
    monkeypatch.setattr(cli, "SCOPE_KEYS", ("account_id", "workspace_id"))
    monkeypatch.setattr(
        cli,
        "STG_DEFAULT_SCOPE_ENVS",
        {
            "account_id": "MARKETPLACE_ACCOUNT",
            "workspace_id": "MARKETPLACE_WORKSPACE",
        },
    )

    class Provider(NullProvider):
        def get(self, key, default=None):
            return {
                "MARKETPLACE_ACCOUNT": "account-1",
                "MARKETPLACE_WORKSPACE": "workspace-1",
            }.get(key, default)

    assert cli._scope_from_environment(Provider()) == {
        "account_id": "account-1",
        "workspace_id": "workspace-1",
    }


@pytest.mark.parametrize("missing_key", cli._REQUIRED_ENV_KEYS)
def test_missing_required_environment_fails_during_startup(monkeypatch, missing_key):
    _set_required_environment(monkeypatch)
    monkeypatch.delenv(missing_key)

    with pytest.raises(MissingSecret, match=missing_key):
        cli.MarketplaceGraph().provision_secrets(NullProvider())


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://example.cognitiveservices.azure.com/openai/responses?api-version=2025-04-01-preview",
        "https://example.services.ai.azure.example/openai",
        "https://example.services.ai.azure.example/v1",
        "https://example.services.ai.azure.example?api-version=2024-10-21",
        "example.services.ai.azure.example",
    ],
    ids=["api-path", "openai-path", "base-path", "query-only", "missing-scheme"],
)
def test_unusable_azure_endpoint_shape_fails_during_startup(monkeypatch, endpoint):
    """AzureOpenAIClient appends its own path, so anything but a bare endpoint breaks.

    The resulting error surfaces inside complete(); a node that swallows it
    degrades silently while the invocation still reports status: success, so the
    adapter has to reject the shape at startup instead.
    """

    _set_required_environment(monkeypatch)
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", endpoint)

    with pytest.raises(ValueError, match="bare"):
        cli.MarketplaceGraph().provision_secrets(NullProvider())


@pytest.mark.parametrize(
    "invalid",
    ["", " ", "bad value", " leading", "trailing ", "bad\nvalue", "bad\tvalue"],
)
@pytest.mark.parametrize("scope_env", cli._SCOPE_ENV_KEYS)
def test_empty_or_whitespace_scope_fails_closed_during_startup(monkeypatch, scope_env, invalid):
    _set_required_environment(monkeypatch)
    monkeypatch.setenv(scope_env, invalid)

    with pytest.raises(ValueError, match=scope_env):
        cli.MarketplaceGraph().provision_secrets(NullProvider())


@pytest.mark.parametrize("azure_key", cli._AZURE_OPENAI_SECRET_KEYS)
def test_empty_or_whitespace_azure_secret_fails_during_startup(monkeypatch, azure_key):
    _set_required_environment(monkeypatch)
    monkeypatch.setenv(azure_key, "bad value")

    with pytest.raises(ValueError, match=azure_key):
        cli.MarketplaceGraph().provision_secrets(NullProvider())


def test_azure_llm_resolves_fresh_credentials_and_client_for_every_call(monkeypatch):
    configs = []

    class FakeAzureOpenAIClient:
        def __init__(self, config):
            configs.append(dict(config))

        def complete(self, messages):
            return {"content": "ok", "tool_calls": [], "messages": messages}

        def stream(self, messages):
            del messages
            yield "ok"

        def bind_tools(self, tools):
            del tools
            return self

    azure_module = ModuleType("shared.services.llm.azure_openai_client")
    azure_module.AzureOpenAIClient = FakeAzureOpenAIClient
    monkeypatch.setitem(sys.modules, azure_module.__name__, azure_module)

    provider = cli._AllowlistedEnvironmentProvider()
    llm = cli.MarketplaceAzureLLM()
    assert vars(llm) == {}

    with bound_secrets(provider):
        first = _set_required_environment(monkeypatch, "first")
        llm.complete("first")
        assert vars(llm) == {}

        second = _set_required_environment(monkeypatch, "second")
        llm.complete("second")
        assert vars(llm) == {}

    assert configs == [
        {
            "api_key": first["AZURE_OPENAI_API_KEY"],
            "azure_endpoint": first["AZURE_OPENAI_ENDPOINT"],
            "azure_deployment": first["AZURE_OPENAI_DEPLOYMENT"],
        },
        {
            "api_key": second["AZURE_OPENAI_API_KEY"],
            "azure_endpoint": second["AZURE_OPENAI_ENDPOINT"],
            "azure_deployment": second["AZURE_OPENAI_DEPLOYMENT"],
        },
    ]


def test_cat3_config_keeps_budget_iterations_and_custom_finalize():
    config = cli.load_agent_config(Path(__file__).resolve().parents[2])
    agent = cli.MarketplaceGraph(config)

    assert isinstance(agent.config["llm"], cli.MarketplaceAzureLLM)
    assert agent.config["budget_usd"] == 1.50
    assert agent.max_iterations == 24

    agent.compile()

    assert isinstance(agent._nodes["think"]._llm, cli.MarketplaceAzureLLM)
    assert isinstance(agent._nodes["finalize"], TriageFinalizeNode)


def test_cat3_construction_rejects_missing_budget():
    with pytest.raises(ConfigError, match="budget_usd"):
        cli.MarketplaceGraph({})


def test_awaiting_human_is_a_marketplace_failure(monkeypatch):
    _set_required_environment(monkeypatch)

    def parent_invoke(self, user_input, session_id="", ctx=None, input_context=None):
        del self, user_input, session_id, ctx, input_context
        return {"status": AgentStatus.AWAITING_HUMAN.value}

    monkeypatch.setattr(MonitoringAlertFirstTriageAgent, "invoke", parent_invoke)

    with pytest.raises(RuntimeError, match="AWAITING_HUMAN"):
        cli.MarketplaceGraph().invoke("alert")


def test_provisioned_provider_is_environment_only_and_allowlisted(monkeypatch):
    values = _set_required_environment(monkeypatch)
    monkeypatch.setenv("DIAG_READONLY_CREDENTIALS", "not-used-at-external-trust")
    monkeypatch.setenv("UNRELATED_PLATFORM_SECRET", "must-not-be-visible")
    agent = cli.MarketplaceGraph()

    agent.provision_secrets(NullProvider())

    provider = agent._secrets_provider
    assert {key: provider.require(key) for key in cli._REQUIRED_ENV_KEYS} == values
    assert provider.get("DIAG_READONLY_CREDENTIALS") is None
    assert provider.get("UNRELATED_PLATFORM_SECRET") is None


def test_main_uses_marketplace_contract_and_repo_config(monkeypatch):
    called = {}

    def fake_runner(agent_cls, **kwargs):
        called["agent_cls"] = agent_cls
        called.update(kwargs)

    monkeypatch.setattr(cli, "run_agent_marketplace", fake_runner)
    cli.main()

    assert called["agent_cls"] is cli.MarketplaceGraph
    assert called["agent_name"] == "TEL-C3-909"
    assert called["namespace"] == "tel"
    assert called["config"]["budget_usd"] == 1.50
    assert called["config"]["max_iterations"] == 24
