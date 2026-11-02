# tectonic_runtime: shared core modules

The Builder Agent and every worker agent it generates share one runtime. It runs ten core
functional modules from `modules/` in-process, behind one standardized API, and streams their
activity to the Command Center's Agent Office view.

| Module | Operations | Backed by |
| --- | --- | --- |
| `guardrails` | `check_input`, `check_output`, `redact_pii`, `configure_profile`, `stats` | `modules/guardrails` PolicyEngine |
| `intent_detection` | `classify`, `define_taxonomy`, `get_taxonomy` | `modules/intent-detection` ClassificationService |
| `short_term_memory` | `append`, `get_context`, `clear` | `modules/short-term-memory` BufferManager |
| `long_term_memory` | `remember`, `recall`, `share`, `forget`, `stats` | `modules/long-term-memory` MemoryService |
| `tool_orchestration` | `call_tool`, `list_tools`, `health` (+ Python `register_callable`) | `modules/tool-orchestration` ToolOrchestrationService |
| `agentic_rag` | `ask`, `retrieve`, `ingest`, `documents_list`, `delete_document`, `recent` | `modules/agentic-rag` RAGService + RetrievalLoop |
| `vector_db` | `search`, `index`, `delete`, `stats`, `migrate_embeddings`* | `modules/vector-db` VectorService + MigrationManager (Qdrant) |
| `finops` | `report`, `check`, `recommendations`, `rates`, `set_budget`*, `set_rate`*, `optimise`* | `modules/finops` budgets, aggregation, forecasting, optimiser |
| `promptops` | `resolve`, `record_outcome`, `list_prompts`, `versions`, `get_version`, `ab_status`, `register`*, `promote`*, `rollback`*, `start_ab_test`*, `conclude_ab_test`*, `check_drift`*, `propose_improvement`* | `modules/promptops` registry, A/B (z-test), drift, reflection |
| `auditability` | `query`, `ask`, `verify`, `stats`, `export`* | `modules/auditability` SHA-256 hash chain, NL query, audit packs |

`*` = admin-only (`@operation(..., permission="admin")`).

## Permissions and inheritance

Every call carries a `CallContext(role, team)`. Roles come from the ADK app the agent runs in (`identity.py`):

| App | Role | Team |
| --- | --- | --- |
| `agent_builder` (Builder + its specialists) | `admin` | `agent_builder` |
| `generated_agents.<project>.<project>` (every agent of a generated project) | `worker` | `<project>` |
| Command Center UI (`POST /modules/{module}/{op}`) | `operator` (admin-equivalent, attributed separately) | — |

Workers therefore inherit their project's scope with no configuration: its FinOps budget, its knowledge namespace
(plus `shared`), `shared:<team>` memory and audit visibility. Admin-only calls by workers are refused and recorded
as `MODULE_ACCESS_DENIED`.

## Models

`models.py` is the single Gemini integration (all overridable by env):

| Role | Default | Used for |
| --- | --- | --- |
| reasoning | `GEMINI_MODEL` (gemini-3.6-flash) | grounded RAG answers, prompt reflection |
| utility | `TECTONIC_UTILITY_MODEL` (gemini-3.5-flash-lite) | RAG routing, groundedness critique, query reformulation, audit NL → filter |
| embeddings | `TECTONIC_EMBEDDING_MODEL` (gemini-embedding-2, `TECTONIC_EMBEDDING_DIM`=768) | Vector DB (RETRIEVAL_DOCUMENT / RETRIEVAL_QUERY task types) |

Every model call is metered into FinOps. With no key or `TECTONIC_OFFLINE=1`, deterministic fallbacks keep everything working (tests run this way).

## Platform wiring

- **FinOps:** the telemetry plugin meters every agent LLM response and blocks calls when a team's `block` budget is exhausted.
- **PromptOps:** the Builder's own system prompt is the managed prompt `agent_builder.root`. It follows the code default until an admin promotes a different version, and runs report outcomes to it.
- **Auditability:** every agent, tool, A2A, module and run event is PII-redacted and appended to a JSONL hash chain. Streamed thinking becomes one `AGENT_REASONING` entry per turn.

## State and configuration

State lives under `generated_agents/_platform/` (git-ignored). Each store can be overridden: `TECTONIC_VECTOR_PATH`
(or `QDRANT_URL` / `QDRANT_API_KEY` for a Qdrant server), `TECTONIC_RAG_DOCS_PATH`, `TECTONIC_FINOPS_PATH`,
`TECTONIC_PROMPTOPS_PATH`, `TECTONIC_AUDIT_PATH`. The value `:memory:` disables persistence.

The modules' `core/` code runs unmodified. `adapters.py` replaces the ports that only had test stubs
(LLM gateway, vector DB, MCP servers, Redis, Postgres) with real zero-LLM, in-process
implementations. Any adapter can later be swapped for a network client without changing agent code.

## Using it

**Python** (any agent, script or tool):

```python
from tectonic_runtime import get_runtime, CallContext

rt = get_runtime()
res = await rt.invoke("guardrails", "check_input", {"text": msg}, CallContext(agent_id="billing_worker"))
if res.ok and res.data["allowed"]:
    safe_text = res.data["text"]
```

Every call returns a `ModuleResult` (`ok, module, operation, call_id, duration_ms, data | error`).
Operations never raise to the caller.

**ADK agents:**

```python
from tectonic_runtime.adk_tools import worker_tools, CORE_MODULE_INSTRUCTIONS
LlmAgent(..., instruction=MY_PROMPT + CORE_MODULE_INSTRUCTIONS, tools=[*my_tools, *worker_tools(team="my_project")])
```

- **Builder:** `agent_builder/agent.py` mounts `builder_tools()`, i.e. durable memory plus
  `core_share_with_project`, which hands requirements to a generated project's workers.
- **Workers:** the code generator emits `shared_libraries/core_modules.py` in every new project and
  mounts the core tools on each `LlmAgent`. Run standalone (plain `adk run`), the import falls back
  to no core tools.
- **Team memory:** memory scopes are private per agent. `shared:<team>` scopes are the hand-off
  channel between agents, enforced by the long-term-memory visibility policy.

## Office events

`events.bus` emits `MODULE_REGISTERED`, `MODULE_STATE` (offline → booting → idle/busy/error),
`MODULE_CALL_START` and `MODULE_CALL_END`. `server.py` forwards them to `/ws/telemetry`.
`POST /modules/simulate` runs a real shift: the Builder spins up four workers who use every module
concurrently, including retries on a flaky tool and a blocked jailbreak. `GET /modules` lists the
registry.

## Adding a module

1. Subclass `BaseModule` in `services/` and build its core in `setup()`.
2. Mark public methods `@operation("…")`, with signature `async def op(self, ctx, **params)`.
3. Add the module's folder to `_paths.MODULE_PACKAGES` and the class to `runtime.MODULE_CLASSES`.

Tests: `python -m pytest` from the project root.
