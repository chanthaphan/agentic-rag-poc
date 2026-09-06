# agentic-rag-poc: bank product recommendation agents

Proof of concept for Bangkok Bank product Q&A / recommendation built on **Microsoft Foundry agents** grounded in an **Azure AI Search** knowledge base through **Foundry IQ** (knowledge bases exposed to agents over MCP). Two things are meant to be easy:

- **Knowledge base**: drop `.md` / `.pdf` files into `knowledge/<product-category>/` and run `bankrag ingest`.
- **Skills**: one folder per product family in `skills/<id>/SKILL.md` (frontmatter + instructions). `bankrag skills sync` turns each skill into a Foundry agent wired to its own knowledge base; the `bank-router` agent picks the skill for each user message.

Read [docs/architecture.md](docs/architecture.md), [docs/howto-add-skill.md](docs/howto-add-skill.md), [docs/decisions.md](docs/decisions.md), and (after the end-to-end run) [docs/findings-for-real-app.md](docs/findings-for-real-app.md).

## Two front ends

- **/** – customer app: the Bangkok Bank mobile prototype's Conversation screen inside an iPhone frame (BBL Sans, glass background, suggestion chips, typing dots, bottom tab bar). Conversations are persisted in SQLite (`.state/bankrag.db`, tables `sessions` and `turns` with per-answer skill, language, tokens, latency and retrieved-chunk counts) so follow-up questions keep their Foundry conversation and previous skill even after a restart; `GET /sessions/stats` aggregates usage and cost; model prices live in `pricing.yaml` (editable in Studio → Settings & usage) and every answer carries a `trace` with timings, token usage, retrieval stats and USD cost; the history button lists past chats. A "Behind the scenes" panel beside the phone shows routing, detected language, agent, tool calls and sources per turn. The language of each message (Thai or English) is detected per turn; the answer and the suggested follow-ups follow it. Answers stream token by token and render as markdown (bold, lists, tables). Foundry conversations are rotated every 6 answers (with a recap) to keep input tokens bounded.
- **/studio** – tester workbench (login page, `STUDIO_PASSWORD`): skill editor with lint, preview, version diffs and a streamed playground; knowledge files with PDF status, chunk browser, index search, a built-in site crawler and URL import; eval runner and model comparison; conversation review with ratings and CSV export; base rules, runtime settings and bundle export/import. `/legacy` keeps the original debug page.

## Layout

```
skills/            _base (shared rules) + credit-card, debit-card, insurance, wealth, general
knowledge/         credit-card/ seeded from the bblwebsite_crawler (19 products, page + PDF texts)
src/bankrag/       config, skills, search_index, ingest/, knowledge_base, connections, foundry_sync, router, chat, api, cli
web/               mobile.* (customer app), studio.* (tester page), index.html (legacy debug), fonts/
infra/             az CLI scripts: login, create search service, project identity + roles, write .env
evals/             routing and answer question sets
```

## Setup (personal tenant, Free-tier search)

```bash
uv sync --extra dev                # python 3.12, pins azure-ai-projects 2.6.0 / azure-search-documents 12.1.0b2
./infra/00-login.sh                # az login --tenant <personal tenant>, interactive MFA
./infra/01-search-create.sh        # creates the Free search service; prints SEARCH_SERVICE_NAME=...
export SEARCH_SERVICE_NAME=<name>
./infra/02-project-identity.sh     # project managed identity + Search Index Data Reader / Search Service Contributor
./infra/03-env.sh                  # writes endpoints and keys into .env
uv run bankrag setup index         # index bank-products (Thai analyzer, vectors, semantic config, vectorizer)
uv run bankrag seed                # knowledge/credit-card from the crawler output (already committed)
uv run bankrag ingest              # chunk + embed + upload (incremental on re-runs)
uv run bankrag setup kb            # knowledge sources + knowledge bases per skill
uv run bankrag setup connections   # RemoteTool project connections (project identity -> KB MCP)
uv run bankrag skills sync         # one agent per skill + router; idempotent
uv run bankrag chat "บัตรอินฟินิทเข้าเลานจ์ได้กี่ครั้ง" --debug
uv run bankrag serve               # http://localhost:8010
```

`bankrag setup all` runs index, kb, connections and sync in one go. On the Free tier only 3 knowledge sources/bases are allowed, so skills whose category has no documents yet share `kb-general` until you add documents and sync again. `bankrag skills list` shows whether each Foundry agent is in sync with its folder.

## Everyday commands

| task | command |
|---|---|
| validate skills | `uv run bankrag skills validate` |
| sync skills to Foundry (only changed ones get a new version) | `uv run bankrag skills sync [--only id] [--prune] [--keep 3] [--register-native]` |
| install a skill zip | `uv run bankrag skills install my-skill.zip` |
| ingest documents | `uv run bankrag ingest [--category credit-card] [--full] [--dry-run]` |
| query a knowledge base directly | `uv run bankrag retrieve "..." --skill credit-card` |
| routing accuracy | `uv run bankrag eval routing` |
| grounded-answer checks | `uv run bankrag eval rag` |
| tests | `uv run pytest` |

## API

`POST /chat` (session_id keeps context), `GET /sessions`, `GET|DELETE /sessions/{id}`, `GET /app/config`, `GET /skills`, `PUT /skills/{id}`*, `POST /skills`*, `DELETE /skills/{id}`*, `GET /skills/{id}/zip`*, `GET /skills/{id}`, `POST /skills/upload` (zip), `POST /skills/sync`, `GET /knowledge/stats`, `GET|DELETE /knowledge/files`*, `POST /knowledge/upload?category=`*, `POST /knowledge/ingest`*, `GET /knowledge/retrieve?q=&skill=`, `GET /jobs/{id}`, `GET /health`. Endpoints marked * require the Studio password (HTTP basic auth).

## Environment

Copy `.env.example` to `.env`. Keys: `AOAI_API_KEY` (embeddings + index vectorizer), `SEARCH_ADMIN_KEY` (index and knowledge-base management), `SEARCH_QUERY_KEY` (only for `KB_MCP_AUTH=apikey`, a POC fallback when the managed-identity connection is unavailable). Foundry calls use `DefaultAzureCredential` (your `az login`).
