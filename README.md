# agentic-rag-poc: bank product recommendation agents

Proof of concept for Bangkok Bank product Q&A / recommendation built on **Microsoft Foundry agents** grounded in an **Azure AI Search** knowledge base through **Foundry IQ** (knowledge bases exposed to agents over MCP). Two things are meant to be easy:

- **Knowledge base**: drop `.md` / `.pdf` files into `knowledge/<product-category>/` and run `bankrag ingest`.
- **Evals**: routing accuracy, grounded answers, DeepEval quality metrics judged by a Foundry model (LLM-as-judge), model comparison; every run exports to .xlsx from Studio.
- **Responsible Lending**: a subset of the rules from BBL's MCCS (Media Compliance Checker System) lives in `rules/mccs/`, one file per rule. They are compiled into the concierge and specialist instructions, and every drafted answer is checked before it is sent (missing mandatory warnings are appended verbatim).
- **Live tools**: `bank-services` answers from the bank's own APIs instead of documents (today's FX rates) through an MCP endpoint the app hosts at `/mcp/services`. A skill opts in with `tools:` in its frontmatter; the Foundry agent calls it as the project's managed identity, so the endpoint stays behind Easy Auth and no key travels in the agent definition.
- **Skills**: one folder per product family in `skills/<id>/SKILL.md` (frontmatter + instructions). `bankrag skills sync` turns each skill into a Foundry agent wired to its own knowledge base; the `bank-router` agent picks the skill for each user message.

Read [docs/architecture.md](docs/architecture.md), [docs/howto-add-skill.md](docs/howto-add-skill.md), [docs/responsible-lending.md](docs/responsible-lending.md), [docs/decisions.md](docs/decisions.md), and (after the end-to-end run) [docs/findings-for-real-app.md](docs/findings-for-real-app.md).

## Two front ends

- **/** – customer app: the Bangkok Bank mobile prototype's Conversation screen inside an iPhone frame (BBL Sans, glass background, suggestion chips, typing dots, bottom tab bar). Conversations are persisted in SQLite (`.state/bankrag.db`, tables `sessions` and `turns` with per-answer skill, language, tokens, latency and retrieved-chunk counts) so follow-up questions keep their Foundry conversation and previous skill even after a restart; `GET /sessions/stats` aggregates usage and cost; model prices live in `pricing.yaml` (editable in Studio → Settings & usage) and every answer carries a `trace` with timings, token usage, retrieval stats and USD cost; the history button lists past chats. A 👎 on an answer asks why: pick any of the listed reasons (wrong information, did not answer the question, not enough detail, hard to understand, wrong language or tone, too slow), add a note, and it is stored with the rating for review in Studio. A "Behind the scenes" panel beside the phone shows routing, detected language, agent, tool calls and sources per turn. The language of each message (Thai or English) is detected per turn; the answer and the suggested follow-ups follow it. Answers stream token by token and render as markdown (bold, lists, tables). Foundry conversations are rotated every 6 answers (with a recap) to keep input tokens bounded.
- **/studio** – tester workbench (login page, `STUDIO_PASSWORD`): skill editor with lint, preview, version diffs and a streamed playground; knowledge files with PDF status, chunk browser, index search, a built-in site crawler and URL import; eval runner and model comparison; conversation review with ratings and CSV export; base rules, the Responsible Lending rule pack (edit rules and the product family → skill mapping, import/export the MCCS sheet, dry-run the guard on any answer), runtime settings and bundle export/import. `/legacy` keeps the original debug page. Access roles: `admin`, `tester`, and `external`, which gets a chat web page at `/` (in place of the customer app, with a panel showing how each answer was produced) and none of the Studio tabs.

## Layout

```
skills/            _base (shared rules) + credit-card, debit-card, insurance, wealth, general
rules/mccs/        Responsible Lending rules (PACK.md product taxonomy + one file per rule)
knowledge/         credit-card/ seeded from the bblwebsite_crawler (19 products, page + PDF texts)
src/bankrag/       config, skills, search_index, ingest/, knowledge_base, connections, foundry_sync, router, chat, api, cli
web/               mobile.* (customer app), studio.* + studio-common.js (tester workbench), external.* (chat page for the external role), index.html (legacy debug), fonts/
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
| list / validate the Responsible Lending rules | `uv run bankrag rules list` · `uv run bankrag rules validate` |
| import the compliance team's sheet | `uv run bankrag rules import mccs-rules.xlsx [--dry-run]` |
| check an answer against the rules | `uv run bankrag rules check "…" --skill credit-card` |
| check the live FX service + its JSON shapes | `uv run bankrag services probe` |
| today's rate for one currency | `uv run bankrag services fx USD` |
| tests | `uv run pytest` |

## API

`POST /chat` (session_id keeps context), `GET /sessions`, `GET|DELETE /sessions/{id}`, `GET /app/config`, `GET /skills`, `PUT /skills/{id}`*, `POST /skills`*, `DELETE /skills/{id}`*, `GET /skills/{id}/zip`*, `GET /skills/{id}`, `POST /skills/upload` (zip), `POST /skills/sync`, `GET /knowledge/stats`, `GET|DELETE /knowledge/files`*, `POST /knowledge/upload?category=`*, `POST /knowledge/ingest`*, `GET /knowledge/retrieve?q=&skill=`, `GET /jobs/{id}`, `GET /health`. Endpoints marked * require the Studio password (HTTP basic auth).

## Environment

Copy `.env.example` to `.env`. Keys: `AOAI_API_KEY` (embeddings + index vectorizer), `SEARCH_ADMIN_KEY` (index and knowledge-base management), `SEARCH_QUERY_KEY` (only for `KB_MCP_AUTH=apikey`, a POC fallback when the managed-identity connection is unavailable). Foundry calls use `DefaultAzureCredential` (your `az login`).

## Deploy to Azure (low cost)

The app runs as one container on **Azure Container Apps (consumption)** with an **Azure Files** share for skills, Responsible Lending rules, knowledge, evals, prices and JSON state; the SQLite session database stays on the container's local disk and is backed up to the share every minute (restored on boot). Approximate monthly cost: container ~$10-15 at 1 replica, storage <$1, Azure Container Registry Basic $5, Free-tier search $0, models pay-per-use.

```bash
./infra/00-login.sh                 # personal tenant
export ACR_NAME=bankragacr
./infra/10-acr-build.sh             # builds the image in ACR (no local Docker needed); prints IMAGE=...
IMAGE=<printed image> ./infra/11-containerapp.sh   # storage share, environment, app, secrets + STUDIO_ADMINS/TESTERS/EXTERNALS from .env, volume, Foundry roles
./infra/12-easyauth.sh              # Entra ID sign-in in front of the whole app (app registration + built-in auth)
./infra/13-services-tool.sh         # live FX/branch tools: lets the Foundry project's identity call /mcp/services
```

Re-deploy after a code change: run `10-acr-build.sh`, then `az containerapp update -g my-aiverse -n talkwithgrace --image <IMAGE>`. The app identity uses managed identity for Foundry (`DefaultAzureCredential`), so no `az login` is needed inside the container. Environment: `DATA_DIR=/data` (mounted share), `SQLITE_DB_PATH` / `SQLITE_DB_BACKUP` set by `docker/entrypoint.sh`.
