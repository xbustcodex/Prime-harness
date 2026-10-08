# Prime Token Harness: Investigation and Architecture

**Status:** investigation and isolated offline prototype complete. Production
integration is blocked on access to the PrimePi source tree containing its online
model clients.

## Executive finding

The checked-out repository is `xbustcodex/Prime-harness`, a Python control-room
application. It is not the PrimePi source tree described in the request. It has no
online model client, API request serializer, common online-provider dispatch function,
or persisted agent message/tool transcript to preprocess. The shipped
`ShellAgentProvider` executes a narrow local file-operation command; the reviewer is
deterministic. An uncommitted OMP adapter currently starts an external `omp` process
and sends OMP RPC prompts. OMP owns the model context and provider requests beyond
that process boundary.

Consequently, this checkout cannot prove the architectural invariant
“never dispatch an unsuppressed online request while the harness is active.” Adding a
compressor to the shell adapter, a reviewer stub, or the OMP prompt RPC would not
establish interception of every online inference. The safe next step is to build and
measure an isolated, provider-neutral prototype here, then integrate only after the
actual PrimePi online dispatch source and its structured request contract are
available. That prototype has now been implemented and measured; its synthetic
results are recorded in section K. No claim of provider-facing token savings or
online task correctness is made by this report.

## A. Current PrimePi / checked-out repository path

The repository inspected is Prime Harness, not PrimePi. The available path is:

```text
HTTP instruction
  -> src/prime_harness/api.py:create_app / instruction route
  -> HarnessService.send_instruction
  -> CodingAgentProvider.send_instruction
       -> ShellAgentProvider: constrained local file operation, no model inference
       -> OmpAgentProvider [uncommitted]: JSONL RPC prompt to external `omp`
            -> OMP-owned context, inference and provider dispatch (outside this repo)
  -> checkpoint and sanitized event in HarnessService
  -> SqliteStore: session/checkpoint/review/event metadata and evidence
  -> later review / continue request
```

Exact files and call sites:

- API entry points: `src/prime_harness/api.py:create_app`, `instruction` at line 100,
  and `continue_agent` at line 161.
- Provider protocol and local implementation: `src/prime_harness/agents.py:
  CodingAgentProvider` and `ShellAgentProvider.send_instruction` at line 106.
- Session orchestration and persistence: `src/prime_harness/services.py:
  HarnessService.send_instruction` at line 744 and `continue_agent` at line 847.
- Checkpoint/reviewer path: `HarnessService._create_checkpoint` and
  `get_review_context` / `get_transcript` at lines 348-459; reviewer contract and
  deterministic implementation are in `src/prime_harness/reviewers.py`.
- History: `HarnessService.get_history` and `compact_history` at lines 1190 and 1200.
  `compact_history` returns checkpoint/review/event identifiers and counts; it is not
  model-context compaction.
- Persistence: `src/prime_harness/stores.py`; SQLite has sessions, checkpoints,
  reviews, events, and approvals. It has no conversation-message or tool-result
  history table. Checkpoint evidence can hold a transcript field, but the shell
  adapter does not provide a real model conversation.
- OMP boundary in the current worktree: `src/prime_harness/omp.py:
  OmpAgentProvider.send_instruction` at line 393. It delegates `prompt` to another
  process; it does not construct or intercept that process's online provider request.

The earlier `ARCHITECTURE.md` describes future provider adapters and full-fidelity
history as design goals, not capabilities already implemented. No common online
provider dispatch, compaction subsystem, tool-output artifact store, or request-token
measurement exists in this checkout.

## B. Common hook point

**No hook point covering all online calls exists in this repository.** The required
integration seam in PrimePi is the common online-model request construction/dispatch
boundary, after PrimePi has assembled its structured messages, tools and provider
options but before serialization/network dispatch. It must be above individual
provider adapters and below agent/session context construction.

The hook must be an explicit contract in the common PrimePi path, not an OMP-specific
wrapper. If PrimePi delegates inference to OMP, an outer `prompt` rewrite is not
equivalent: OMP may add system prompts, conversation history, tools, and subsequent
model calls after the first prompt. In that deployment, a verified upstream request
middleware/hook or a PrimePi-owned provider integration is needed before the invariant
can be asserted. Do not proxy credentials or put them into URLs as a shortcut.

## C. Existing capabilities to reuse

- Lane/session identifiers, ownership checks, writer leases, state transitions, and
  checkpoint/review lifecycle can anchor isolation and audit for a later integration.
- Checkpoint evidence and event records can retain compact references and measured
  pipeline metadata, provided they never persist raw secrets or silently replace
  authoritative source artifacts.
- `compact_history` is a history index, not a compressor. Preserve its audit semantics;
  do not repurpose it as a lossy request transform.
- Keep complete artifacts outside active provider context and retrieve bounded,
  relevant slices when requested. This repository does not yet implement artifact
  storage/retrieval for agent tool output.
- Preserve the existing shell path, OMP RPC behavior, authorization, lease, approval,
  isolation, path protections, and reviewer semantics. No settings UI changes are
  relevant or proposed.

## D. Technology matrix

Hardware and latency depend on the actual selected checkpoint, quantization, batch,
input length, and runtime. None of these figures have been benchmarked on PrimePi.
“Windows/Linux: likely” means the Python stack is available in principle, not that
this project has validated every CUDA/kernel dependency on either OS. Open-source
code licenses do not automatically license separately distributed model weights,
datasets, or their use conditions.

| Candidate | Implementation, dependencies and resources | Compatibility, trade-offs and harness stage | Decision |
|---|---|---|---|
| **LLMLingua** ([Microsoft repo](https://github.com/microsoft/LLMLingua), [EMNLP 2023](https://aclanthology.org/2023.emnlp-main.825/)) | MIT repository code; Python/PyTorch/Transformers and a small causal LM (examples include GPT-2 small or LLaMA); CPU is possible but model scoring adds latency, GPU can accelerate; RAM/VRAM is model-dependent. Python is broadly available on Linux and Windows, but each model/kernel combination needs validation. Check model-weight licenses independently for commercial use. | Returns literal text that can be sent to black-box APIs; does not require the target provider to host the compressor. Token/sentence/context filtering and forced tokens/context are useful ideas. Added local model load and scoring latency; code/source regions and exact structures are not safe compression targets without workload tests. Candidate for designated old natural-language history, never schemas/current task by default. | **EXPERIMENT** |
| **LongLLMLingua** ([paper](https://aclanthology.org/2024.acl-long.91/), same repository) | Same Python/PyTorch/Transformers family and checkpoint-dependent CPU/RAM/GPU considerations as LLMLingua. MIT code; separate checkpoint license review. Linux/Windows support is inherited from the dependencies, not a guarantee for all acceleration paths. | Query-aware context selection/order seeks to improve relevance and lost-in-the-middle behavior; emits ordinary text, so the destination API may be black-box. It changes which information and order survive, so incorrect selection is a material coding risk; adds local scoring latency. Candidate for retrieval/selection on historical prose/artifact indexes. | **EXPERIMENT** |
| **LLMLingua-2** ([paper](https://aclanthology.org/2024.findings-acl.57/), [Microsoft repo](https://github.com/microsoft/LLMLingua)) | MIT repository; distilled token-classification approach with an encoder model, Python/PyTorch/Transformers. Smaller encoder inference is expected to be lighter than a causal-LM compressor; exact RAM/CPU/GPU requirements depend on the selected model. Windows/Linux require dependency-specific testing. Model/data licenses remain separate commercial checks. | Produces textual selections compatible with arbitrary text APIs. Microsoft reports faster processing than LLMLingua in its experiments; that is not a PrimePi workload result. Candidate for suppression of clearly compressible natural-language history, not exact code, tool schemas, paths or structured calls. | **EXPERIMENT** |
| **Selective Context** ([repo](https://github.com/liyucheng09/Selective_Context), [EMNLP 2023 paper](https://arxiv.org/abs/2310.06201)) | MIT repository. Python package uses spaCy and a GPT-2-family self-information model; English/Chinese spaCy models are separately downloaded. CPU-capable in principle, with model scoring/memory overhead; GPU optional. Windows/Linux Python is plausible, but package/model installation should be tested. Review separate model and dependency terms for commercial use. | Self-information scores filter lexical units; output is text and therefore usable by black-box APIs. Natural language is a better fit than source code, where rare identifiers can be valuable precisely because they are rare. Candidate as a suppression baseline before stronger compression. | **EXPERIMENT** |
| **CAPC (Cache-Aware Prompt Compression)** ([arXiv:2607.15516](https://arxiv.org/abs/2607.15516)) | July 2026 arXiv preprint. The reviewed reference is a paper, not a verified reusable package; no official implementation language, dependency, license, CPU/RAM/GPU profile, or cross-platform status was established. Commercial reuse of a paper's proposed method/code requires separate review. | Treats prompt compression and provider prefix-cache economics jointly; proposes query-agnostic compression, explicit cache controls, and a ratio bound preserving cache-tier usefulness. The paper reports experiments on specific providers/workloads; those results are not independently reproduced here. Cache controls are provider-specific and must stay in adapters. It is an economics/cache-aware assembly policy, not a generic compressor; caching can reduce provider processing/cost without shrinking bytes physically sent. | **EXPERIMENT** as a later cache-aware policy; do not integrate provider-specific heuristics into the generic core. |
| **MemGPT / virtual context management** ([paper](https://arxiv.org/abs/2310.08560), [current Letta Code](https://github.com/letta-ai/letta-code)) | The paper contributes a memory/control-flow architecture, not a drop-in compression library. Historical code used Python; current Letta Code is TypeScript/Node/Bun-oriented and its repository declares Apache-2.0. Resource requirements depend on its server and chosen model/provider; no runtime profile for a PrimePi integration was validated. | Strongly relevant pattern: durable external/archival memory distinct from a small active context, with explicit retrieval/eviction. Agent/tool-driven paging can require extra model turns and still needs safe retrieval policies. Black-box APIs can support the pattern through ordinary tool/request calls; no target model internals are required. Reuse the separation principle, not an agent runtime. | **ACCEPT** as an architectural principle; **REJECT** wholesale runtime adoption. |
| **AutoCompressors** ([Princeton repo](https://github.com/princeton-nlp/AutoCompressors), [EMNLP 2023 paper](https://aclanthology.org/2023.emnlp-main.232/)) | Python/PyTorch/Transformers; pretrained model code example loads a Llama-2 7B model in bfloat16 on CUDA and uses FlashAttention. High RAM/VRAM relative to text filters; CPU may be technically possible but not a practical assumption. Linux CUDA is the demonstrated path; Windows CUDA/FlashAttention compatibility is not established. No repository LICENSE file was found in the checked paths, so commercial reuse is not cleared. | Compresses context into learned soft-prompt vectors consumed by a compatible local model implementation. Those vectors are not ordinary prompt text that an arbitrary hosted API accepts; cannot meet provider-independent black-box compatibility. | **REJECT** for this objective. |
| **ICAE (In-Context Autoencoder)** ([repo](https://github.com/getao/icae), [ICLR 2024 paper](https://openreview.net/forum?id=uREj4ZuGJE)) | Repository is CC0-1.0; separately licensed Llama/Mistral weights and dependencies must still be checked. Python/PyTorch/Transformers; released checkpoints are based on 7B models, implying significant local memory/GPU requirements for practical inference. Windows/Linux depend on the chosen kernels; no PrimePi compatibility validation. | Uses model-specific learned memory tokens/embeddings and a compatible model decoder. Not a portable text transform consumable by arbitrary remote APIs. | **REJECT** for generic online provider dispatch. |
| **500xCompressor** ([ACL 2025 paper](https://aclanthology.org/2025.acl-long.1219/), [repo](https://github.com/ZongqianLi/500xCompressor)) | Repository describes its project content as CC BY 4.0; model files are not publicly available in the README. Python/model-training ecosystem; demonstrated base is Llama-3-8B-Instruct with LoRA, so substantial model/GPU resources should be expected. Windows/Linux need the exact training/inference environment; not validated. Check base weights and datasets separately for commercial use. | Uses special learned tokens/model parameters. This is not a standards-based text payload for arbitrary API endpoints. The project itself reports retained ability below the uncompressed baseline and the released model artifacts are unavailable, so claims cannot be tested here. | **REJECT** for the PrimePi black-box API path. |

### Provider context management and caching

Provider caching is complementary to suppression but does not satisfy “send small”:
cache hits can avoid repeated provider-side prefill/cost, but the client still submits
a request containing its prompt/context (or a provider-specific reference to
explicitly cached material). Measure transmitted request size separately from billed
cached tokens and latency.

- [OpenAI prompt caching](https://developers.openai.com/api/docs/guides/prompt-caching):
  automatic for supported models; exact reusable prefixes matter.
- [Anthropic prompt caching](https://platform.claude.com/docs/en/build-with-claude/prompt-caching):
  automatic or explicit `cache_control` breakpoints with provider/model-specific TTL
  and minimum-prefix constraints.
- [Gemini API context caching](https://ai.google.dev/gemini-api/docs/caching):
  implicit caching and explicit reusable contexts; supported models, minimum lengths,
  response usage fields and storage behavior are provider-specific.
- [Amazon Bedrock prompt caching](https://docs.aws.amazon.com/bedrock/latest/userguide/prompt-caching.html):
  implicit best-effort or explicit model/API-specific checkpoints; cache hits are not
  guaranteed.
- [OpenRouter prompt caching](https://openrouter.ai/docs/guides/best-practices/prompt-caching):
  availability and cache controls/routing vary by underlying provider/model.
- [Azure OpenAI prompt caching](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/prompt-caching):
  supported model/deployment behavior and billing vary.

The generic harness should only expose stable-prefix and changing-suffix structure.
Provider adapters own cache controls, token thresholds, TTLs, routing, and usage
interpretation. No cache is grounds to keep irrelevant active context.

## E. Proposed harness contract

Use PrimePi's actual language and request structures once available. For the Python
prototype in this checkout, a small typed `Protocol` contract is sufficient:

```python
class ContextModule(Protocol):
    @property
    def metadata(self) -> ModuleMetadata: ...
    def process(self, context: StructuredContext) -> ModuleResult: ...

class TokenHarness(Protocol):
    def prepare(self, request: ProviderRequest) -> PreparedRequest: ...
```

The structured request should preserve roles, ordered messages, text/multimodal block
types, tool calls/results, tool schemas, provider options, and stable IDs. The
prototype automatically protects all system, developer, and user-role messages;
callers mark additional exact content such as active code/error regions and
identifiers. Tool schemas and provider options are immutable across every stage.

Each module declares stable metadata (name/version/capabilities/priority/input
support/requirements) and returns a typed result with the new context, input/output
token counts when a provider-accurate tokenizer is available, protected IDs retained,
latency, warnings, and explicit failure. Token estimators must identify their
tokenizer/version; a character heuristic is a prototype-only estimate, never a
provider usage claim.

The pipeline validates after every transformation that protected items and required
message/tool structure remain. When active, a failure, malformed result, missing
estimator required by policy, or validation failure **blocks dispatch**. Do not
silently pass the original unsuppressed request. An explicit operator-approved
disable mode may be considered later; it must be observable and cannot be an implicit
fallback.

## F. Proposed pipeline

```text
Structured PrimePi request + local full-fidelity history/artifacts
  -> PROTECT: mark exact critical instructions, schemas, active code/errors
  -> DEDUPLICATE: remove only verifiable repeated content with identity/provenance
  -> RETRIEVE / SELECT: query-index local history and artifact slices for this task
  -> SUPPRESS: drop stale/redundant items only under explicit recency/relevance policy
  -> COMPRESS: apply interchangeable text compressors only to eligible spans
  -> ASSEMBLE: preserve roles, order, tool semantics, and optional stable prefix
  -> VALIDATE: check protected IDs, structural validity, budgets and measured output
  -> EXISTING COMMON ONLINE PROVIDER DISPATCH
```

Preserve full source artifacts locally; send a concise structured summary plus a
stable opaque artifact reference, then retrieve precise ranges on demand. References
are useful only if PrimePi can actually resolve them and if the model knows how to ask
for relevant ranges. Do not invent one universal “summarize logs” rule: retain exact
current failures, commands, source spans, and test counts; suppress old duplicates
only when the current request no longer needs them.

## G. Data flow

```text
PrimePi session and complete local history
  -> structured request + protected elements + artifact index
  -> Prime Token Harness (modules are replaceable)
  -> validated smaller request
  -> existing provider adapter / online provider
  -> structured response / tool calls
  -> PrimePi state and full local artifacts
  -> next request selects/retrieves only relevant context
  -> Prime Token Harness -> provider
```

The provider response and tool output must re-enter the local state with provenance;
they are not assumed to be transmitted in full on the next call.

## H. Smallest safe prototype

An isolated structured-request pipeline has been built in this repository; it does
not change `HarnessService`, `OmpAgentProvider`, API routes, settings, or provider
execution. Its tests prove:

1. Structured messages and tool schemas enter without flattening.
2. System/developer/user content and explicitly protected additional content remain
   byte-for-byte identical.
3. Exact duplicate historical blocks can be removed deterministically.
4. A replaceable conservative suppression/compression module can reduce designated
   repeated/noisy historical text while leaving the caller's full source input
   unchanged. The prototype does not add persistent artifact storage.
5. The assembled request remains structurally valid and preserves order/tool fields.
6. Modules can be substituted through the contract.
7. A fixture suite measures input/output size, ratio, reduction, and elapsed time.
8. Exceptions, malformed output, or lost protected IDs are visible and prevent
   `PreparedRequest` creation.

The prototype may use a clearly labelled deterministic heuristic and a rough
character-based token estimate to avoid adding model dependencies. It must not label
the result as real token counts or coding-task quality evidence. A later experiment
can compare LLMLingua-2/Selective Context against the same representative fixtures
and a real target tokenizer. The initial isolated files are expected to be
`src/prime_harness/token_harness.py` and `tests/test_token_harness.py`; add fixture
files only if inline fixture structures become unwieldy.

## I. Test plan and measurable acceptance

**Prototype gate (offline, deterministic):**

- fixtures cover short/long/very long sessions, repeated tool results, terminal logs,
  repeated source reads, active debugging, build/test/fix cycles, plans, multiple
  files, exact edit context, and structured tool calls;
- zero protected-ID/content loss and zero message/tool-schema mutation;
- duplicate/noise removals are explainable and source fixtures remain unchanged;
- processed request remains valid for every passing fixture;
- input/output character counts, clearly labelled token estimates, percent reduction,
  ratio, and pipeline wall time recorded per case; no fixed reduction target;
- plugin replacement changes only the selected stage; plugin exception/malformed
  output/protection loss blocks output rather than dispatching an unsuppressed request;
- no provider API request occurs in the prototype.

**PrimePi integration gate (not testable in this checkout):**

- instrument the single common online dispatch and assert every online call passes
  through `prepare`; no alternate provider/agent/retry path can dispatch raw requests;
- compare provider tokenizer counts and actual outbound serialized bytes/tokens against
  the unmodified request, with cache-hit tokens reported separately;
- test tool-call equivalence, code-edit/test correctness and protected-content
  retention on the listed coding-agent workloads against the same provider/model;
- measure preprocessing latency, end-to-end latency, successful turns, rate limits,
  usage/cost, and task correctness; report distributions, not a promised ratio;
- verify all full artifacts remain retrievable and that a later turn can retrieve
  required details on demand;
- run against actual supported providers and a real PrimePi build before claims of
  broad provider coverage.

## J. Risks and unresolved questions

1. **Source-tree mismatch:** obtain the actual PrimePi repository/revision and inspect
   its agent context assembly, compaction, online provider clients, serializers,
   tool schemas/results, and current tests before production integration.
2. **OMP ownership:** if PrimePi delegates to a separately running OMP, identify and
   verify an upstream request hook covering every generated inference, tool follow-up,
   retry and reviewer call; a first-prompt rewrite is insufficient.
3. **Input types:** image/audio/video/document blocks, provider-specific schemas,
   structured output constraints, and cached-content references may not be safely
   compressible as text. Define exact pass-through/protection behavior.
4. **Correctness and security:** selection/compression can drop rare identifiers,
   vulnerable code, negations, tool-call constraints, or prompt-injection warnings.
   Source code and safety instructions need conservative treatment and adversarial
   tests.
5. **Token measurement:** provider tokenization differs. Exact local estimates may
   be impossible for some providers; outbound usage fields may arrive only after
   dispatch.
6. **Cache economics:** cached prefixes may reduce marginal provider cost yet still
   be physically resubmitted. Cache thresholds, retention, pricing, region and
   data-retention policy change by provider/model and over time.
7. **Artifact retrieval:** stable local references need scoped authorization,
   lifecycle/retention, integrity, and a retrieval interface the model can use without
   turning large payloads into another repeated context channel.
8. **Fail-closed product behavior:** with the harness active, rejecting an inference
   on module failure is safer than sending raw context, but may interrupt work.
   Error UX, explicit disable policy, availability targets and latency budget require
   product decisions before production activation.
9. **Model/weight terms and resource footprint:** evaluate licenses, Windows/Linux
   packaging, CPU throughput and memory using the actual deployment environment
   before selecting learned compressors.
10. **Evidence quality:** published ratios and CAPC cache results use different
    workloads/models/providers. Replicate on PrimePi code tasks; do not translate them
    into project acceptance targets.

## Initial recommendation

1. **Reuse:** PrimePi's existing structured message/tool representation, lane/session
   authorization, durable local history/artifact store, existing compaction as a
   separate fallback, and common provider dispatch. In this checked-out harness,
   reuse the isolation/checkpoint concepts only; there is no online request structure
   to reuse.
2. **Adapt:** LLMLingua-2 or Selective Context as replaceable candidate stages for
   selected old natural-language spans; CAPC only as a later provider-aware cache
   policy. Benchmark against deterministic deduplication and retrieval first.
3. **Reject:** AutoCompressors, ICAE, and 500xCompressor as generic black-box API
   preprocessors because their learned representations require compatible model-side
   support; do not add a separate Tool Harness or adopt MemGPT/Letta wholesale.
4. **Build natively:** structured protection, provenance-aware deduplication,
   artifact indexing/retrieval, strict validation, real tokenizer accounting,
   common-dispatch interception, fail-closed activation, and workload/equivalence
   evaluation.
5. **Prototype delivered:** the offline structured contract and conservative
   interchangeable stages are in `src/prime_harness/token_harness.py`, with
   deterministic fixture tests in `tests/test_token_harness.py`; no network or
   production dispatch is involved.
6. **Files for that prototype:** those two files only, unless representative fixtures
   justify separate fixture assets. Do not modify API, OMP adapter, shell behavior,
   settings UI, or dependencies for the initial prototype.
7. **Why:** this tests the replaceable boundary and invariants without claiming to
   intercept an OMP-owned request or changing current agent behavior. Production
   integration waits for the actual PrimePi source tree and a verified universal
   online dispatch seam.

## References

- Microsoft LLMLingua project, papers and examples:
  https://github.com/microsoft/LLMLingua
- Selective Context implementation and license:
  https://github.com/liyucheng09/Selective_Context
- CAPC preprint:
  https://arxiv.org/abs/2607.15516
- MemGPT paper:
  https://arxiv.org/abs/2310.08560
- AutoCompressors implementation:
  https://github.com/princeton-nlp/AutoCompressors
- ICAE implementation:
  https://github.com/getao/icae
- 500xCompressor paper and implementation:
  https://aclanthology.org/2025.acl-long.1219/
  https://github.com/ZongqianLi/500xCompressor
- Provider caching references:
  https://developers.openai.com/api/docs/guides/prompt-caching
  https://platform.claude.com/docs/en/build-with-claude/prompt-caching
  https://ai.google.dev/gemini-api/docs/caching
  https://docs.aws.amazon.com/bedrock/latest/userguide/prompt-caching.html
  https://openrouter.ai/docs/guides/best-practices/prompt-caching
  https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/prompt-caching

## K. Prototype execution and results

Implemented `src/prime_harness/token_harness.py` and
`tests/test_token_harness.py`. The prototype accepts structured requests and keeps
provider/model, tools, parameters, roles, order, tool-call fields, and message content
separate. It protects system/developer/user roles automatically and permits explicit
protection of additional material. Its replaceable stages perform exact duplicate
removal only for messages marked `kind="history"` and repeated-line suppression only
for caller-marked compressible tool-result text. It checks removal provenance,
protected content/order, and immutable tools/options; plugin, estimator, schema, and
protection failures block creation of a prepared request with sanitized errors.

The original input object is not mutated, but the prototype does not persist artifacts
or implement retrieval. Its estimate is serialized JSON character count divided by
four, not a provider tokenizer. The following measurements are synthetic and include
only trivial repeated lines and a duplicated history message; they are not coding-task
benchmarks, quality results, provider-facing token savings, or a fixed compression
target:

| Fixture | Serialized bytes | Heuristic token estimate | Byte reduction | Approx. local pipeline time |
|---|---:|---:|---:|---:|
| Short session | 879 → 808 | 220 → 202 | 8.1% | 1.354 ms |
| Long session | 2,125 → 892 | 532 → 223 | 58.0% | 1.358 ms |
| Repeated tool output | 1,501 → 908 | 376 → 227 | 39.5% | 1.247 ms |
| Large terminal log | 4,057 → 904 | 1,015 → 226 | 77.7% | 1.370 ms |
| Repeated source reads | 1,503 → 910 | 376 → 228 | 39.5% | 1.251 ms |
| Multiple files | 1,105 → 895 | 277 → 224 | 19.0% | 1.202 ms |
| Active debugging | 1,813 → 900 | 454 → 225 | 50.4% | 1.228 ms |
| Build/test/fix cycle | 2,461 → 908 | 616 → 227 | 63.1% | 1.243 ms |
| Repository plan | 1,331 → 898 | 333 → 225 | 32.5% | 1.222 ms |
| Exact code edit | 1,171 → 897 | 293 → 225 | 23.4% | 1.202 ms |
| Structured tool call | 1,245 → 908 | 312 → 227 | 27.1% | 1.205 ms |

All **20 prototype tests passed**. Targeted Ruff and mypy checks passed for the two
prototype Python files, as did Python compilation and trailing-whitespace checks.
There are no third-party compression dependencies, hardware requirements, network
calls, or production dispatch changes in this prototype.

**What worked:** the typed boundary composes interchangeable modules; user/system
content and tool schemas survive; exact historical duplicates and explicitly
compressible repeated lines are removed; the original input remains unchanged;
metrics expose per-stage latency and separately label their tokenizer estimate;
invalid module/estimator behavior fails closed.

**What did not get proven:** semantic relevance/retrieval, compression quality,
provider token counts, real transmitted payload interception, cache behavior, online
task correctness, and cross-platform learned-compressor performance. The repeated
line rule is intentionally narrow and cannot replace summarization or relevance
selection. No hardware or online provider evaluation occurred.

## Final recommendation

1. Reuse PrimePi's native structured messages/tool schemas, durable artifact storage,
   authorization and common online dispatch when those source files are available.
   This checkout contributes isolation/audit patterns but no provider request model.
2. Experiment next with LLMLingua-2 and Selective Context on real coding-agent
   histories only after a real PrimePi request fixture and provider tokenizer exist;
   compare them against the deterministic prototype stages.
3. Do not integrate AutoCompressors, ICAE, or 500xCompressor into arbitrary online
   provider payloads; their learned representations require compatible model support.
   Use MemGPT/Letta only as a reference for local archival/active-context separation.
4. PrimePi must implement native protection, provenance-aware deduplication,
   retrieval, artifact references, validation, provider-accurate token accounting,
   and an interception point covering every online dispatch/retry/tool follow-up.
5. The smallest prototype has been completed. The next engineering step is to inspect
   the actual PrimePi source tree and locate its common online request builder; the
   online integration should not begin until that seam and structured contract are
   verified.
6. Prototype changes are limited to `src/prime_harness/token_harness.py` and
   `tests/test_token_harness.py`; the report is `PRIME_TOKEN_HARNESS_ARCHITECTURE.md`.
   No API, OMP, settings UI, authorization, or production inference files were
   changed for this work.
