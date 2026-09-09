# Responsible Lending

Some products may not be talked about freely. Under the Bank of Thailand market-conduct notification 3/2568 (and the
SEC notification สธ. 10/2558), any communication that mentions a loan or a credit card is advertising: it must carry
fixed warnings, must state a reference rate with its date whenever it quotes an interest rate, and must never make
borrowing sound effortless. An answer the assistant sends to a customer is such a communication.

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

Loan families point at `general` today because there is no loan skill yet; add one and move the `skills:` entry, and
its agent picks the rules up on the next sync — no code change.

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

1. detect the product families in the question + answer (taxonomy `match`, or the specialist's own family);
2. take the active rules covering them, skipping those whose `applies_when` does not match the answer;
3. decide a verdict per rule — the sheet's vocabulary: `compliant` / `non_compliant` / `undefined` / `not_applicable`;
4. append the mandated wording verbatim for `enforcement: append` rules that are missing it, and stream that text as
   one more delta so the customer sees the full answer;
5. write the report to the turn's `trace.compliance`, which the "Behind the scenes" panel and Studio render.

| `check` | decided how | typical rule |
|---|---|---|
| `required_phrase` | the wording is present (whitespace / smart quotes normalised) | the three mandatory warnings |
| `prohibited_phrase` | none of the phrases or regexes appear | ข้อ 2.3.1 over-indebtedness wording |
| `required_pattern` | a regex for the required figures matches | MRR + date, effective-rate range, instalment assumptions |
| `judgement` | nobody can decide it from the text alone → `undefined` | "show the key conditions completely and clearly" |

`judgement` rules are still enforced — in the agent instructions — but the guard reports them for review rather than
pretending to check them. A `required_pattern` violation is never auto-fixed: the missing figures are facts the app
does not have, so the rule tells the agent to drop the number instead of publishing an incomplete disclosure.

## Keeping the sheet as the source

The compliance team works in `mccs-rules.xlsx`. Import merges it into the files, keeping the engineering frontmatter
and our assistant notes:

```bash
bankrag rules import ~/Downloads/mccs-rules.xlsx --dry-run   # what would change
bankrag rules import ~/Downloads/mccs-rules.xlsx
bankrag rules validate
bankrag skills sync                                          # push the change into the agents
```

Rows match existing rules by rule id, else by clause + the opening of the legal text. A product name the pack does not
know is not dropped: it is added to `PACK.md` as `p-<hash>` and reported, so a reviewer gives it an id, aliases and
skills. Studio → Settings → **Responsible lending** does the same through the browser (preview, then confirm), lists
what each agent carries, and has a "Try an answer" box that runs the same guard over arbitrary text.

`bankrag rules export` writes the sheet back with the same columns plus the rule id and how each rule is checked.

## What this does not do

- **Images.** Several rules also govern font size and contrast of a warning inside an advertising image. In chat the
  answer is text, so those clauses cannot fail — the `undefined` verdict the sheet describes for them belongs to an
  asset-review pipeline, not here.
- **Judgement rules are not judged per turn.** Adding them to the answer-time check means an LLM call per answer; today
  they are enforced in the prompt and surfaced as "to review" on the turn.
- **Product detection is lexical.** A question that circles a loan without naming it ("ผ่อนบ้านยังไงดี") may not match
  a family. The specialist's own family is used as a fallback, which is why loan families should get their own skill.
