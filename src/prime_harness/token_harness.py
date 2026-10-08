from __future__ import annotations

import copy
import json
import time
from dataclasses import dataclass, field, replace
from math import ceil
from typing import Protocol


class HarnessError(RuntimeError):
    """A context pipeline failure that blocks provider dispatch."""


@dataclass(frozen=True, slots=True)
class ProviderMessage:
    message_id: str
    role: str
    content: object
    provider_fields: dict[str, object] = field(default_factory=dict)
    protected: bool = False
    compressible: bool = False
    kind: str = "message"


@dataclass(frozen=True, slots=True)
class StructuredRequest:
    provider: str
    model: str
    messages: tuple[ProviderMessage, ...]
    tools: tuple[dict[str, object], ...] = ()
    parameters: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ModuleMetadata:
    name: str
    version: str
    stage: str
    capabilities: tuple[str, ...]
    supported_input_types: tuple[str, ...] = ("structured_request",)
    requirements: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ModuleResult:
    request: StructuredRequest
    removed_message_ids: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


class ContextModule(Protocol):
    @property
    def metadata(self) -> ModuleMetadata: ...

    def process(self, request: StructuredRequest) -> ModuleResult: ...


class TokenEstimator(Protocol):
    @property
    def name(self) -> str: ...

    def estimate(self, payload: dict[str, object]) -> int: ...


class CharacterTokenEstimate:
    """Prototype-only estimate; provider tokenization is not available here."""

    name = "serialized JSON characters / 4 (heuristic)"

    def estimate(self, payload: dict[str, object]) -> int:
        serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        return ceil(len(serialized) / 4)


@dataclass(frozen=True, slots=True)
class StageMetrics:
    module: str
    input_bytes: int
    output_bytes: int
    elapsed_ms: float
    removed_message_ids: tuple[str, ...]
    warnings: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PipelineMetrics:
    input_bytes: int
    output_bytes: int
    input_token_estimate: int
    output_token_estimate: int
    estimator: str
    reduction_percent: float
    compression_ratio: float | None
    elapsed_ms: float
    stages: tuple[StageMetrics, ...]


@dataclass(frozen=True, slots=True)
class PreparedRequest:
    request: StructuredRequest
    payload: dict[str, object]
    metrics: PipelineMetrics
    removed_message_ids: tuple[str, ...]
    warnings: tuple[str, ...]


def _payload(request: StructuredRequest) -> dict[str, object]:
    messages: list[dict[str, object]] = []
    for message in request.messages:
        item = {"role": message.role, **copy.deepcopy(message.provider_fields)}
        item["content"] = copy.deepcopy(message.content)
        messages.append(item)
    return {
        **copy.deepcopy(request.parameters),
        "model": request.model,
        "messages": messages,
        "tools": copy.deepcopy(list(request.tools)),
    }


def _encoded_size(payload: dict[str, object]) -> int:
    try:
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError):
        raise HarnessError("Request is not JSON serializable; provider dispatch blocked") from None
    return len(encoded)


def _message_fingerprint(message: ProviderMessage) -> str:
    return json.dumps(
        {
            "role": message.role,
            "content": message.content,
            "provider_fields": message.provider_fields,
            "kind": message.kind,
        },
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _validate_request(request: StructuredRequest) -> None:
    if not request.provider or not request.model:
        raise HarnessError("Provider and model are required; provider dispatch blocked")
    if {"model", "messages", "tools"} & request.parameters.keys():
        raise HarnessError("Reserved provider parameter; provider dispatch blocked")
    identifiers = [message.message_id for message in request.messages]
    if any(not identifier for identifier in identifiers) or len(identifiers) != len(
        set(identifiers)
    ):
        raise HarnessError("Message identities must be unique; provider dispatch blocked")
    for message in request.messages:
        if not message.role:
            raise HarnessError("Message roles are required; provider dispatch blocked")
        if {"role", "content"} & message.provider_fields.keys():
            raise HarnessError("Reserved provider message field; provider dispatch blocked")
    _encoded_size(_payload(request))


def _protect_request(request: StructuredRequest) -> StructuredRequest:
    protected_roles = {"system", "developer", "user"}
    messages = tuple(
        replace(
            message,
            protected=message.protected or message.role in protected_roles,
        )
        for message in request.messages
    )
    return replace(request, messages=messages)


def _validate_stage(
    before: StructuredRequest,
    result: ModuleResult,
    module_name: str,
) -> None:
    if not isinstance(result, ModuleResult) or not isinstance(result.request, StructuredRequest):
        raise HarnessError(f"Context module {module_name} returned malformed output")
    after = result.request
    if (
        after.provider != before.provider
        or after.model != before.model
        or after.tools != before.tools
        or after.parameters != before.parameters
    ):
        raise HarnessError(
            f"Context module {module_name} changed provider, model, tools, or parameters"
        )
    try:
        _validate_request(after)
    except HarnessError as exc:
        raise HarnessError(f"Context module {module_name} returned invalid output") from exc

    before_by_id = {message.message_id: message for message in before.messages}
    after_by_id = {message.message_id: message for message in after.messages}
    missing_ids = set(before_by_id) - set(after_by_id)
    reported_ids = result.removed_message_ids
    if len(reported_ids) != len(set(reported_ids)) or set(reported_ids) != missing_ids:
        raise HarnessError(f"Context module {module_name} returned inconsistent removal provenance")

    protected = {
        identifier: message for identifier, message in before_by_id.items() if message.protected
    }
    for identifier, original in protected.items():
        transformed = after_by_id.get(identifier)
        if transformed != original:
            raise HarnessError(
                f"Context module {module_name} changed protected message {identifier}"
            )
    protected_order = [message.message_id for message in after.messages if message.protected]
    expected_order = [message.message_id for message in before.messages if message.protected]
    if protected_order != expected_order:
        raise HarnessError(f"Context module {module_name} reordered protected messages")


def _estimate_tokens(estimator: TokenEstimator, payload: dict[str, object]) -> int:
    try:
        estimate = estimator.estimate(payload)
    except Exception:
        raise HarnessError(
            f"Token estimator {estimator.name} failed; provider dispatch blocked"
        ) from None
    if not isinstance(estimate, int) or estimate < 0:
        raise HarnessError(
            f"Token estimator {estimator.name} returned invalid output; provider dispatch blocked"
        )
    return estimate


class ExactDuplicateModule:
    @property
    def metadata(self) -> ModuleMetadata:
        return ModuleMetadata(
            name="exact-duplicate-removal",
            version="1",
            stage="deduplicate",
            capabilities=("remove-exact-duplicate-history",),
        )

    def process(self, request: StructuredRequest) -> ModuleResult:
        seen: set[str] = set()
        messages: list[ProviderMessage] = []
        removed: list[str] = []
        for message in request.messages:
            fingerprint = _message_fingerprint(message)
            if not message.protected and message.kind == "history" and fingerprint in seen:
                removed.append(message.message_id)
                continue
            messages.append(message)
            if message.kind == "history":
                seen.add(fingerprint)
        return ModuleResult(
            request=StructuredRequest(
                provider=request.provider,
                model=request.model,
                messages=tuple(messages),
                tools=copy.deepcopy(request.tools),
                parameters=copy.deepcopy(request.parameters),
            ),
            removed_message_ids=tuple(removed),
        )


class RepeatedLineSuppressor:
    @property
    def metadata(self) -> ModuleMetadata:
        return ModuleMetadata(
            name="annotated-repeated-line-suppression",
            version="1",
            stage="suppress",
            capabilities=("suppress-identical-lines-in-annotated-tool-output",),
            requirements=("input marked compressible by caller",),
        )

    def process(self, request: StructuredRequest) -> ModuleResult:
        messages: list[ProviderMessage] = []
        warnings: list[str] = []
        for message in request.messages:
            if (
                not message.compressible
                or message.protected
                or message.kind != "tool_result"
                or not isinstance(message.content, str)
            ):
                messages.append(message)
                continue

            lines = message.content.splitlines()
            unique_lines = list(dict.fromkeys(lines))
            suppressed_count = len(lines) - len(unique_lines)
            if suppressed_count == 0:
                messages.append(message)
                continue

            annotation = (
                f"[Prime Harness suppressed {suppressed_count} identical lines; "
                "the original input remains unchanged.]"
            )
            content = "\n".join([*unique_lines, annotation])
            messages.append(
                ProviderMessage(
                    message_id=message.message_id,
                    role=message.role,
                    content=content,
                    provider_fields=copy.deepcopy(message.provider_fields),
                    protected=message.protected,
                    compressible=message.compressible,
                    kind=message.kind,
                )
            )
            warnings.append(f"{message.message_id}: suppressed {suppressed_count} identical lines")

        return ModuleResult(
            request=StructuredRequest(
                provider=request.provider,
                model=request.model,
                messages=tuple(messages),
                tools=copy.deepcopy(request.tools),
                parameters=copy.deepcopy(request.parameters),
            ),
            warnings=tuple(warnings),
        )


class TokenHarness:
    def __init__(
        self,
        modules: tuple[ContextModule, ...],
        estimator: TokenEstimator | None = None,
    ) -> None:
        self.modules = modules
        self.estimator = estimator or CharacterTokenEstimate()

    def prepare(self, request: StructuredRequest) -> PreparedRequest:
        _validate_request(request)
        original_payload = _payload(request)
        working = copy.deepcopy(request)
        input_bytes = _encoded_size(original_payload)
        input_tokens = _estimate_tokens(self.estimator, original_payload)
        stages: list[StageMetrics] = []
        all_removed: list[str] = []
        all_warnings: list[str] = []
        started = time.perf_counter()
        protection_started = time.perf_counter()
        working = _protect_request(working)
        stages.append(
            StageMetrics(
                module="builtin-protection",
                input_bytes=input_bytes,
                output_bytes=_encoded_size(_payload(working)),
                elapsed_ms=(time.perf_counter() - protection_started) * 1000,
                removed_message_ids=(),
                warnings=(),
            )
        )

        for module in self.modules:
            before = working
            before_bytes = _encoded_size(_payload(before))
            stage_started = time.perf_counter()
            try:
                result = module.process(copy.deepcopy(before))
            except Exception:
                raise HarnessError(
                    f"Context module {module.metadata.name} failed; provider dispatch blocked"
                ) from None
            _validate_stage(before, result, module.metadata.name)
            working = result.request
            stage_elapsed = (time.perf_counter() - stage_started) * 1000
            after_bytes = _encoded_size(_payload(working))
            stages.append(
                StageMetrics(
                    module=module.metadata.name,
                    input_bytes=before_bytes,
                    output_bytes=after_bytes,
                    elapsed_ms=stage_elapsed,
                    removed_message_ids=result.removed_message_ids,
                    warnings=result.warnings,
                )
            )
            all_removed.extend(result.removed_message_ids)
            all_warnings.extend(result.warnings)

        output_payload = _payload(working)
        output_bytes = _encoded_size(output_payload)
        output_tokens = _estimate_tokens(self.estimator, output_payload)
        reduction = ((input_bytes - output_bytes) / input_bytes) * 100 if input_bytes else 0.0
        ratio = input_bytes / output_bytes if output_bytes else None
        metrics = PipelineMetrics(
            input_bytes=input_bytes,
            output_bytes=output_bytes,
            input_token_estimate=input_tokens,
            output_token_estimate=output_tokens,
            estimator=self.estimator.name,
            reduction_percent=reduction,
            compression_ratio=ratio,
            elapsed_ms=(time.perf_counter() - started) * 1000,
            stages=tuple(stages),
        )
        return PreparedRequest(
            request=working,
            payload=output_payload,
            metrics=metrics,
            removed_message_ids=tuple(all_removed),
            warnings=tuple(all_warnings),
        )
