# Findings for the real knowledge system

Observed on 2026-09-06 with the personal tenant (Foundry `my-model-hub/firstProject`, Free-tier Azure AI Search `bankrag-search` in South Central US, 617 chunks from 53 credit-card documents).

## 1. What worked as designed

- **Skill folder -> Foundry agent** is a clean 1:1 mapping. `bankrag skills sync` created 5 skill agents + the router in one run; re-running reports `unchanged` for every agent (hash stored in agent version metadata). Editing a `SKILL.md` produces exactly one new version.
- **Router as a Foundry agent with strict JSON output**: 28/28 on the routing set (Thai + English, follow-ups not yet in the set). Confidence is well calibrated enough for the "stay on previous skill below 0.5" rule.
- **Foundry IQ over MCP**: the skill agent calls `knowledge_base_retrieve` with 2 query variants it generates itself (Thai + English), gets 6 documents, and writes grounded answers with concrete figures (e.g. Infinite lounge: 4 visits/year, 1,300 THB cap; Visa Platinum annual fee 3,000 THB).
- **Incremental ingest**: content-hash manifest; second run uploads 0; removing files deletes their chunks.
- **Lazy knowledge-base provisioning**: skills without documents get no retrieval tool and say so; adding documents to a category and syncing creates `ks-<id>`/`kb-<id>` plus the agent tool (verified with a temporary `mortgage` skill created in Studio: upload -> ingest -> sync -> retrieval -> delete -> prune), and removing them reverts.
- **Mobile-app UI + sessions**: the prototype's Conversation screen was reproduced in plain HTML/CSS (BBL Sans, tokens, phone frame); sessions are JSON files and follow-ups survive a server restart because the Foundry conversation id is reused.
- **Studio**: testers edit `SKILL.md` in the browser (form + markdown), save & sync shows the new agent version, upload documents, run ingest with a live log, and test retrieval. Protected with a shared password.
- **No web search**: verified that knowledge bases contain only search-index sources and agents only the `knowledge_base_retrieve` MCP tool. Answers that looked "too knowledgeable" came from the model's own memory (e.g. hotline numbers) or, before decision 10, from credit-card documents leaking through the shared base; both are now blocked by the strict grounding rule.

## 2. Foundry IQ vs classic AI Search tool

| aspect | Foundry IQ (used) | classic `AzureAISearchTool` |
|---|---|---|
| per-skill scoping | `base_filter` on the knowledge source (preview API) | `filter` on the tool (GA) |
| query planning | the agent generates query variants; the KB can add LLM planning (`low`/`medium`, needs an LLM + on Free tier a key) | single query from the model |
| citations | `url_citation` pointing at the **search service document URL**; needs a lookup to show the original URL (done in `chat.resolve_citations`) | `url_citation` uses the index's URL field directly |
| setup | ARM `RemoteTool` connection per KB + project managed identity + `Search Index Data Reader` | project connection to the search service (key or identity) |
| quotas | Free tier: 3 knowledge sources / 3 knowledge bases | none beyond indexes |

Recommendation: keep Foundry IQ for the real system (shared, permission-aware knowledge across agents; Blob/SharePoint sources with managed indexing), but budget a Basic+ tier and managed identities. Keep the classic tool as a documented fallback for simple single-index agents.

## 3. Thai retrieval quality

- `th.microsoft` analyzer + hybrid (vector + keyword) + semantic ranker works well on product pages and brochures; the top reference is usually the right document. The retrieval scores in the Sources panel (reranker) are a useful signal for "no answer" thresholds.
- Chunking by headings with a breadcrumb prefix keeps context (card name + section) inside each chunk; 450-token targets produced 617 chunks for 53 documents.
- Crawled pages carry site chrome (help tools, cookie banner, PDPA notice); cutting at the first chrome heading removed ~80% of noise. Product hero text is duplicated on the page and needed paragraph-level de-duplication.
- Open questions to measure next: numbers split across table rows in PDF text (the crawler stores Azure Document Intelligence output; consider keeping table structure), and promotion pages (1,605 files, excluded by default) which would drown product pages without a `doc_type` boost/filter.

## 3b. Cost and latency per turn (measured with the trace panel)

One credit-card question (Infinite lounge access), gpt-4.1-mini, minimal-effort knowledge base:

| phase | time | tokens |
|---|---|---|
| router agent | ~4.3 s | 1,317 in (1,152 cached) / 40 out |
| skill agent incl. `knowledge_base_retrieve` | ~5.8 s | 17,700 in / 205 out |
| direct retrieve for the Sources panel | ~2.8 s | - |
| total | ~13 s | ~19k in / ~245 out |

At list prices for gpt-4.1-mini this is about **$0.007-0.008 per answer** (agent ~$0.0074, router ~$0.0003), i.e. roughly $7-8 per 1,000 questions; the retrieval context dominates the cost. The MCP tool returned 6 chunks = ~24.5k characters = ~16.4k tokens, because the knowledge base returns Thai text JSON-escaped (`\u0E1A...`), which costs roughly 6 tokens per Thai character instead of ~1. For the real system: reduce `top_k`/`max_output_documents`, shorten chunks, and check whether a newer API version returns raw UTF-8; the Sources call can be skipped in production (it is a debugging aid). The router and agent calls are sequential; running the direct retrieve in parallel with the agent call would save ~3 s.

### Retrieval context cannot be trimmed from the client (measured)

- Each `knowledge_base_retrieve` response is capped by the service at roughly 24.5k characters (~16.4k tokens once Thai is JSON-escaped); `top_k` 6 → 4 returned 4 chunks but the same 16.4k tokens, so the cap, not the chunk count, sets the cost.
- `retrieveDefaults.maxOutputSizeInTokens` counts service-side tokens on unescaped text: a 6,000-token cap returned 8 chunks / ~33k escaped tokens (4x the cost). Leave it off.
- Reasoning models call the tool more than once: gpt-5.6-luna made 2 retrieval calls on a simple fee question (~30k context tokens, 664 output tokens incl. 332 reasoning) and cost ~$0.045 vs ~$0.008 for gpt-4.1-mini. For the real app, keep a small fast model on the skill agents and reserve large models for a review step.
- **Conversation growth is the biggest cost driver**: Foundry conversations keep every tool output, so by turn 18 one answer carried ~152k input tokens ($0.19 on gpt-5.6-luna) even though its own retrieval was 16k. The POC now rotates the Foundry conversation every 6 answers, seeding the new one with a 3-question recap; the real app should do the same or summarise server-side.
- Streaming helps perceived latency only after retrieval: first token arrived at ~10 s (route 4 s + retrieval); the router is the cheapest place to save time (cache or a rules-first pass).

### Built-in crawler (no external service)

`bankrag` now crawls bangkokbank.com itself: `curl_cffi` with Chrome impersonation gets past the Akamai front (plain `requests` is blocked), BeautifulSoup + markdownify extract the `<main>` content, links are followed only under the chosen prefix, linked PDFs are downloaded and text-extracted with PyMuPDF, and a per-category manifest skips unchanged pages. Measured: 10-12 pages + 3-4 PDFs per section in about a minute each; Debit (Be1st), Bancassurance and Wealth/Save-and-Invest sections were seeded this way (603 new chunks). Image-only PDFs (e.g. the OIC guide) are kept but not ingested.

### Free-tier quota shapes the topology

With 3 knowledge sources per Free service, only `general`, `credit-card` and one more category can have dedicated knowledge bases. Extra categories with documents fall back to the shared unfiltered base with a scoping note in the agent instructions. For the real system, a Basic tier (or one knowledge base with `base_filter` per category on a higher tier) removes this limit.

### Evals and model comparison (from Studio)

- Routing eval (28 Thai/English questions): 28/28, $0.0067 total, ~2.4 s per routing call on gpt-4.1-mini.
- Comparison on the credit-card skill, 2 questions: gpt-4.1-mini $0.0130 / avg 6.6 s vs gpt-5.4-mini $0.0082 / avg 5.6 s. gpt-5.4-mini was cheaper and faster here.
- **Answer consistency is the real problem, not the model**: the same lounge-access question produced "1 per trip", "2 per year international" and "4 per year" across runs and models, because different retrieval calls surface different brochure chunks (several cards and years are described in the same documents). For the real app: (1) chunk product documents so that each chunk carries the card name and validity year, (2) add a `product_name` filter hint or a two-step retrieve (identify the card, then retrieve for that card only), (3) keep a grounded-answer eval with exact expected figures and run it after every skill or document change.

## 4. Skill authoring experience

- Frontmatter (`description`, `keywords`, `product_category`, `top_k`, optional `filter`) is enough for routing and scoping; instructions stay free-form markdown. `_base/SKILL.md` carries compliance and citation rules so product skills stay short.
- Native Foundry Skills (`beta.skills`) exist but are only for portal visibility here; the agent consumes the SKILL.md through its instructions. Revisit when Toolboxes + `ToolSearchToolboxTool` are GA, since they would let one agent discover skills dynamically.
- The answer text still contains Foundry citation markers like `【4:0†source】`; strip or render them in the real UI.

## 5. Gaps before production

- Auth: everything key-based on the Free tier (index vectorizer, ingestion, KB retrieve). Move to managed identities (search -> Azure OpenAI, app -> search) on Basic+.
- Free tier limits: 3 knowledge sources/bases, 50 MB (this POC uses ~11 MB storage + ~23 MB vector index for 617 chunks at 3072 dims). Consider `text-embedding-3-large` with 1024 dims + scalar compression, or Basic tier.
- East US 2 had no search capacity; the service is in South Central US while Foundry is in East US 2 (cross-region latency is acceptable here, not for production).
- Preview APIs: `2026-08-01-preview` search API, `azure-search-documents 12.1.0b2`, Foundry connections `2025-10-01-preview`. Pin and re-test on GA.
- No streaming, no auth on the FastAPI app, in-memory sessions, single conversation shared across skill agents (worked in tests, but verify long multi-topic sessions).
- Evaluation: extend `evals/` with follow-up turns, negative cases (no answer in KB), and answer-correctness grading.

## 6. Recommended target architecture

1. Documents in Blob Storage per product family -> Azure AI Search knowledge sources with managed indexing (chunking + vectorization in the service), Basic or Standard tier, managed identity.
2. One knowledge base per product family plus a global one; `base_filter`/ACLs for scoping; query planning enabled (`low`) with a small model.
3. Skill repository (git) of `SKILL.md` folders -> CI job running `bankrag skills sync` (or its equivalent) to version Foundry agents; router agent regenerated from the catalog.
4. Application layer: route -> agent -> citation resolution -> answer; log tool calls and retrieval references for evaluation.
