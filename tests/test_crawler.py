from bankrag.ingest.crawler import html_to_markdown, in_scope, slug_for

HTML = """<html><head><title>บัตรเดบิต บีเฟิสต์ สมาร์ท | Bangkok Bank</title></head>
<body><header>menu</header><nav><a href="/th-TH/Personal">Personal</a></nav>
<main><h1>Be1st Smart</h1><p>ค่าธรรมเนียมรายปี <b>200</b> บาท</p>
<a href="/th-TH/Personal/Cards/Debit-Cards/Be1st-Smart-TPN">TPN</a>
<a href="https://www.bangkokbank.com/-/media/files/be1st.pdf">brochure</a>
<a href="https://other.example/x">out</a><a href="#top">top</a></main><footer>foot</footer></body></html>"""


def test_html_to_markdown_extracts_main_and_links():
    md, title, links = html_to_markdown(HTML, "https://www.bangkokbank.com/th-TH/Personal/Cards/Debit-Cards/Be1st-Smart")
    assert title.startswith("บัตรเดบิต") and "# Be1st Smart" in md and "**200**" in md and "menu" not in md and "foot" not in md
    assert "https://www.bangkokbank.com/th-TH/Personal/Cards/Debit-Cards/Be1st-Smart-TPN" in links
    assert "https://www.bangkokbank.com/-/media/files/be1st.pdf" in links and "https://other.example/x" in links
    assert not any(l.endswith("#top") for l in links)


def test_scope_and_slug():
    prefix = ["https://www.bangkokbank.com/th-TH/Personal/Cards/Debit-Cards"]
    assert in_scope("https://www.bangkokbank.com/th-TH/Personal/Cards/Debit-Cards/Be1st-Smart", prefix)
    assert not in_scope("https://www.bangkokbank.com/th-TH/Personal/Cards/Credit-Cards", prefix)
    assert slug_for("https://www.bangkokbank.com/th-TH/Personal/Cards/Debit-Cards/Be1st-Smart") == "debit-cards-be1st-smart"
