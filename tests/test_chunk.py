from bankrag.ingest.chunk import chunk_markdown, count_tokens, _hard_split

THAI = "บัตรอินฟินิท ธนาคารกรุงเทพ ให้สิทธิ์เข้าใช้บริการห้องรับรองพิเศษ ณ สนามบินทั้งในและต่างประเทศ "


def test_chunks_have_breadcrumb_and_respect_limit():
    text = "## สิทธิประโยชน์\n\n" + "\n\n".join([THAI * 6] * 12) + "\n\n### เอกสิทธิ์ด้านการเดินทาง\n\n" + THAI * 3
    chunks = chunk_markdown(text, "บัตรอินฟินิท", target=200, max_tokens=300)
    assert len(chunks) > 2
    for c in chunks:
        assert c["content"].startswith("บัตรอินฟินิท > สิทธิประโยชน์") or c["content"].startswith("บัตรอินฟินิท > สิทธิประโยชน์ > เอกสิทธิ์")
        assert count_tokens(c["content"]) <= 300
    assert [c["chunk_index"] for c in chunks] == list(range(len(chunks)))


def test_hard_split_never_starts_with_combining_mark():
    text = ("ประกันการเดินทางคุ้มครองสูงสุด" * 200)
    pieces = _hard_split(text, 120)
    assert len(pieces) > 1
    for p in pieces:
        assert count_tokens(p) <= 120
        assert not ("ั" <= p[0] <= "ฺ" or "็" <= p[0] <= "๎")
    assert "".join(pieces).replace(" ", "") == text.replace(" ", "")
