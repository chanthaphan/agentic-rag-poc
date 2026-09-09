# Responsible Lending

Some products may not be talked about freely. Under the Bank of Thailand market-conduct notification 3/2568 (and the
SEC notification สธ. 10/2558), any communication that mentions a loan or a credit card is advertising: it must carry
fixed warnings, must state a reference rate with its date whenever it quotes an interest rate, and must never make
borrowing sound effortless. An answer the assistant sends to a customer is such a communication.

Bangkok Bank already checks its marketing media against those rules in **MCCS (Media Compliance Checker System)**.
This POC takes a **subset of the MCCS rule set** — the 12 rows exported as `mccs-rules.xlsx` — and applies it to the
assistant's answers instead of to an advertisement. Same rules, same wording, a different medium.

So the assistant needs two things: the concierge has to **know, before it answers, that some product families are
regulated**, and the app has to **prove afterwards that the answer that went out complied**. Both come from one store.

## Where the rules live

```
rules/mccs/
  PACK.md                       pack metadata + the product taxonomy
  credit-card-use-warning.md    one file per rule (12 today, from mccs-rules.xlsx)
  floating-rate-warning.md
  …
```

Rules are files in git, like `skills/`, not rows in a database: they are legal text, so they want review, diffs, and
the same bundle export/import as everything else. One file per rule keeps a change to one rule a one-file diff.

Each rule is markdown with YAML frontmatter. The **body** is owned by the compliance team and the **frontmatter** by
engineering:

```markdown
---
id: credit-card-use-warning
pack: mccs
title: คำเตือนการใช้บัตรเครดิต "ใช้เท่าที่จำเป็นและชำระคืนได้เต็มจำนวนตามกำหนด"
regulation: (MCCS) ประกาศธนาคารแห่งประเทศไทยที่ 3/2568, …          # เล่มกฎหมาย
clause: เอกสารแนบ 2 ข้อ 2.3.3 (1)                                  # ข้อกฎหมาย
products: [credit-card-bbl, credit-card-other]                     # ผลิตภัณฑ์ที่ต้องตรวจสอบ
status: active            # active | draft | retired               # สถานะ
trigger: promotion        # promotion (only when the answer sells) | mention (any mention)
severity: block           # block | warn
check: required_phrase    # required_phrase | prohibited_phrase | required_pattern | judgement
enforcement: append       # append (add the mandated wording) | flag | none
phrases: [ใช้เท่าที่จำเป็นและชำระคืนได้เต็มจำนวนตามกำหนด จะได้ไม่เสียดอกเบี้ย, Use when necessary and pay back full amount on time to avoid]
patterns: []              # regex, for required_pattern and wording variants
applies_when: []          # regex on the answer; empty = whenever one of its products is mentioned
disclosure: {th: …, en: …}
template: '"ใช้เท่าที่จำเป็น…" หรือ "Use when necessary…"'
---
## กฎหมาย (legal text)          ← verbatim from the sheet
## กฎสำหรับระบบ (system rule)   ← verbatim from the sheet; goes into the agent instructions
## หมายเหตุสำหรับผู้ช่วย (assistant note)  ← ours: how the rule applies to a chat answer; survives re-imports
```

`PACK.md` holds the product taxonomy — the bridge between the regulator's product names and the app:

```yaml
products:
  - id: credit-card-bbl
    name: บัตรเครดิตของธนาคารกรุงเทพ      # exactly as the sheet writes it
    aliases: [บัตรเครดิตบัวหลวง, Bangkok Bank credit card, …]
    skills: [credit-card]                  # which skill agents carry these rules
    match: ['บัตรเครดิต', '(?i)\bcredit\s+card\b']   # how the family is detected in a question or an answer
```

`skills:` is the answer to "which skill uses which product family". Loan families point at `general` today because
there is no loan skill yet; tick a different skill (in Studio, or in this file) and its agent carries those rules from
the next sync — no code change. It also decides the fallback when the answer's wording alone is not conclusive: an
answer from a specialist whose single family is regulated is treated as being about that family.

## How a rule reaches an answer

**Prompt time — the agent knows before it answers.** On `bankrag skills sync`, the rules covering a skill's product
families are compiled into that agent's instructions (after the base rules and the skill body), and a summary of every
regulated family plus the required warnings is compiled into the `bank-concierge` agent. The block is part of the
agent definition that is hashed, so editing a rule makes the affected agents "outdated" in Studio and the next sync
creates a new version of exactly those agents.

```bash
bankrag rules prompt --skill credit-card   # the block that goes into bank-credit-card
bankrag rules prompt                       # the block that goes into bank-concierge
```

**Answer time — the app proves it.** Before an answer leaves `ChatSession` (both the router path and the A2A concierge
path), `rules.guard()` runs over the drafted text:

1. detect the product families in the **answer** (taxonomy `match`, or the specialist's own family). What the customer
   typed is not the advertisement, so the question is not searched;
2. decide whether the answer is *advertising*: `promotion.signals` in `PACK.md` say what offering or recommending looks
   like (a quoted rate, fee, instalment or benefit, a comparison, a suggestion, how to apply), and `promotion.exclude`
   holds our own "no details yet" sentences, which veto only when the answer quotes no figures. A definition, a general
   answer or a refusal is not an advertisement and gets no warning;
3. take the active rules covering those families, skipping the `trigger: promotion` ones when the answer is not
   advertising, and those whose `applies_when` does not match the answer;
4. decide a verdict per rule — the sheet's vocabulary: `compliant` / `non_compliant` / `undefined` / `not_applicable`;
5. append the mandated wording verbatim for `enforcement: append` rules that are missing it, grouped under the family's
   short customer-facing label (`label` / `label_en` in `PACK.md`, not the long name the MCCS sheet uses), and stream
   exactly that text as one more delta so the stored answer is the one the customer read:

   ```
   ---
   **บัตรเครดิต**
   ⚠️ ใช้เท่าที่จำเป็นและชำระคืนได้เต็มจำนวนตามกำหนด จะได้ไม่เสียดอกเบี้ย
   ```

   The app owns this block: the agents are told never to write a warning or their own version of one, so the customer
   never reads the same warning twice. An English answer gets the English label and the English disclosure — except
   where the regulator mandates a Thai phrase with no official English version (ข้อ 2.2.1 (1)), which stays Thai.

6. write the report to the turn's `trace.compliance`, which the "Behind the scenes" panel and Studio render.

| `check` | decided how | typical rule |
|---|---|---|
| `required_phrase` | the wording is present (whitespace / smart quotes normalised) | the three mandatory warnings |
| `prohibited_phrase` | none of the phrases or regexes appear | ข้อ 2.3.1 over-indebtedness wording |
| `required_pattern` | a regex for the required figures matches | MRR + date, effective-rate range, instalment assumptions |
| `judgement` | nobody can decide it from the text alone → `undefined` | "show the key conditions completely and clearly" |

`judgement` rules are still enforced — in the agent instructions — but the guard reports them for review rather than
pretending to check them. A `required_pattern` violation is never auto-fixed: the missing figures are facts the app
does not have, so the rule tells the agent to drop the number instead of publishing an incomplete disclosure.

## Editing without Excel

Studio → Settings → **Responsible lending** edits the same files, and a change applies to the answer-time guard
immediately (the pack is re-read when a file changes); the agents pick it up on the next `skills sync`.

| what | where |
|---|---|
| which skill carries a product family | **Product families → edit** → tick the skills; the same dialog holds the family's aliases and detection regexes |
| a rule's status, severity, check type, enforcement, wording, regexes, disclosure | **Rules → edit** |
| a rule's legal text / system rule (from MCCS) and our assistant note | same dialog; the two MCCS sections are overwritten by the next import, the note is not |
| a rule that has no MCCS row (an internal policy) | **New rule** |
| a product family MCCS mentions but the pack did not know | **New family**, or the placeholder an import created |

The same edits are available as `PUT /rules/{id}`, `POST /rules`, `DELETE /rules/{id}`,
`PUT /rules/products/{id}`, `DELETE /rules/products/{id}` (Studio admin), or by editing the files directly and
running `bankrag rules validate`. Every write is validated the way the loader reads it — a bad regex, an unknown
product or a missing disclosure is refused rather than silently breaking the guard.

## Keeping the sheet as the source

The compliance team works in MCCS and exports `mccs-rules.xlsx`. Import merges that sheet into the files, keeping
the engineering frontmatter and our assistant notes:

```bash
bankrag rules import ~/Downloads/mccs-rules.xlsx --dry-run   # what would change
bankrag rules import ~/Downloads/mccs-rules.xlsx
bankrag rules validate
bankrag skills sync                                          # push the change into the agents
```

Rows match existing rules by rule id, else by clause + the opening of the legal text. A product name the pack does not
know is not dropped: it is added to `PACK.md` as `p-<hash>` and reported, so a reviewer opens it and gives it a
readable id, aliases and skills. Studio does the same import through the browser (preview, then confirm).

`bankrag rules export` writes the sheet back with the same columns plus the rule id and how each rule is checked.

## What this does not do

- **Images.** Several rules also govern font size and contrast of a warning inside an advertising image. In chat the
  answer is text, so those clauses cannot fail — the `undefined` verdict the sheet describes for them belongs to an
  asset-review pipeline, not here.
- **Judgement rules are not judged per turn.** Adding them to the answer-time check means an LLM call per answer; today
  they are enforced in the prompt and surfaced as "to review" on the turn.
- **Product detection is lexical.** An answer that circles a loan without naming it may not match a family. The
  specialist's own family is used as a fallback, which is why loan families should get their own skill.
- **The sales gate is lexical too.** `promotion.signals` is a keyword list, so an answer that recommends a product in
  wording nobody listed gets no warning. Widen the list in `PACK.md` (or Studio) when you see a miss; the trade-off is
  deliberate — the bank asked for warnings on the sales pitch, not on every sentence that names a product.
