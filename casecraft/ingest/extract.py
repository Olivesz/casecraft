"""PDF text extraction, including repair of broken font encodings.

Most casebooks extract cleanly. Some — the Case Playbook among them — embed
subset fonts with no usable ToUnicode CMap, so every extractor (pypdf, pdfminer,
PyMuPDF alike) returns consistent gibberish:

    "YoX oZn a bXVineVV Velling high-end VhoeV."

That is not random. The glyph ids are offset from ASCII by a constant 29, and
the offset applies to a different letter range depending on the font used:

    regular faces  →  only q–z are displaced   (a–p survive as themselves)
    bold faces     →  the whole alphabet is displaced

Decoding is therefore a per-span operation, which is why this works from
PyMuPDF spans rather than a flat page string.

One ambiguity survives: a genuine capital and a displaced lowercase can land on
the same byte ('Q' is both a real Q and a displaced 'n'). The PDF gives us
nothing to tell them apart — same font object, same flags, same size. So the
last pass is a dictionary repair: if a decoded word isn't a word but restoring
its original first character makes one, restore it. That turns "nuestion" back
into "Question" without a lookup table of special cases.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

SHIFT = 29
BOLD_LO, BOLD_HI = 68, 93       # 'D'..']'  — displaced a–z
PLAIN_LO, PLAIN_HI = 84, 93     # 'T'..']'  — displaced q–z

WORDS_FILE = Path("/usr/share/dict/words")


@lru_cache(maxsize=1)
def _dictionary() -> frozenset[str]:
    if not WORDS_FILE.exists():
        return frozenset()
    return frozenset(w.strip().lower() for w in WORDS_FILE.read_text(errors="ignore").splitlines()
                     if w.strip())


def _is_word(token: str) -> bool:
    d = _dictionary()
    if not d:
        return True                      # no dictionary → trust the decode
    t = token.lower()
    # The macOS words file omits most plurals ("ingredient" but not
    # "ingredients"), which silently blocked repairs on plural words.
    return t in d or (t.endswith("s") and t[:-1] in d)


@dataclass
class Page:
    number: int
    text: str


def _decode_span(text: str, bold: bool) -> list[tuple[str, str]]:
    """Return (decoded, original) pairs so the repair pass can undo a guess."""
    lo, hi = (BOLD_LO, BOLD_HI) if bold else (PLAIN_LO, PLAIN_HI)
    out = []
    for ch in text:
        code = ord(ch)
        out.append((chr(code + SHIFT), ch) if lo <= code <= hi else (ch, ch))
    return out


_WORDISH = re.compile(r"[A-Za-z']+")


def _repair(pairs: list[tuple[str, str]]) -> str:
    """Restore genuine capitals that the shift swallowed.

    Only the first character of a word is ever ambiguous in practice, because
    the source styles headings as small caps — capital first letter in an
    intact face, the rest in the broken one.
    """
    decoded = "".join(d for d, _ in pairs)
    result = list(decoded)

    for m in _WORDISH.finditer(decoded):
        start = m.start()
        word = m.group(0)
        if len(word) < 2:
            continue
        original_first = pairs[start][1]
        # Two ways the first letter can be wrong: the shift moved a genuine
        # capital (undo = restore the original), or the word sits in a face our
        # font-detection missed so its capital arrived pre-shifted ("fngredients"
        # for "Ingredients"; undo = shift the first char back by 29).
        candidates = []
        if original_first != word[0] and original_first.isalpha():
            candidates.append(original_first)
        unshifted = chr(ord(word[0]) - SHIFT) if ord(word[0]) - SHIFT > 0 else ""
        if unshifted.isalpha() and unshifted != word[0]:
            candidates.append(unshifted.upper() if _sentence_start(decoded, start)
                              or word[1:2].islower() else unshifted)

        if _is_word(word) and not any(
                c.isupper() and _sentence_start(decoded, start) for c in candidates):
            continue
        for c in candidates:
            if _is_word(c + word[1:]):
                result[start] = c
                break

    return "".join(result)


def _sentence_start(text: str, index: int) -> bool:
    for ch in reversed(text[max(0, index - 4):index]):
        if ch.isspace():
            continue
        return ch in ".?!:;•"
    return True


def extract(path: str | Path, *, repair_encoding: bool | None = None) -> list[Page]:
    """Extract pages as text, repairing broken font encodings when detected.

    `repair_encoding=None` auto-detects: a page whose text is mostly displaced
    characters gets repaired, a normal page is passed through untouched.
    """
    import fitz

    doc = fitz.open(str(path))
    pages: list[Page] = []

    for index, page in enumerate(doc):
        # Line structure has to survive: the repair pass keys off word starts,
        # and spans joined edge-to-edge fuse the last word of one line onto the
        # first of the next ("...profit" + "Question 2" -> "profitQuestion 2").
        lines: list[list[tuple[str, bool]]] = [
            [(span["text"], "bold" in span["font"].lower() or "black" in span["font"].lower())
             for span in line["spans"]]
            for block in page.get_text("dict")["blocks"] if block.get("lines")
            for line in block["lines"]
        ]
        raw = "\n".join("".join(t for t, _ in line) for line in lines)

        needs = repair_encoding
        if needs is None:
            needs = _looks_displaced(raw)

        if needs:
            pairs: list[tuple[str, str]] = []
            for line in lines:
                for text, bold in line:
                    pairs.extend(_decode_span(text, bold))
                pairs.append(("\n", "\n"))
            text = _repair(pairs)
        else:
            text = page.get_text()

        # Punctuation outside the shifted ranges maps to stray symbols; the
        # curly apostrophe lands on the pilcrow ("friend¶s").
        text = text.replace("\u00b6", "\u2019")
        pages.append(Page(index + 1, text))

    doc.close()
    return pages


def _looks_displaced(raw: str) -> bool:
    """Heuristic: real English prose doesn't run 15% capitals mid-word.

    Displaced text is full of V, W, X, U where s, t, u, r belong, so the
    give-away is uppercase letters appearing *inside* words at a rate no normal
    document reaches.
    """
    inner_caps = len(re.findall(r"[a-z][A-Z\[\]\\]", raw))
    letters = len(re.findall(r"[A-Za-z]", raw))
    return letters > 200 and inner_caps / letters > 0.02


def full_text(path: str | Path, *, repair_encoding: bool | None = None) -> str:
    """All pages joined, with page markers the parsers can anchor on."""
    return "".join(f"\n<<<PAGE {p.number}>>>\n{p.text}" for p in extract(path, repair_encoding=repair_encoding))
