import sys
sys.path.insert(0, "/Users/atthawutchanthaphan/Library/Application Support/Claude/local-agent-mode-sessions/e5926c2b-4c91-48e7-986d-353f7f4abfde/d6910cb8-0ea2-436b-bced-5df179338639/rpm/plugin_01R8j7MfJnC78K7LmMKvu1Td/skills/bbl-pptx/references")
import bbl_deck as B

prs, LAY = B.new_deck()
FOOT = "Internal"

s = B.slide(prs, LAY, "Cover", title="User Manual: Product Assistant POC and Studio", footer=FOOT)
B.settext(s, 1, [("For testers and product owners", 0)])
B.settext(s, 13, [("Version 0.3 · September 2026", 0)])

s = B.slide(prs, LAY, "Title only", title="Getting started in four steps", footer=FOOT,
            lead="App: http://localhost:8010 · Studio: /studio (password = STUDIO_PASSWORD in .env).")
B.process_steps(s, [("Open the app", "Go to localhost:8010. The phone screen greets you with three starter questions."),
                    ("Ask and follow up", "Type Thai or English. The answer streams in; tap a suggested follow-up to continue the same conversation."),
                    ("Look behind the scenes", "The panel beside the phone shows the routing decision, agent, retrieval, tokens and cost per turn."),
                    ("Sign in to Studio", "Click 'Studio (testers)' at the bottom-left, enter your name and the password.")], y=3.25, accent_upto=0)

s = B.slide(prs, LAY, "Two columns", title="The customer app screen", footer=FOOT,
            lead="What each element on the phone does.")
B.settext(s, 29, [("Chat area", 0), ("Greeting and starter chips: tap to ask", 1), ("Blue bubbles are yours; white bubbles are the assistant's", 1),
                  ("Skill badge (e.g. credit-card · 95%): tap to open the Sources sheet", 1), ("Citation chips open the original bangkokbank.com page or PDF", 1),
                  ("Suggested follow-ups appear under the latest answer in your language", 1), ("👍 / 👎 under an answer sends feedback to the testers' review", 1)])
B.settext(s, 30, [("Top bar and composer", 0), ("History icon: past conversations and 'New chat'", 1), ("The conversation is remembered even after the server restarts", 1),
                  ("Type in the glass box and press Enter or the arrow; the mic and image buttons are decorative", 1),
                  ("Behind the scenes panel (right): route, agent, timeline, retrieval chunks, tokens, cost, sources; 'hide' collapses it", 1)])

s = B.slide(prs, LAY, "Title only", title="Studio at a glance", footer=FOOT,
            lead="Five tabs: author skills, grow knowledge, evaluate, review, configure.")
B.flow(s, ["Skills", "Knowledge", "Evals", "Conversations", "Settings"], y=3.05, h=0.95, accent=0)
B.label(s, 0.37, 4.55, 12.5, 1.2, "Every long action (sync, ingest, crawl, eval) runs as a job. The Knowledge tab shows it in the Ingestion panel: phase stepper, progress bar, counters, log and recent runs. Changes to skills reach the assistant only after 'Save & sync'.", sz=12)

s = B.slide(prs, LAY, "Title only", title="Skills tab: edit a skill and publish it", footer=FOOT,
            lead="A skill = one product family: description, keywords, model, follow-ups, instructions.")
B.process_steps(s, [("Open", "Click a skill card (dot = Foundry state: in sync, outdated, not deployed). Lint findings sit above the instructions (keyword overlap, missing Thai/English follow-ups, model not deployed)."),
                    ("Edit", "Routing and answering settings on the left, markdown instructions on the right. Cmd/Ctrl+S saves; an 'unsaved' badge warns before you leave."),
                    ("Save & sync", "Creates a new agent version in Foundry; the Foundry sync panel shows progress per skill and the result rows. 'Save' alone only writes the file."),
                    ("Test", "Playground pane: ask the deployed agent; the answer streams with sources and a trace card.")], y=3.25, accent_upto=2)
B.notes(s, "Other actions: New skill (form scaffolds a folder), Upload zip, Download zip, Delete skill (also removes its Foundry agent and knowledge base), Sync all. 'Try routing' in the form sends a sample question to the router only.")

s = B.slide(prs, LAY, "Three column", title="Skills tab: preview, versions, playground", footer=FOOT)
B.settext(s, 14, [("Preview", 0)]); B.settext(s, 31, [("Rendered markdown of the description and instructions, as the agent will read it.", 0)])
B.settext(s, 22, [("Versions", 0)]); B.settext(s, 32, [("Lists the Foundry versions of this skill's agent. Select one to see a line diff against your local file (green = local, red = deployed). 'Restore' copies that version's instructions into the editor.", 0)])
B.settext(s, 24, [("Playground", 0)]); B.settext(s, 33, [("Ask the deployed agent directly with the skill forced. The banner tells you which version is live and whether your edits are synced. 'New conversation' resets context.", 0)])

s = B.slide(prs, LAY, "Title only", title="Knowledge tab: add documents", footer=FOOT,
            lead="Documents live in knowledge/<category>/; a skill answers only from its own category.")
B.process_steps(s, [("Choose a category", "Pick an existing one or type a new id (it becomes the product_category of a skill)."),
                    ("Add content", "Drag .md/.pdf/.txt onto the drop zone, paste single URLs, or crawl a site section (start URL, prefix, max pages, PDFs)."),
                    ("Run ingest", "Only new or changed files are chunked and embedded. The Ingestion panel shows Scan, Embed, Upload progress with counts; the table shows chunks per file."),
                    ("Sync skills", "Go to Skills and 'Sync all' so the skill gets its knowledge base (Free tier allows three; extra categories share the general base).")], y=3.25, accent_upto=1)

s = B.slide(prs, LAY, "Three column", title="Knowledge tab: inspect and search", footer=FOOT,
            footnote="Image-only PDFs are kept but not ingested; the table flags them.")
B.settext(s, 14, [("File table", 0)]); B.settext(s, 31, [("Kind, size, chunk count and PDF text status per file. 're-ingest' re-embeds one file; 'delete' removes it (run ingest afterwards to drop its chunks). Click a file name to open the chunk browser.", 0)])
B.settext(s, 22, [("Search the index", 0)]); B.settext(s, 32, [("Runs the same question two ways: the skill's knowledge-base retrieve (what the agent sees) and a direct hybrid search of the index filtered by category, with reranker scores.", 0)])
B.settext(s, 24, [("Crawler", 0)]); B.settext(s, 33, [("Built in, no external service. Follows links only under the prefix, downloads linked PDFs and extracts their text, skips unchanged pages on re-runs. Start with 10-30 pages.", 0)])

s = B.slide(prs, LAY, "Two columns", title="Evals tab: measure before and after a change", footer=FOOT,
            lead="Run the question sets after any change; every run is stored with cost and latency.")
B.settext(s, 29, [("Routing and grounded answers", 0), ("Edit questions in the tables ('Add question', 'Save questions')", 1),
                  ("Routing: expected skill per question; reports accuracy and confusion", 1), ("Grounded: optional forced skill, expected substrings, whether a source is required", 1),
                  ("The Eval run panel shows progress with pass/fail counts; the result opens with KPI cards and a failed-only filter", 1),
                  ("Export .xlsx per run, or 'Export all' for every stored run (one sheet each); run history shows a trend", 1)])
B.settext(s, 30, [("Quality (DeepEval) and model comparison", 0), ("Quality: a judge model (your Foundry deployment) scores each answer on RAG metrics (faithfulness, relevancy, context) and agentic metrics (tool use, task completion, language & tone, no advice)", 1),
                  ("Hover a score for the judge's reason; tune the threshold and the metric set; export .xlsx", 1),
                  ("Question sets: download as .xlsx (also the template) and upload an .xlsx or CSV to append or replace; or send questions from the Conversations tab", 1),
                  ("Comparison: pick a skill, two or three models and a few questions; temporary agents are deleted after the run", 1),
                  ("Side-by-side answers with latency, tokens and cost per model", 1)])

s = B.slide(prs, LAY, "Two columns", title="Conversations tab: review and give feedback", footer=FOOT,
            lead="Everything customers and testers asked, and what the system did.")
B.settext(s, 29, [("Questions and selection box", 0), ("Search question or answer text; filter by skill, rating and source; every customer question is one row", 1),
                  ("Tick questions (or 'select all shown', or all questions of one conversation) to fill the selection box", 1),
                  ("Export the box to .xlsx (question, answer, skill, rating, comment, cost, latency)", 1),
                  ("Send the box to the Routing, Grounded or Quality eval set; duplicates are skipped", 1)])
B.settext(s, 30, [("Transcript and feedback", 0), ("Rate each answer 👍/👎 and add a comment for the real-app team (your name is attached)", 1), ("Expand 'trace' to see routing, retrieval and cost for that answer", 1), ("Ratings given in the customer app appear here too; 'Feedback CSV' exports them all", 1)])

s = B.slide(prs, LAY, "Four column with fotnot/data source", title="Settings tab", footer=FOOT,
            footnote="Overrides live in .state/settings.json and win over .env; model changes need a sync.")
for hidx, bidx, head, body in [(14, 48, "Usage & prices", "Sessions, answers, tokens, total and average cost, per-day table. Edit USD prices per model (per 1M tokens) and save to pricing.yaml."),
                               (30, 49, "Base rules", "The shared persona, language, grounding and compliance rules prepended to every skill. 'Save & sync all' republishes every agent."),
                               (33, 50, "Runtime settings", "Router model, default skill model, knowledge-base reasoning, assistant and customer names. Dropdowns list the live Foundry deployments."),
                               (36, 51, "Export / import", "Download a bundle (skills, knowledge text, evals, prices, settings) or import one (merge or replace) to reproduce the setup elsewhere.")]:
    B.settext(s, hidx, [(head, 0)]); B.settext(s, bidx, [(body, 0)])

s = B.slide(prs, LAY, "Title and content", title="When something looks wrong", footer=FOOT)
B.settext(s, 13, [("The answer says it has no details on a topic yet", 0), ("Expected when the category has no documents or the retrieved text does not cover the question. Add documents, ingest, sync.", 1),
                  ("A skill shows 'outdated' or 'missing' in Foundry", 0), ("Run 'Save & sync' on the skill, or 'Sync all'. The log shows any error.", 1),
                  ("'knowledge-source quota exceeded' in the sync log", 0), ("Free search tier allows three; the skill still works through the shared general base with a scoping note.", 1),
                  ("'Model deployment rate limit exceeded'", 0), ("The model's quota is too small for the traffic; use a smaller model on the skill or raise the deployment capacity.", 1),
                  ("An answer looks expensive in the trace", 0), ("Large reasoning models retrieve more than once; long conversations grow; start a new chat or switch the skill model.", 1)])

s = B.slide(prs, LAY, "Outro", title="Thank you", footer=FOOT)
B.settext(s, 20, [("App", 0), ("http://localhost:8010", 1)])
B.settext(s, 21, [("Studio", 0), ("http://localhost:8010/studio", 1)])
B.settext(s, 22, [("Guide", 0), ("docs/howto-add-skill.md in the repository", 1)])

B.finish(prs, "docs/slides/bankrag-user-manual.pptx")
print("manual deck written")
