"""`python -m casecraft.ingest` — import casebook PDFs into the local library.

    python -m casecraft.ingest FILE.pdf [FILE.pdf ...]
        --format darden|playbook|auto   (default auto)
        --dry-run                       parse and report, write nothing
        --out DIR                       default ~/.casecraft/cases

Imported cases are written to ~/.casecraft/cases/ and are never added to the
distributable bundle. Nothing is uploaded anywhere; parsing is entirely local.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from . import darden, playbook
from .build import USER_CASES, build_case
from .extract import extract


def detect_format(pages) -> str:
    head = "\n".join(p.text for p in pages[:8]).lower()
    if "case playbook" in head or re.search(r"^\s*set\s+\d+\s*$", head, re.M):
        if "set 1 answers" in "\n".join(p.text for p in pages).lower():
            return "playbook"
    if "darden" in head or "casebook" in head:
        return "darden"
    return "darden"


def book_identity(path: Path, pages) -> tuple[str, str]:
    head = "\n".join(p.text for p in pages[:4])
    year = re.search(r"(20\d{2})\s*[-–]\s*(20?\d{2})", head)
    if "darden" in head.lower() and year:
        label = f"Darden {year.group(1)}-{year.group(2)}"
        return label, f"darden{year.group(1)[2:]}"
    stem = re.sub(r"[^a-z0-9]+", "-", path.stem.lower()).strip("-")
    stem = re.sub(r"(copy-of-)+|-copy|-\d+$", "", stem).strip("-")
    return path.stem, stem[:24] or "casebook"


def ingest(path: Path, fmt: str, out_dir: Path, dry_run: bool) -> dict:
    pages = extract(path)
    resolved = detect_format(pages) if fmt == "auto" else fmt
    book, book_id = book_identity(path, pages)

    cases: list[dict] = []
    notes: list[str] = []

    if resolved == "playbook":
        cases, notes = playbook.parse(pages, book=book, book_id=book_id)
    else:
        raw_cases = darden.attach_pages(darden.parse_toc(pages), pages)
        if not raw_cases:
            notes.append(f"{path.name}: no table of contents recognised — nothing to import")
        for raw in raw_cases:
            case, case_notes = build_case(raw, book=book, book_id=book_id)
            notes.extend(case_notes)
            if case:
                cases.append(case)

    written = []
    if not dry_run:
        out_dir.mkdir(parents=True, exist_ok=True)
        for case in cases:
            target = out_dir / f"{case['id']}.json"
            target.write_text(json.dumps(case, indent=2, ensure_ascii=False))
            written.append(target)

    return {
        "file": path.name, "format": resolved, "book": book,
        "cases": cases, "notes": notes, "written": written,
        "questions": sum(len(c["questions"]) for c in cases),
        "needs_review": sum(1 for c in cases for q in c["questions"] if "_review" in q),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="casecraft.ingest")
    ap.add_argument("pdfs", nargs="+", type=Path)
    ap.add_argument("--format", default="auto", choices=["auto", "darden", "playbook"])
    ap.add_argument("--out", type=Path, default=USER_CASES)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args(argv)

    total_cases = total_questions = 0
    all_notes: list[str] = []

    for pdf in args.pdfs:
        if not pdf.exists():
            print(f"  ✗ {pdf} not found", file=sys.stderr)
            continue
        result = ingest(pdf, args.format, args.out, args.dry_run)
        total_cases += len(result["cases"])
        total_questions += result["questions"]
        all_notes.extend(result["notes"])

        print(f"\n{result['file']}")
        print(f"  format:    {result['format']}  ({result['book']})")
        print(f"  cases:     {len(result['cases'])}")
        print(f"  questions: {result['questions']}")
        for case in result["cases"]:
            kinds: dict[str, int] = {}
            for q in case["questions"]:
                kinds[q["type"]] = kinds.get(q["type"], 0) + 1
            summary = " ".join(f"{k}×{v}" for k, v in kinds.items())
            print(f"    · {case['title'][:44]:<46} {summary}")
        if result["notes"] and args.verbose:
            for n in result["notes"]:
                print(f"    ! {n}")

    print("\n" + "─" * 66)
    print(f"total: {total_cases} cases, {total_questions} questions"
          + ("  (dry run — nothing written)" if args.dry_run else f"  → {args.out}"))
    if all_notes and not args.verbose:
        print(f"{len(all_notes)} parser notes — re-run with -v to see them")

    if not args.dry_run and total_cases:
        print("\nEVERY imported question carries a _review flag. Numeric answers were")
        print("inferred from worked solutions and are NOT verified — check them before")
        print("trusting a verdict. Run:  python -m casecraft --check")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
