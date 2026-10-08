from __future__ import annotations

from dataclasses import replace

import pytest

from prime_harness.token_harness import (
    ExactDuplicateModule,
    HarnessError,
    ModuleMetadata,
    ModuleResult,
    ProviderMessage,
    RepeatedLineSuppressor,
    StructuredRequest,
    TokenHarness,
)


def _workload_request(workload: str) -> StructuredRequest:
    noise_count = {
        "short-session": 1,
        "long-session": 40,
        "repeated-tool-output": 20,
        "large-terminal-log": 100,
        "repeated-source-reads": 20,
        "multiple-files": 8,
        "active-debugging": 30,
        "build-test-fix-cycle": 50,
        "repository-plan": 15,
        "exact-code-edit": 10,
        "structured-tool-call": 12,
    }[workload]
    history = ProviderMessage(
        message_id=f"{workload}-tool-output",
        role="tool",
        content="\n".join(["same warning: no cached result"] * noise_count),
        provider_fields={"tool_call_id": f"call-{workload}"},
        compressible=True,
        kind="tool_result",
    )
    return StructuredRequest(
        provider="example-provider",
        model="example-model",
        messages=(
            ProviderMessage(
                "system",
                "system",
                "Keep the exact current task in view.",
                protected=True,
            ),
            ProviderMessage(
                "user",
                "user",
                f"Fix the failure from workload {workload}.",
                protected=True,
            ),
            history,
            ProviderMessage(
                f"{workload}-duplicate",
                "assistant",
                "Previous status: tests still running.",
                kind="history",
            ),
            ProviderMessage(
                f"{workload}-duplicate-again",
                "assistant",
                "Previous status: tests still running.",
                kind="history",
            ),
            ProviderMessage(
                f"{workload}-active-code",
                "user",
                {
                    "path": "src/module.py",
                    "line": "if result is None: raise RuntimeError('required')",
                },
                protected=True,
            ),
            ProviderMessage(
                f"{workload}-tool-call",
                "assistant",
                [{"id": "call-test", "type": "function", "function": {"name": "run_tests"}}],
                provider_fields={"tool_calls": [{"id": "call-test"}]},
                kind="tool_call",
            ),
        ),
        tools=(
            {
                "type": "function",
                "function": {
                    "name": "run_tests",
                    "parameters": {
                        "type": "object",
                        "properties": {"selector": {"type": "string"}},
                        "required": ["selector"],
                    },
                },
            },
        ),
        parameters={"temperature": 0, "response_format": {"type": "json_object"}},
    )


@pytest.mark.parametrize(
    "workload",
    [
        "short-session",
        "long-session",
        "repeated-tool-output",
        "large-terminal-log",
        "repeated-source-reads",
        "multiple-files",
        "active-debugging",
        "build-test-fix-cycle",
        "repository-plan",
        "exact-code-edit",
        "structured-tool-call",
    ],
)
def test_structured_workloads_preserve_protected_content_and_measure(workload: str) -> None:
    original = _workload_request(workload)
    prepared = TokenHarness((ExactDuplicateModule(), RepeatedLineSuppressor())).prepare(original)

    assert prepared.request.provider == original.provider
    assert prepared.request.model == original.model
    assert prepared.request.tools == original.tools
    assert prepared.request.parameters == original.parameters
    assert prepared.request.messages[0] == original.messages[0]
    assert prepared.request.messages[1] == original.messages[1]
    prepared_by_id = {message.message_id: message for message in prepared.request.messages}
    assert prepared_by_id[f"{workload}-active-code"] == original.messages[5]
    assert len(prepared.request.messages) == len(original.messages) - 1
    compressed_tool_output = prepared_by_id[f"{workload}-tool-output"].content
    original_tool_output = original.messages[2].content
    assert isinstance(compressed_tool_output, str)
    assert isinstance(original_tool_output, str)
    if workload == "short-session":
        assert compressed_tool_output == original_tool_output
    else:
        assert compressed_tool_output.endswith("the original input remains unchanged.]")
    assert (
        original_tool_output.count("same warning")
        == {
            "short-session": 1,
            "long-session": 40,
            "repeated-tool-output": 20,
            "large-terminal-log": 100,
            "repeated-source-reads": 20,
            "multiple-files": 8,
            "active-debugging": 30,
            "build-test-fix-cycle": 50,
            "repository-plan": 15,
            "exact-code-edit": 10,
            "structured-tool-call": 12,
        }[workload]
    )
    assert prepared.metrics.input_bytes > prepared.metrics.output_bytes
    assert prepared.metrics.input_token_estimate > prepared.metrics.output_token_estimate
    assert prepared.metrics.estimator.startswith("serialized JSON")
    assert prepared.metrics.elapsed_ms >= 0
    assert prepared.metrics.stages
    assert prepared.removed_message_ids == (f"{workload}-duplicate-again",)


def test_provider_payload_keeps_structured_calls_and_does_not_emit_harness_metadata() -> None:
    prepared = TokenHarness(()).prepare(_workload_request("structured-tool-call"))

    assert prepared.payload["tools"] == list(prepared.request.tools)
    messages = prepared.payload["messages"]
    assert isinstance(messages, list)
    assert messages[-1] == {
        "role": "assistant",
        "tool_calls": [{"id": "call-test"}],
        "content": [{"id": "call-test", "type": "function", "function": {"name": "run_tests"}}],
    }
    assert "protected" not in str(prepared.payload)
    assert "compressible" not in str(prepared.payload)


def test_modules_can_be_swapped_without_changing_request_contract() -> None:
    request = _workload_request("long-session")
    baseline = TokenHarness(()).prepare(request)
    processed = TokenHarness((ExactDuplicateModule(),)).prepare(request)

    assert baseline.request.messages == request.messages
    assert len(processed.request.messages) == len(request.messages) - 1
    assert processed.metrics.stages[-1].module == "exact-duplicate-removal"


class _ProtectedContentLoss:
    @property
    def metadata(self) -> ModuleMetadata:
        return ModuleMetadata("bad-plugin", "1", "compress", ("test-invalid-output",))

    def process(self, request: StructuredRequest) -> ModuleResult:
        return ModuleResult(
            replace(request, messages=tuple(message for message in request.messages[1:])),
            removed_message_ids=(request.messages[0].message_id,),
        )


class _ExceptionWithSensitiveText:
    @property
    def metadata(self) -> ModuleMetadata:
        return ModuleMetadata("broken-plugin", "1", "compress", ("test-failure",))

    def process(self, request: StructuredRequest) -> ModuleResult:
        raise RuntimeError("untrusted plugin error includes sensitive-prompt-value")


class _UnreportedRemoval:
    @property
    def metadata(self) -> ModuleMetadata:
        return ModuleMetadata("bad-provenance", "1", "suppress", ("test-invalid-provenance",))

    def process(self, request: StructuredRequest) -> ModuleResult:
        return ModuleResult(replace(request, messages=request.messages[1:]))


def test_protected_content_loss_fails_closed() -> None:
    with pytest.raises(HarnessError, match="changed protected message"):
        TokenHarness((_ProtectedContentLoss(),)).prepare(_workload_request("short-session"))


def test_system_and_user_messages_are_protected_even_without_annotations() -> None:
    request = StructuredRequest(
        provider="example-provider",
        model="example-model",
        messages=(
            ProviderMessage("system", "system", "exact policy"),
            ProviderMessage("current", "user", "exact request"),
        ),
    )
    prepared = TokenHarness(()).prepare(request)

    assert [message.protected for message in prepared.request.messages] == [True, True]
    assert [message.protected for message in request.messages] == [False, False]


def test_plugin_exception_is_sanitized_and_blocks_prepared_output() -> None:
    with pytest.raises(HarnessError, match="provider dispatch blocked") as error:
        TokenHarness((_ExceptionWithSensitiveText(),)).prepare(_workload_request("short-session"))

    assert "sensitive-prompt-value" not in str(error.value)


def test_unreported_removal_is_rejected() -> None:
    with pytest.raises(HarnessError, match="inconsistent removal provenance"):
        TokenHarness((_UnreportedRemoval(),)).prepare(_workload_request("short-session"))


def test_plugin_cannot_mutate_tool_schema_or_provider_parameters() -> None:
    class _ToolMutation:
        @property
        def metadata(self) -> ModuleMetadata:
            return ModuleMetadata("bad-tools", "1", "assemble", ("test-tool-mutation",))

        def process(self, request: StructuredRequest) -> ModuleResult:
            return ModuleResult(replace(request, tools=()))

    with pytest.raises(HarnessError, match="changed provider, model, tools, or parameters"):
        TokenHarness((_ToolMutation(),)).prepare(_workload_request("short-session"))


def test_reserved_request_parameters_cannot_replace_structural_fields() -> None:
    request = replace(
        _workload_request("short-session"),
        parameters={"messages": [{"role": "system", "content": "replaced"}]},
    )

    with pytest.raises(HarnessError, match="Reserved provider parameter"):
        TokenHarness(()).prepare(request)


def test_token_estimator_failure_blocks_prepared_output() -> None:
    class _FailingEstimator:
        name = "failing-test-estimator"

        def estimate(self, payload: dict[str, object]) -> int:
            raise RuntimeError("prompt data must not appear here")

    with pytest.raises(HarnessError, match="failing-test-estimator failed") as error:
        TokenHarness((), _FailingEstimator()).prepare(_workload_request("short-session"))

    assert "prompt data" not in str(error.value)
