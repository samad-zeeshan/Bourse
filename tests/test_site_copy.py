"""
Check the demo page text for internal jargon used as labels.

Run with python -m pytest tests/test_site_copy.py. It reads site/index.html and site/app.js as plain text.
"""
import re
from pathlib import Path

SITE = Path(__file__).resolve().parent.parent / "site"

# Phrases a visitor saw before the plain-language pass. Each one means nothing outside the project.
BANNED = [
    "fuzz findings",
    "crossed book",
    "to the power",
    "fitted power",
    "nothing isolated",
    "log scale",
    "unseen days",
    "µs",
]

# The recorder clicks these, so renaming one breaks the demo video.
DEMO_STEPS = ["replay", "timing", "impact", "cheat"]


def page_text():
    return "\n".join((SITE / name).read_text(encoding="utf-8") for name in ("index.html", "app.js"))


def test_no_banned_phrases():
    text = page_text().lower()
    found = [p for p in BANNED if p.lower() in text]
    assert not found, f"internal phrases on the demo page: {found}"


def test_eyebrows_stay_short():
    html = (SITE / "index.html").read_text(encoding="utf-8")
    for label in re.findall(r'<p class="eyebrow"><span>\d+</span>([^<]*)</p>', html):
        assert len(label.split()) <= 3, f"eyebrow over three words: {label!r}"


def test_demo_selectors_present():
    html = (SITE / "index.html").read_text(encoding="utf-8")
    for step in DEMO_STEPS:
        assert f'data-demo="{step}"' in html, f"missing data-demo={step}"


# Plain-language rules for the README and the page. Filler words and long sentences make the text harder for a stranger.
BANNED_WORDS = ["comprehensive", "robust", "seamless", "cutting-edge", "state-of-the-art", "powerful", "leverage",
                "utilize", "ensure", "delve", "showcase", "crucial", "various", "numerous", "simply", "just",
                "really", "basically", "actually"]
MAX_WORDS = 25
ROOT = SITE.parent


def readme_prose():
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    text = re.sub(r"```.*?```", "", text, flags=re.S)
    lines = [l for l in text.splitlines() if not l.lstrip().startswith("|") and not l.startswith("<!--")]
    # Paper titles are proper names in italics, so "Robust Market Making" stays as published.
    lines = [re.sub(r"(?<!\*)\*[^*]+\*(?!\*)", "TITLE", l) for l in lines]
    return "\n".join(lines)


def page_strings():
    html = (SITE / "index.html").read_text(encoding="utf-8")
    html = re.sub(r"<(script|style)\b.*?</\1>", "", html, flags=re.S)
    attrs = re.findall(r'(?:content|alt|aria-label|title)="([^"]*)"', html)
    body = re.sub(r"<[^>]+>", "\n", html)
    # Only string literals in app.js reach the reader. Template holes become a placeholder word.
    js = (SITE / "app.js").read_text(encoding="utf-8")
    js = re.sub(r"^\s*//.*$", "", js, flags=re.M)
    lits = re.findall(r'"((?:[^"\\\n]|\\.)*)"|`([^`]*)`', js)
    lits = [re.sub(r"\$\{[^}]*\}", "N", a or b) for a, b in lits]
    return "\n".join([body, *attrs, *lits])


def sentences(text):
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"<[^>]+>", " ", text)
    return [s for s in re.split(r"(?<=[.!?])\s+|\n|·", text) if s.strip()]


def test_sentences_stay_short():
    for name, text in (("README.md", readme_prose()), ("site", page_strings())):
        long = [s.strip() for s in sentences(text) if len(re.findall(r"\w[\w'.,%-]*", s)) > MAX_WORDS]
        assert not long, f"{name}: sentences over {MAX_WORDS} words: {long}"


def test_no_filler_words():
    for name, text in (("README.md", readme_prose()), ("site", page_strings())):
        found = sorted({w for w in BANNED_WORDS if re.search(rf"\b{re.escape(w)}\b", text, re.I)})
        assert not found, f"{name}: filler words: {found}"


def test_no_em_dash_or_semicolon_in_prose():
    for name, text in (("README.md", readme_prose()), ("site", page_strings())):
        bad = [s.strip() for s in sentences(text) if "—" in s or ";" in s]
        assert not bad, f"{name}: em dash or semicolon: {bad}"


# A form of "be" followed within one word by a past participle. It is rough, so real exceptions go in PASSIVE_OK.
PASSIVE = re.compile(r"\b(?:is|are|was|were|be|been|being)\s+(?:\w+\s+)?(?:\w+ed|made|done|found|given|held|kept|left|"
                     r"run|seen|sent|shown|taken|written|built|caught)\b", re.I)
# Nothing needs an exception today. Add each full sentence here with a one-line reason comment.
PASSIVE_OK: set[str] = set()


def test_no_passive_voice():
    for name, text in (("README.md", readme_prose()), ("site", page_strings())):
        bad = [s.strip() for s in sentences(text) if PASSIVE.search(s) and s.strip() not in PASSIVE_OK]
        assert not bad, f"{name}: passive voice: {bad}"
