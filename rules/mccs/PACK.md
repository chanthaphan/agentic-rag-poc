---
id: mccs
name: Responsible Lending (MCCS subset)
description: ชุดกฎย่อจาก MCCS (Media Compliance Checker System) ของธนาคารกรุงเทพ ซึ่งเป็นระบบตรวจสื่อโฆษณาให้เป็นไปตาม หลักการให้สินเชื่ออย่างรับผิดชอบ (Responsible Lending) — POC นี้นำกฎบางส่วนมาใช้กับ "คำตอบ" ของผู้ช่วยแทนสื่อโฆษณา. A subset of Bangkok Bank's Media Compliance Checker System, applied to the assistant's answers instead of marketing media.
sources:
- ประกาศธนาคารแห่งประเทศไทยที่ 3/2568 (Market Conduct) เอกสารแนบ 2
- ประกาศสำนักงาน กลต. ที่ สธ. 10/2558 และประกาศเพิ่มเติม
promotion:
  signals:
  - แนะนำ\s*(ให้\s*)?(เป็น\s*)?(บัตร|สินเชื่อ|ผลิตภัณฑ์|ตัว)
  - (เหมาะ|ตอบโจทย์|คุ้ม)(กับ|สำหรับ|มาก)
  - น่าสนใจ|ตัวเลือกที่ดี|ควรเลือก|ขอเสนอ
  - (?i)\b(recommend\w*|suits?|suitable|ideal|perfect|best (for|option|choice)|good choice|we offer|here are)\b
  - สมัคร|ยื่นกู้|ขอสินเชื่อ|เปิดวงเงิน
  - (?i)\b(apply|applying|sign up)\b
  - ดอกเบี้ย[^\n]{0,40}\d
  - \d+(\.\d+)?\s*%
  - (ค่าธรรมเนียม|วงเงิน|รายได้ขั้นต่ำ|ผ่อน|ค่างวด)[^\n]{0,30}\d
  - ฟรีค่า|ยกเว้นค่า|โปรโมชัน|สิทธิประโยชน์|คะแนนสะสม|เครดิตเงินคืน|เงินคืน
  - (?i)\b(cashback|cash back|reward points?|annual fees?|interest rates?|credit limits?|instal?lments?)\b
  - (?i)\b(income requirements?|minimum income|privileges?|benefits?|lounge|miles|points)\b
  - คุณสมบัติผู้สมัคร|เอกสารที่ใช้สมัคร
  - เปรียบเทียบ|ดีกว่า|เหนือกว่า
  - (?i)\bcompared?\b
  exclude:
  - ยังไม่มีรายละเอียด|ไม่มีรายละเอียดให้แนะนำ|ยังไม่มีข้อมูล|ไม่มีข้อมูล
  - (?i)(don't|do not) have the details|no details (on|about) that
products:
- id: home-loan
  name: สินเชื่อบ้าน
  label: สินเชื่อบ้าน
  label_en: Home loan
  aliases:
  - สินเชื่อที่อยู่อาศัย
  - สินเชื่อบ้านบัวหลวง
  - home loan
  - housing loan
  - mortgage
  skills:
  - general
  match:
  - สินเชื่อ\s*(บ้าน|ที่อยู่อาศัย)
  - (?i)\b(home|housing)\s+loans?\b
  - (?i)\bmortgage\b
- id: personal-loan-unsecured
  name: สินเชื่อส่วนบุคคลไม่มีหลักประกัน
  label: สินเชื่อส่วนบุคคล
  label_en: Personal loan
  aliases:
  - สินเชื่อส่วนบุคคล
  - สินเชื่อบุคคล
  - personal loan
  - unsecured loan
  - บัวหลวงสุขใจ
  skills:
  - general
  match:
  - สินเชื่อ(ส่วน)?บุคคล
  - (?i)\bpersonal\s+loans?\b
  - (?i)\bunsecured\s+loan\b
- id: multipurpose-loan
  name: สินเชื่ออเนกประสงค์
  label: สินเชื่ออเนกประสงค์
  label_en: Multipurpose loan
  aliases:
  - สินเชื่ออเนกประสงค์
  - multipurpose loan
  - สินเชื่อบัวหลวงอเนกประสงค์
  skills:
  - general
  match:
  - สินเชื่ออเนกประสงค์
  - (?i)\bmulti-?purpose\s+loan\b
- id: credit-card-bbl
  name: บัตรเครดิตของธนาคารกรุงเทพ
  label: บัตรเครดิต
  label_en: Credit card
  aliases:
  - บัตรเครดิตธนาคารกรุงเทพ
  - บัตรเครดิตบัวหลวง
  - Bangkok Bank credit card
  - บัตรเครดิต
  skills:
  - credit-card
  match:
  - บัตรเครดิต
  - (?i)\bcredit\s+cards?\b
  - (?i)\b(rudee|be\s?smart|แบ่งชำระ)\b
- id: credit-card-other
  name: บัตรเครดิตที่ไม่ใช่ของธนาคารกรุงเทพ
  label: บัตรเครดิต
  label_en: Credit card
  aliases:
  - บัตรเครดิตธนาคารอื่น
  - other bank credit card
  skills: []
  match:
  - บัตรเครดิต(ของ)?(ธนาคาร)?(กสิกร|ไทยพาณิชย์|กรุงศรี|กรุงไทย|ทีทีบี|ttb|scb|kbank|krungsri)
---

## What this pack is

**MCCS** is Bangkok Bank's *Media Compliance Checker System*: it checks marketing media against the market-conduct
rules below before the media goes out. This pack is a **small subset of the same rules**, exported from MCCS as
`mccs-rules.xlsx` (columns เล่มกฎหมาย / ข้อกฎหมาย / กฎหมาย / กฎสำหรับระบบ / ผลิตภัณฑ์ที่ต้องตรวจสอบ / สถานะ) and applied to what
the assistant writes rather than to an advertisement. One file per rule: the compliance team owns the markdown
sections, engineering owns the frontmatter that says how the rule is applied and checked.

An answer that mentions or recommends one of the product families above counts as advertising under these rules, so
`bankrag` applies them twice: the matching rules are compiled into the agent instructions (concierge and specialists),
and every drafted answer is checked before it reaches the customer — missing mandatory warnings are appended verbatim,
everything else is reported on the turn.

`promotion:` is the sales gate. The rules govern การโฆษณา, so the warnings are attached only to an answer that
offers, recommends or details a product — `signals` say what that looks like, `exclude` are our own "no details yet"
sentences, which only veto when the answer quotes no figures at all. A definition, a refusal or a passing mention gets
no warning; rules marked `trigger: mention` (the prohibited-wording one) still apply to every answer.

`label:` / `label_en:` are the short names the warning block shows the customer ("บัตรเครดิต", "Credit card").
`name:` stays exactly as the MCCS sheet writes it, because that is what an import matches on.

`skills:` maps a product family to the skill agents that answer about it. Loan families point at `general` today;
point them at a dedicated loan skill as soon as one exists and its agent picks the rules up on the next sync.
