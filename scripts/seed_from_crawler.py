#!/usr/bin/env python
"""Thin wrapper: uv run python scripts/seed_from_crawler.py [--from DIR] [--include-promotions] [--clear]"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from bankrag.seed import DEFAULT_CRAWLER_DATA, seed_credit_cards  # noqa: E402

p = argparse.ArgumentParser()
p.add_argument("--from", dest="src", default=str(DEFAULT_CRAWLER_DATA))
p.add_argument("--knowledge", default="knowledge")
p.add_argument("--include-promotions", action="store_true")
p.add_argument("--clear", action="store_true")
a = p.parse_args()
seed_credit_cards(Path(a.src), Path(a.knowledge), include_promotions=a.include_promotions, clear=a.clear)
