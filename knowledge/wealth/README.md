# knowledge/wealth

Drop documents for the **wealth** skill here (`.md` or `.pdf`, Thai or English, any sub-folder structure).

Optional per-document metadata: YAML frontmatter in a `.md` file, or a sibling `doc.yaml` for PDFs:

```yaml
title: ชื่อเอกสาร
source_url: https://www.bangkokbank.com/...
product_name: ชื่อผลิตภัณฑ์
doc_type: product-page   # product-page | pdf | promotion | terms
```

Then run `uv run bankrag ingest --category wealth`. README files are ignored by the ingester.
