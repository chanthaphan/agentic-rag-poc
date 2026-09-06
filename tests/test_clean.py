from bankrag.ingest.clean import clean_markdown, detect_language, extract_meta, first_heading

PAGE = """<!-- url: https://www.bangkokbank.com/x -->
<!-- title: บัตรอินฟินิท ธนาคารกรุงเทพ -->

![](https://www.bangkokbank.com/hero.jpg)

### บัตรเครดิต

### บัตรอินฟินิท ธนาคารกรุงเทพ

### บัตรเครดิต

### บัตรอินฟินิท ธนาคารกรุงเทพ

- บัตรเครดิตธนาคารกรุงเทพ
- คำถามที่พบบ่อย
- เครื่องมือช่วยเหลือ

## สิทธิประโยชน์

เข้าเลานจ์ได้ 2 ครั้ง [ดูรายละเอียด](https://www.bangkokbank.com/pdf)

#### เครื่องมือช่วยเหลือ

## ธนาคารพร้อมให้คำปรึกษา

### หนังสือแจ้งการคุ้มครองข้อมูลส่วนบุคคล

ข้อความ PDPA ยาวมาก
"""


def test_clean_strips_footer_images_nav_and_duplicates():
    out = clean_markdown(PAGE)
    assert "PDPA" not in out and "เครื่องมือช่วยเหลือ" not in out and "ธนาคารพร้อมให้คำปรึกษา" not in out
    assert "hero.jpg" not in out
    assert out.count("### บัตรอินฟินิท ธนาคารกรุงเทพ") == 1
    assert "เข้าเลานจ์ได้ 2 ครั้ง" in out
    assert "<!--" not in out


def test_meta_and_language():
    assert extract_meta(PAGE)["url"] == "https://www.bangkokbank.com/x"
    assert first_heading(clean_markdown(PAGE)) == "บัตรเครดิต"
    assert detect_language("บัตรเครดิต ธนาคารกรุงเทพ") == "th"
    assert detect_language("Bangkok Bank credit card") == "en"
