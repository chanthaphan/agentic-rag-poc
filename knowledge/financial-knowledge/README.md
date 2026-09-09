# knowledge/financial-knowledge

Knowledge space **Financial Knowledge Sharing**: drop financial-literacy articles and guides for the
**financial-knowledge** skill here (`.md` or `.pdf`, Thai or English, any sub-folder structure).

Optional per-document metadata: YAML frontmatter in a `.md` file, or a sibling `doc.yaml` for PDFs:

```yaml
title: ชื่อบทความ
source_url: https://www.bangkokbank.com/...
doc_type: article   # article | guide | faq | pdf
```

Then run `uv run bankrag ingest --category financial-knowledge` or use Studio > Knowledge. README files are ignored by the ingester.
