from bankrag.chat import resolve_citations
from bankrag.models import Citation, Reference

SVC = "https://s.search.windows.net/indexes/bank-products/docs/{id}?$select=title&api-version=2026-08-01-preview"


def test_resolve_citations_from_references_then_lookup():
    cites = [Citation(title="", url=SVC.format(id="a1")), Citation(title="", url=SVC.format(id="b2")), Citation(title="x", url="https://other.example/x")]
    refs = [Reference(id="a1", title="Doc A", source_url="https://www.bangkokbank.com/a")]
    looked = {}

    def lookup(ids):
        looked["ids"] = ids
        return {"b2": {"source_url": "https://www.bangkokbank.com/b", "title": "Doc B"}}

    out = resolve_citations(cites, refs, lookup=lookup)
    assert looked["ids"] == ["b2"]
    assert [(c.title, c.url) for c in out] == [("Doc A", "https://www.bangkokbank.com/a"), ("Doc B", "https://www.bangkokbank.com/b"), ("x", "https://other.example/x")]


def test_resolve_citations_dedupes_same_document():
    cites = [Citation(url=SVC.format(id="a1")), Citation(url=SVC.format(id="a2"))]
    refs = [Reference(id="a1", title="A", source_url="https://www.bangkokbank.com/a"), Reference(id="a2", title="A", source_url="https://www.bangkokbank.com/a")]
    assert len(resolve_citations(cites, refs)) == 1
