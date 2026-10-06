"""Check plain-language limits from ASD-STE100 in the README and key pages.

    python3 scripts/check_ste.py [FILE ...]

Fails when a sentence has more than 25 words (the STE limit for descriptive writing).
Prints passive-looking sentences as warnings; STE prefers the active voice, but the
pattern check cannot tell every case. Code blocks, tables, headings, link targets and
HTML comments are ignored.
"""

import re
import sys
from pathlib import Path

LIMIT = 25
PASSIVE = re.compile(
    r"\b(is|are|was|were|be|been|being)\s+(\w+ed|built|made|shown|run|done|given|kept|sent|set|found|seen|written)\b",
    re.I,
)


def sentences(text):
    text = re.sub(r"```.*?```", "", text, flags=re.S)
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    kept = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(("|", "#", ">")) or not stripped:
            kept.append("\n")
            continue
        kept.append(re.sub(r"^([-*]|\d+\.)\s+", "", stripped))
    blocks = " ".join(kept).split("\n")
    for block in blocks:
        for sentence in re.split(r"(?:(?<=[.!?:])|(?<=[.!?][”\"]))\s+(?=[A-Z`*(“\"])", block):
            if len(sentence.split()) > 2:
                yield sentence.strip()


def main(paths):
    failed = 0
    for path in paths:
        for sentence in sentences(Path(path).read_text(encoding="utf-8")):
            words = len(sentence.split())
            if words > LIMIT:
                failed += 1
                print(f"{path}: {words} words (limit {LIMIT}): {sentence}")
            elif PASSIVE.search(sentence):
                print(f"{path}: note, passive voice?: {sentence}")
    if failed:
        print(f"{failed} sentence(s) over {LIMIT} words. Split them; see the writing guide.")
        return 1
    print(f"Plain-language check passed: {', '.join(paths)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:] or ["README.md"]))
