"""Casebook ingestion: PDF → casecraft case JSON.

Output always lands in ~/.casecraft/cases/, never in the repo's data/ directory.
That separation is deliberate and load-bearing: bundled cases are originals we
can distribute, imported ones are derived from copyrighted casebooks and must
stay on the machine that made them.
"""

from .build import USER_CASES, build_case
from .extract import extract, full_text

__all__ = ["extract", "full_text", "build_case", "USER_CASES"]
