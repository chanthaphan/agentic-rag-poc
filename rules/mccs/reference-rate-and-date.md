---
id: reference-rate-and-date
pack: mccs
title: ต้องแสดงอัตราดอกเบี้ยอ้างอิง (เช่น MRR) พร้อมวันที่ที่ใช้อัตรานั้น
regulation: (MCCS) ประกาศธนาคารแห่งประเทศไทยที่ 3/2568
clause: เอกสารแนบ 2 ข้อ 2.2.1 (1)
products:
- home-loan
- multipurpose-loan
- personal-loan-unsecured
status: active
severity: block
check: required_pattern
enforcement: flag
patterns:
- (?i)\b(MRR|MLR|MOR)\b[^\n]{0,40}ณ\s*วันที่[^\n]{0,30}\d[^\n]{0,20}=?\s*\d+(\.\d+)?\s*%
- (?i)\b(MRR|MLR|MOR)\b[^\n]{0,60}\bas\s+of\b[^\n]{0,30}\d[^\n]{0,20}\d+(\.\d+)?\s*%
applies_when:
- (?i)\b(MRR|MLR|MOR)\b
- ดอกเบี้ย[^\n]{0,40}\d
- (?i)interest[^\n]{0,40}\d
template: ต้องระบุ "อัตราดอกเบี้ย MRR ณ วันที่ {DATE} = {NUMBER}%"
---

## กฎหมาย (legal text)
โฆษณาจะต้องแสดง อัตราดอกเบี้ยอ้างอิง (เช่น MRR) และวันที่ใช้อัตราดอกเบี้ยอ้างอิง

## กฎสำหรับระบบ (system rule)
ต้องระบุ "อัตราดอกเบี้ย MRR ณ วันที่ {DATE} = {NUMBER}%"

## หมายเหตุสำหรับผู้ช่วย (assistant note)
ตรวจอัตโนมัติจากคำตอบ: ถ้าคำตอบระบุตัวเลขอัตราดอกเบี้ย ต้องพบรูปแบบ “อัตราดอกเบี้ย MRR ณ วันที่ {DATE} = {NUMBER}%”
ถ้าไม่ทราบวันที่หรือค่าปัจจุบันของอัตราอ้างอิง ห้ามระบุตัวเลขอัตราดอกเบี้ยในคำตอบ ให้อธิบายผลิตภัณฑ์โดยไม่ใช้ตัวเลข
และแนะนำให้ตรวจสอบอัตราล่าสุดกับธนาคารแทน — ปลอดภัยกว่าการเปิดเผยข้อมูลไม่ครบ
