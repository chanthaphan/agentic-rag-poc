# How to add or change a skill, and how to add documents

## The easy way: Studio (testers)

Open **http://localhost:8010/studio** and sign in with the Studio password (`STUDIO_PASSWORD` in `.env`); enter your name so feedback is attributed. On the deployed app people sign in with their Microsoft account instead and need the `admin` or `tester` role on the access list (Settings > Access); the `external` role opens a chat page only, not these tabs (see [architecture.md](architecture.md#studio-access-by-identity)). Tabs:

- **Skills**: list with publish state, version and lint badges. The editor has four panes: *Edit* (form + markdown, lint findings, "Try routing", Cmd/Ctrl+S, unsaved guard), *Preview* (rendered markdown), *Versions* (published agent versions, line diff of the published instructions vs your local file, "Restore this version's body"), *Playground* (ask the deployed agent a question, streamed, with sources and a trace card).
- **Knowledge**: per-space files (a knowledge space = one `knowledge/<space>` folder) with PDF text status, drag-and-drop upload, per-file re-ingest, chunk browser (click a file), hybrid index search next to the knowledge-base retrieve, a built-in crawler (start URL + prefix + max pages, follows links and PDFs, no external service) and single-URL import.
- **Evals**: editable routing and grounded-answer question sets, run buttons with live logs, results and run history; model comparison runs the same questions on 2-3 models with temporary agents (deleted afterwards).
- **Conversations**: every session with cost, skills and ratings; open a transcript, rate answers 👍/👎 with a comment; export `feedback.csv`. Customers can also rate answers in the app.
- **Settings**: usage and prices, the shared base rules (with "Save & sync all"), runtime settings (router/default models from the live deployment list, KB reasoning, names), export/import bundle.

- **Skills tab**: click a skill to edit its name, description (what the router reads), knowledge space, keywords, model, top-K, suggested follow-ups (write both Thai and English lines; the app shows the ones matching the user's language) and the markdown instructions. **Save** writes `skills/<id>/SKILL.md`; **Save & sync** also publishes a new agent version and shows `version N -> N+1`. **New skill** scaffolds a folder from a form; **Upload zip** installs a packaged skill; **Delete skill** removes the folder and prunes its published versions / knowledge base.
- **Knowledge tab**: pick a knowledge space (or type a new one), upload `.md` / `.pdf` / `.txt` files, **Run ingest** (incremental) and watch the log, delete files, check per-space chunk counts and index size, and test retrieval against a skill's knowledge base.
- **Settings & usage tab**: total sessions/answers, tokens, cost and latency (from `.state/bankrag.db`), per-day table, and the model price table (USD per 1M tokens, saved to `pricing.yaml`).
- The customer app at **/** has a "Behind the scenes" panel next to the phone that shows, per turn, the routing decision and reason, language, agent, a timeline bar (route / agent+retrieval / sources), retrieval chunks and context tokens, token and cost bars, session totals, and the sources.

Everything below is the file-based equivalent.

## Add a skill

1. Create `skills/<id>/SKILL.md` (copy `skills/insurance/SKILL.md`). Frontmatter:

```yaml
---
name: Mortgage Advisor
id: mortgage                 # lowercase letters, digits, dashes; must equal the folder name
description: >-              # Thai + English, one to three sentences; the router reads this
  สินเชื่อบ้าน ... / Home loans: rates, eligibility, documents.
product_category: mortgage   # documents live in knowledge/mortgage/
keywords: [สินเชื่อบ้าน, home loan, mortgage, refinance]
model: gpt-4.1-mini          # optional
top_k: 5                     # optional, documents returned per retrieval
---
Markdown instructions for this product family...
```

2. `uv run bankrag skills validate`
3. `uv run bankrag skills sync` (or the **Sync** button in the web UI). This creates `ks-mortgage` and `kb-mortgage` in Azure AI Search, publishes version 1 of the agent `bank-mortgage` in the local registry, and republishes `bank-router` and `bank-concierge`.

Upload instead of editing: zip the folder (SKILL.md at the root or inside one folder) and use **Upload skill zip** in the web UI, or `uv run bankrag skills install mortgage.zip`, then sync.

## Rename the persona, or change its voice

The assistant's name and gender are settings, not text in the prompt files: Studio › Settings › Runtime › *Assistant name* (Thai and English) and *Assistant gender*, or `ASSISTANT_NAME` / `ASSISTANT_NAME_EN` / `ASSISTANT_GENDER`. The prompts refer to them through placeholders, filled everywhere the persona speaks:

| placeholder | male | female |
|---|---|---|
| `{assistant_name}` / `{assistant_name_en}` | the two names | the two names |
| `{gender_word}` | man | woman |
| `{particle}` / `{particle_q}` / `{particle_soft}` | ครับ / ครับ / นะครับ | ค่ะ / คะ / นะคะ |
| `{pronoun_th}` | ผม | ดิฉัน |
| `{wrong_particles}` | ค่ะ, คะ, นะคะ, ดิฉัน | ครับ, นะครับ, ผม |

The app greeting and the speech-mode prompt read the same values. Save, then **Sync all**: they are part of every agent definition, so the change is published as a new version.

## Change a skill

Edit `SKILL.md`, run `bankrag skills sync`. Only skills whose definition hash changed get a new agent version; `bankrag skills list` shows `in-sync` / `outdated` / `missing`.

## Remove a skill

Delete the folder and run `bankrag skills sync --prune` (deletes the published versions, knowledge base and knowledge source of skills whose folder is gone).

## Add documents

Drop `.md` or `.pdf` files anywhere under `knowledge/<product_category>/`, then `uv run bankrag ingest --category <product_category>` (or **Upload documents** + **Run ingest** in the UI). Optional metadata: frontmatter in `.md` files, or a sibling `doc.yaml` for PDFs:

```yaml
title: บัตรเดบิตบีเฟิสต์ สมาร์ท
source_url: https://www.bangkokbank.com/th-TH/Personal/Cards/Debit-Cards/Be1st-Smart
product_name: Be1st Smart
doc_type: product-page
```

Rules: `README.md` and `doc.yaml` are ignored; a `.pdf` next to a `.md` with the same stem is skipped (the `.md` is treated as its extracted text); image-only PDFs are skipped with a warning (no OCR).

## Re-seed credit cards from the crawler

`uv run bankrag seed --clear` (add `--include-promotions` to also ingest promotion pages, deduplicated by URL).
