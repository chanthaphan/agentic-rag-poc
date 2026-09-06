import sys
sys.path.insert(0, "/Users/atthawutchanthaphan/Library/Application Support/Claude/local-agent-mode-sessions/e5926c2b-4c91-48e7-986d-353f7f4abfde/d6910cb8-0ea2-436b-bced-5df179338639/rpm/plugin_01R8j7MfJnC78K7LmMKvu1Td/skills/bbl-pptx/references")
import bbl_deck as B
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import PP_ALIGN

prs, LAY = B.new_deck()
FOOT = "Internal"

# 1 cover
s = B.slide(prs, LAY, "Cover", title="Bank Product Recommendation Agents", footer=FOOT)
B.settext(s, 1, [("POC on Microsoft Foundry + Azure AI Search", 0)])
B.settext(s, 13, [("AI Tech Team · September 2026", 0)])

# 2 objective
s = B.slide(prs, LAY, "Bulletpoint slide with content", title="What the POC set out to prove", footer=FOOT,
            lead="One assistant, many products: a skill per family and a knowledge base anyone can grow.")
B.settext(s, 29, [("Easy-to-build knowledge base: drop documents or crawl a site section, then ingest", 0),
                  ("Skills per product (credit card, debit card, insurance, wealth) as editable files, uploaded and synced to Foundry", 0),
                  ("The agent routes each question to a skill and answers only from that skill's documents, with citations", 0),
                  ("Everything measured (routing accuracy, cost, latency, retrieval) so the real app can be designed from evidence", 0)])
B.notes(s, "Objective as stated at kickoff: construct knowledge (documents) and skills per product easily, let the agent pick the skill from the user query, and use the output to design the knowledge system of the real app.")

# 3 runtime concept flow
s = B.slide(prs, LAY, "Title only", title="How a question becomes a grounded answer", footer=FOOT,
            lead="Every message: two Foundry agents and one Foundry IQ knowledge base.")
B.flow(s, ["Customer question", "Router agent picks a skill", "Skill agent (SKILL.md rules)", "Knowledge base retrieve (MCP)", "Grounded answer + citations"], y=3.05, h=1.05, accent=2)
B.label(s, 0.37, 4.60, 12.5, 0.9, "Language detected per message; the answer and the suggested follow-ups follow it. Session context is kept in a Foundry conversation and rotated every six answers to bound cost.", sz=12, col=B.INK)
B.notes(s, "Router: gpt-4.1-mini with strict JSON output (skill id, confidence, language, reason). Skill agent: instructions = shared base rules + the skill's SKILL.md; its only tool is knowledge_base_retrieve on its own knowledge base. Off-topic questions get a fixed reply without retrieval.")

# 4 platform stack
s = B.slide(prs, LAY, "Title only", title="Platform layers", footer=FOOT,
            lead="Each layer is replaceable; product teams only edit skill and document folders.")
B.layer_stack(s, [("Front ends", "Mobile-style customer app (streaming chat) and tester Studio"),
                  ("Application API", "FastAPI: sessions in SQLite, routing policy, traces, evals, feedback"),
                  ("Foundry Agent Service", "bank-router + one versioned prompt agent per skill"),
                  ("Foundry IQ", "Knowledge base + knowledge source per product family (filtered)"),
                  ("Azure AI Search", "One index: Thai analyzer, vectors, semantic ranking"),
                  ("Documents", "knowledge/<category>/ from uploads or the built-in crawler")], top=1.95, h=0.72, gap=0.10)

# 5 skills as files
s = B.slide(prs, LAY, "Title only", title="A skill is a folder, a knowledge base is a folder", footer=FOOT,
            lead="Files in, Foundry objects out: nothing is hand-configured in the portal.")
B.process_steps(s, [("Author", "skills/<id>/SKILL.md: name, description, keywords, model, suggestions and the instructions in markdown."),
                    ("Validate", "Lint: keyword overlap, bilingual follow-ups, model deployed, documents present."),
                    ("Sync", "Creates the knowledge source, knowledge base, connection and a new agent version only when the hash changed."),
                    ("Evaluate", "Routing set, grounded-answer set, model comparison; results stored per run.")], y=3.25, accent_upto=2)
B.notes(s, "Agents are immutable in Foundry, so every edit is a new version. The hash of the desired definition is stored in the version metadata, which makes sync idempotent. The router is rebuilt on every sync because its schema lists the skill ids.")

# 6 knowledge pipeline
s = B.slide(prs, LAY, "Title only", title="Knowledge pipeline", footer=FOOT,
            lead="From a website section or an upload to a filtered knowledge base, incrementally.")
B.process_steps(s, [("Collect", "Built-in crawler (pages + linked PDFs under a URL prefix), drag-and-drop upload, or single URLs."),
                    ("Clean & chunk", "Strip site chrome, dedupe, heading-aware chunks of about 450 tokens with a breadcrumb."),
                    ("Embed & index", "text-embedding-3-large into one index; only changed files are re-processed."),
                    ("Serve", "Knowledge source per category (base filter) inside a Foundry IQ knowledge base.")], y=3.25, accent_upto=3)
B.notes(s, "Seed so far: 19 credit-card products (page + brochures) from the earlier crawl, plus Be1st debit cards, bancassurance and wealth sections crawled by the app itself. 1,220 chunks in the index.")

# 7 measured
s = B.slide(prs, LAY, "Four column with fotnot/data source", title="What we measured", footer=FOOT,
            footnote="Source: Studio evals and per-turn traces, 6 Sep 2026; list prices.")
for hidx, bidx, head, body in [(14, 48, "28 / 28", "Routing accuracy on the Thai and English question set; about 2.4 s and $0.0002 per routing call."),
                               (30, 49, "$0.008", "Cost per grounded answer on gpt-4.1-mini. gpt-5.4-mini was cheaper and faster in the comparison; gpt-5.6-luna cost up to 6x more."),
                               (33, 50, "~16k tokens", "Retrieval context returned per knowledge-base call regardless of top-K; Thai text arrives JSON-escaped, which inflates tokens."),
                               (36, 51, "152k -> bounded", "Input tokens of one late-session answer before conversation rotation; now capped by rotating the conversation every six answers.")]:
    B.settext(s, hidx, [(head, 0)]); B.settext(s, bidx, [(body, 0)])

# 8 findings
s = B.slide(prs, LAY, "Two columns", title="Findings that shape the real app", footer=FOOT,
            lead="What worked, and what production must do differently.")
B.settext(s, 29, [("Worked as designed", 0), ("Skill folder = agent version; idempotent sync; portal visibility", 1), ("Foundry IQ retrieval with citations; strict grounding ('not in the knowledge base' instead of guessing)", 1),
                  ("Streaming, persistent sessions, follow-ups after restarts", 1), ("Testers can do everything from Studio without engineers", 1)])
B.settext(s, 30, [("Must change for production", 0), ("Basic+ search tier and managed identities (Free tier: 3 knowledge sources, key-based access)", 1),
                  ("Answer consistency: chunk by product and validity year; two-step retrieval (identify card, then retrieve)", 1),
                  ("Small fast models on skill agents; larger models only for review steps", 1), ("Grounded-answer evals with exact expected figures after every change", 1)])

# 9 roadmap
s = B.slide(prs, LAY, "Title only", title="Suggested next steps", footer=FOOT,
            lead="From POC to a pilot-ready knowledge system.")
B.milestone_timeline(s, [("Now", "POC complete: 4 product families, Studio, evals"),
                         ("Next month", "Basic tier, managed identity, product-aware chunking"),
                         ("Quarter", "Blob knowledge sources, skills in git with CI sync"),
                         ("Pilot", "Contact-centre pilot with feedback and cost dashboard")], y=3.9)

# 10 outro
s = B.slide(prs, LAY, "Outro", title="Thank you", footer=FOOT)
B.settext(s, 20, [("Repository", 0), ("github.com/chanthaphan/agentic-rag-poc (private)", 1)])
B.settext(s, 21, [("Docs", 0), ("docs/architecture.md · docs/decisions.md · docs/findings-for-real-app.md", 1)])
B.settext(s, 22, [("Contact", 0), ("Atthawut Chanthaphan", 1)])

B.finish(prs, "docs/slides/bankrag-concept.pptx")
print("concept deck written")
