# Decisions (ADR-lite)

| # | Decision | Why | Consequence |
|---|---|---|---|
| 1 | Foundry IQ knowledge bases via MCP (`knowledge_base_retrieve`) instead of the classic Azure AI Search tool | Requested for the POC; it is Microsoft's strategic path (multi-source, permission-aware, query planning). | Preview APIs (`2026-08-01-preview`, `azure-search-documents 12.1.0b2`); needs a project managed identity + `Search Index Data Reader`; citations point at the MCP endpoint, so the API adds a direct `retrieve` for real URLs. |
| 2 | One index, one knowledge source + knowledge base per skill, scoped with `base_filter` | Free tier allows 3 indexes; a shared index keeps ingestion simple and lets `general` search everything. | Per-skill scoping depends on `base_filter` (preview). Fallback: one index per category on Basic tier. |
| 3 | One Foundry prompt agent per skill, instructions = `_base` + `SKILL.md` | Agents are immutable/versioned in Foundry, visible in the portal, and map 1:1 to skill folders. | Skill edits create new agent versions (hash-guarded); `--keep N` prunes old ones. |
| 4 | Router is a Foundry agent with strict JSON output (+ keyword fallback) | Keeps every prompt versioned in Foundry; cheap (`gpt-4.1-mini`). | Rebuilt on every sync because the enum lists skill ids. |
| 5 | Client-side embeddings at ingest, vectorizer on the index for query time | Free tier has no managed identity for indexer skillsets; explicit embedding is transparent and incremental. | Ingest needs the Azure OpenAI key; vectorizer is key-based too (rotate keys via `.env` + `setup index`). |
| 6 | Free tier, `stored=false` vectors, promotions excluded by default | Cost 0 for search; 617 chunks x 3072 dims fits in 50 MB. | No SLA; semantic ranker and agentic retrieval are available on Free only in some regions (East US 2, South Central US, North Central US qualify). |
| 7 | KB `retrieval_reasoning_effort` = minimal by default | Works without an LLM inside the knowledge base (no extra RBAC on the Free tier); the skill agent does the reasoning. | `KB_REASONING_EFFORT=low` enables query planning inside the KB using the Azure OpenAI key. |
| 8 | Native Foundry Skills registration is optional (`--register-native`) | Beta API; agents already consume SKILL.md through instructions. | Portal visibility only. |
| 9 | FastAPI + vanilla HTML page | Reusable endpoints for the real app; no front-end build step. | No streaming in the POC (responses are returned whole). |
| 10 | Lazy knowledge-base provisioning: a skill gets its own knowledge source/base only when its category has documents; a skill without documents gets **no retrieval tool** and answers that its knowledge base is empty | Free tier allows only 3 knowledge sources and 3 knowledge bases; sharing the unfiltered `general` base leaked credit-card facts into debit-card answers, which makes testing "only what I uploaded" impossible. | `skills sync` creates/deletes KS/KB/connections as documents appear or disappear; `general` keeps the cross-category base. |
| 11 | Suggested follow-ups are a static `suggestions:` list per skill (rotated, minus what was already asked) | No extra model call per turn; testers edit them in Studio; not part of the agent hash, so editing them does not create agent versions. | Less contextual than model-generated suggestions; upgrade path: ask the skill agent for a JSON tail. |
| 12 | Sessions persisted in SQLite (`.state/bankrag.db`: `sessions` + `turns` rows with skill, language, tokens, latency, retrieved chunks), one Foundry conversation per session shared by all skill agents | Follow-ups survive restarts; the Foundry conversation keeps server-side context so a pronoun-free follow-up still resolves. | Verified: after a server restart, "แล้วค่าธรรมเนียมรายปีล่ะ" was answered for the three cards named in the previous turn. |
| 13 | Strict grounding rule: no facts from memory, fixed "not in the knowledge base" sentence | Testers must be able to tell what came from uploaded documents; there is no web search anywhere in the pipeline (knowledge bases have one search-index source each, agents have one MCP tool). | Some answers become shorter; the model may still paraphrase. |
| 14 | Per-turn trace (timings, router/agent token usage, retrieval calls/chunks/tokens, reasoning summaries) stored with the turn and shown in the app's side panel | Testers and the real-app design need cost and latency evidence per question. | Adds tiktoken counting of the MCP output; no extra API calls. |
| 15 | Streaming via SSE (`POST /chat/stream`) with the Sources retrieve running in parallel; non-streaming `/chat` kept for evals | Perceived latency; the direct retrieve no longer adds ~2 s | First token still waits for routing + the agent's own retrieval (~10 s) |
| 16 | Rotate the Foundry conversation every 6 answers, seeding a recap | Conversations keep every tool output; turn 18 carried 152k input tokens | Very long follow-up chains lose exact wording of old turns |
| 17 | Studio auth = login page + cookie (basic auth still accepted); tester name cookie attributes feedback | Browser basic-auth prompts are awkward and cannot carry a tester name | Cookie lasts 12 h; single shared password |
| 18 | Skills without documents get no retrieval tool; `general` keeps the unfiltered base; lint warns on keyword overlap / missing bilingual suggestions / undeployed model | Keeps answers honest and routing unambiguous | Lint is heuristic |
| 19 | Model comparison uses temporary agents `bank-<skill>-cmp-<model>` deleted after the run | Foundry agents pin one model per version; no per-request model override | A failed run may leave a temp agent; `skills sync --prune` does not touch them (tag `bankrag-compare`) |
| 20 | Azure Container Apps (consumption) + Azure Files for files; SQLite on local disk with minute backups to the share | Cheapest always-on option with persistence; SQLite cannot lock reliably over SMB (WAL and rollback modes both failed with 'database is locked') | Up to one minute of session history can be lost on a crash; single replica only |
| 21 | Entra ID sign-in (Container Apps built-in auth) in front of the app; Studio password kept as a second gate | Public URL must not expose the assistant or Studio | Only users of the personal tenant can open it; add guests or switch tenant for broader testing |

## 22. Customer-facing tone instead of "not in the knowledge base"
The base rules (`skills/_base/SKILL.md`) now carry a Tone section: the assistant speaks like bank staff, never mentions
documents, sources, knowledge base or tools, phrases gaps as "I don't have the details on that yet" plus a next step,
uses one consistent Thai voice (ค่ะ/คะ), and does not fill gaps with generic explanations. Grounding stays mandatory;
only the wording the customer sees changed. Product skills were rephrased to match.

## 23. Skills are also published to the Foundry skill registry
Every SKILL.md is published on sync as a versioned Foundry Skill (`bankrag-<id>`, composed base + skill
instructions) and attached to the toolbox `bankrag-skills`. Foundry becomes the shared, versioned store: the skills
are visible in the portal and VS Code, loadable by any MCP client, and a skill authored in Foundry can be imported
into the app (Skills > Foundry skill registry > Import). The prompt agents still get their instructions from the
app's SKILL.md at sync time, because prompt agents cannot yet consume toolbox skills directly (that is an Agent
Framework / hosted-agent feature).

## 24. Handoff over A2A as an alternative to the app-side router
Classic "connected agents" no longer exist in Foundry Agent Service; the replacement is A2A. Sync exposes each skill
agent as an A2A endpoint (agent card from the skill metadata), creates a RemoteA2A connection per agent
(project managed identity, needs the Foundry Agent Consumer role: infra/04-a2a-role.sh) and maintains the
`bank-concierge` agent with one A2A tool per specialist. `ORCHESTRATION_MODE=a2a` sends chat to the concierge, which
picks and calls the specialist inside Foundry; `router` (default) keeps the one-hop app router. Trade-off measured on
the POC: the handoff is native and visible in Foundry, but adds a second model hop (about twice the latency), the
specialist's tokens are not reported in the concierge response, and A2A is preview, text-only and non-streaming.

