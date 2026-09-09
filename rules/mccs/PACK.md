---
id: mccs
name: Responsible Lending (MCCS)
description: >-
  กฎการสื่อสารการตลาดผลิตภัณฑ์สินเชื่อและบัตรเครดิตตามหลักการให้สินเชื่ออย่างรับผิดชอบ (Responsible Lending)
  ที่คำตอบของผู้ช่วยต้องปฏิบัติตามก่อนพูดถึงผลิตภัณฑ์สินเชื่อ. Market-conduct rules the assistant's answers must
  comply with before they mention a loan or a credit card.
sources:
  - ประกาศธนาคารแห่งประเทศไทยที่ 3/2568 (Market Conduct) เอกสารแนบ 2
  - ประกาศสำนักงาน กลต. ที่ สธ. 10/2558 และประกาศเพิ่มเติม
products:
  - id: home-loan
    name: สินเชื่อบ้าน
    aliases: [สินเชื่อที่อยู่อาศัย, สินเชื่อบ้านบัวหลวง, home loan, housing loan, mortgage]
    skills: [general]
    match: ['สินเชื่อ\s*(บ้าน|ที่อยู่อาศัย)', '(?i)\b(home|housing)\s+loan\b', '(?i)\bmortgage\b']
  - id: personal-loan-unsecured
    name: สินเชื่อส่วนบุคคลไม่มีหลักประกัน
    aliases: [สินเชื่อส่วนบุคคล, สินเชื่อบุคคล, personal loan, unsecured loan, บัวหลวงสุขใจ]
    skills: [general]
    match: ['สินเชื่อ(ส่วน)?บุคคล', '(?i)\bpersonal\s+loan\b', '(?i)\bunsecured\s+loan\b']
  - id: multipurpose-loan
    name: สินเชื่ออเนกประสงค์
    aliases: [สินเชื่ออเนกประสงค์, multipurpose loan, สินเชื่อบัวหลวงอเนกประสงค์]
    skills: [general]
    match: ['สินเชื่ออเนกประสงค์', '(?i)\bmulti-?purpose\s+loan\b']
  - id: credit-card-bbl
    name: บัตรเครดิตของธนาคารกรุงเทพ
    aliases: [บัตรเครดิตธนาคารกรุงเทพ, บัตรเครดิตบัวหลวง, Bangkok Bank credit card, บัตรเครดิต]
    skills: [credit-card]
    match: ['บัตรเครดิต', '(?i)\bcredit\s+card\b', '(?i)\b(rudee|be\s?smart|แบ่งชำระ)\b']
  - id: credit-card-other
    name: บัตรเครดิตที่ไม่ใช่ของธนาคารกรุงเทพ
    aliases: [บัตรเครดิตธนาคารอื่น, other bank credit card]
    skills: []
    match: ['บัตรเครดิต(ของ)?(ธนาคาร)?(กสิกร|ไทยพาณิชย์|กรุงศรี|กรุงไทย|ทีทีบี|ttb|scb|kbank|krungsri)']
---
## What this pack is

The rows of `mccs-rules.xlsx` (columns เล่มกฎหมาย / ข้อกฎหมาย / กฎหมาย / กฎสำหรับระบบ / ผลิตภัณฑ์ที่ต้องตรวจสอบ / สถานะ),
one file per rule. The compliance team owns the two markdown sections of each rule; engineering owns the frontmatter
that says how the rule is applied and checked.

An answer that mentions or recommends one of the product families above counts as advertising under these rules, so
`bankrag` applies them twice: the matching rules are compiled into the agent instructions (concierge and specialists),
and every drafted answer is checked before it reaches the customer — missing mandatory warnings are appended verbatim,
everything else is reported on the turn.

`skills:` maps a product family to the skill agents that answer about it. Loan families point at `general` today;
point them at a dedicated loan skill as soon as one exists and its agent picks the rules up on the next sync.
