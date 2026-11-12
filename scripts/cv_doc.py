#!/usr/bin/env python
"""The CV as structured text, in the one layout the editor writes.

A CV is a name, a few headline lines under it, and sections. A section has a
title, its own lines, and entries, and an entry is a heading with lines of its
own. A line is a paragraph line or a bullet. The four standard sections are
always there, Summary, Experience, Skills, and Education and certifications,
and a CV brought in from Word may carry others after them.

The text is markdown and is the one thing anything reads:

    # Luke Keogh
    Software leader in regulated medical devices
    <blank>
    ## Summary
    A paragraph line.
    <blank>
    ## Experience
    <blank>
    ### Software Director, Axithra, 2023 to now
    - A bullet.

render(doc) is the text, and the text is what the scorer numbers with
cv_store.cv_lines and cv_store.numbered_cv, what the page numbers, and what a
working cites. Nothing sits between the editor and the scorer that could
number a line differently: the page shows line n of render(doc), the scorer
is shown [n] before the same line, and a working cites n with that line's
text. parse(render(doc)) is doc again for any doc normalize has made, and
render(parse(text)) is text again for any text render made.

A text the editor did not write, a version imported from Word say, is read by
import_text, which knows the usual words for the sections and the usual bullet
marks, and what it gives is laid out the editor's way the moment it is saved.

The same rules are written once more in JavaScript, in build_dashboard.CV_MODEL_JS,
because the page numbers lines while they are typed. test/run_cv_test.py runs
both on the same inputs and holds them to the same answers.

Standard library only.
"""

from __future__ import annotations

import difflib
import re
import unicodedata

STANDARD_SECTIONS = ("Summary", "Experience", "Skills",
                     "Education and certifications")

CV_TEXT_MAX = 20_000     # characters of CV text, about five pages
CV_LINES_MAX = 400       # lines of it, blank ones included
CV_LINE_MAX = 1_000      # characters on one line
RAW_TEXT_MAX = 200_000   # what a request may carry before it is read at all
KEY_CHARS = 200          # characters of a line a cited line is found again by

OWNER = "Luke Keogh"     # the name in "CV - Luke Keogh - <Company> - <Job title>"
NAME_MAX = 150           # characters in a saved copy's name, before .docx
COMPANY_PART_MAX = 50
TITLE_PART_MAX = 79

# What a company is called when the advert does not say who it is. A saved copy
# of such a role is named after the job title alone.
UNKNOWN_COMPANIES = frozenset((
    "", "unknown", "unknown company", "n/a", "na", "none", "not stated",
    "not specified", "not disclosed", "confidential", "confidential company",
    "company confidential", "undisclosed", "undisclosed company"))

# Characters a file name cannot hold on Windows, or that a header would have to
# escape. They become spaces.
_NAME_BAD = re.compile(r'[<>:"/\\|?*]')

# The usual words for the four sections, in a CV that was not written here.
_ALIASES = {
    "Summary": ("summary", "profile", "professional summary", "personal summary",
                "personal statement", "personal profile", "professional profile",
                "career summary", "executive summary", "profile summary",
                "about me", "overview", "career profile"),
    "Experience": ("experience", "professional experience", "work experience",
                   "employment", "employment history", "career history",
                   "work history", "career", "relevant experience",
                   "experience and achievements", "roles"),
    "Skills": ("skills", "key skills", "core skills", "technical skills",
               "competencies", "core competencies", "key competencies",
               "areas of expertise", "expertise", "skills and expertise",
               "skills and competencies"),
    "Education and certifications": (
        "education and certifications", "education and certification",
        "education", "certifications", "certification", "qualifications",
        "education and qualifications", "education and training",
        "training", "professional development", "certifications and training",
        "qualifications and certifications", "professional qualifications",
        "accreditations", "education and accreditations"),
}
_ALIAS = {alias: title for title, words in _ALIASES.items() for alias in words}

# A bullet in a CV that was not written here. The editor writes "- " only.
_ANY_BULLET = re.compile(r"^([-*•+·–—▪◦‣●○■□➢➤✓✔♦◆»])\s+")
# A heading in a CV that was not written here, a Word heading of any depth.
_ANY_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
# A heading as the editor writes one. Greedy, so "### x" is level three.
_HEADING = re.compile(r"^(#{1,3}) (.*)$")
# What a layout adds to the front of a line, for finding a cited line again.
_LINE_MARK = re.compile(r"^(#{1,6}\s+|[-*•+·–—▪◦‣●○■□➢➤✓✔♦◆»]\s+)")
_PLAIN_HEADING = re.compile(r"^#{1,3} ")


# The byte order mark and the two non-characters, which a Word file cannot
# hold and a browser may treat as white space.
_DROPPED = frozenset((chr(0xFEFF), chr(0xFFFE), chr(0xFFFF)))


class CvError(ValueError):
    """A CV that cannot be taken, in words worth showing as they are."""


# ------------------------------------------------------------- cleaning

def clean(value) -> str:
    """One line of text as the CV holds it.

    NFC, every control character and line or paragraph separator a space,
    lone surrogates, the byte order mark and the two non-characters dropped,
    white space run together, the ends trimmed. What is left is one line that
    a Word file can hold and that reads back the same.
    """
    text = unicodedata.normalize("NFC", str(value or ""))
    out = []
    for ch in text:
        cat = unicodedata.category(ch)
        if cat == "Cs" or ch in _DROPPED:
            continue
        out.append(" " if cat in ("Cc", "Zl", "Zp") else ch)
    return " ".join("".join(out).split())


def clean_plain(value, allow_bullet: bool = True) -> str:
    """A paragraph line, which must not start like a heading.

    A paragraph line has no mark of its own, so one that began "## " would be
    read back as a heading. Such a start is taken off. Where a bullet is not
    allowed, a headline line, a "- " start is taken off as well. Where it is,
    the caller reads a "- " start as a bullet.
    """
    text = clean(value)
    while True:
        found = _PLAIN_HEADING.match(text)
        if found:
            text = clean(text[found.end():])
            continue
        if not allow_bullet and text.startswith("- "):
            text = clean(text[2:])
            continue
        return text


def _list(value) -> list:
    """A list as a list, anything else as nothing."""
    return value if isinstance(value, list) else []


def _line(item) -> dict:
    """One line of a section or an entry, cleaned. A paragraph line that
    starts "- " is a bullet, which is how it reads back anyway."""
    if not isinstance(item, dict):
        item = {"b": False, "t": item}
    if item.get("b"):
        return {"b": True, "t": clean(item.get("t"))}
    text = clean_plain(item.get("t"))
    if text.startswith("- "):
        return {"b": True, "t": clean(text[2:])}
    return {"b": False, "t": text}


# ------------------------------------------------------ the layout itself

def blank_doc() -> dict:
    return {"name": "", "headline": [],
            "sections": [{"title": t, "lines": [], "entries": []}
                         for t in STANDARD_SECTIONS]}


def render_lines(doc: dict) -> list[str]:
    """The lines of the CV text, blank ones included, in order.

    A section with nothing in it is left out, so an empty standard section
    puts no bare heading in the text, the scorer's view or the Word file. The
    editor still shows it, to be filled. normalize drops any other empty
    section, so only a standard one is ever skipped here.
    """
    out = ["# " + doc.get("name", "")]
    out += list(doc.get("headline") or [])
    for sec in doc.get("sections") or []:
        if not (sec.get("lines") or sec.get("entries")):
            continue
        out += ["", "## " + sec.get("title", "")]
        out += [("- " if it.get("b") else "") + it.get("t", "")
                for it in sec.get("lines") or []]
        for entry in sec.get("entries") or []:
            out += ["", "### " + entry.get("heading", "")]
            out += [("- " if it.get("b") else "") + it.get("t", "")
                    for it in entry.get("lines") or []]
    return out


def render(doc: dict) -> str:
    """The CV text. One line feed after every line, the last included."""
    return "\n".join(render_lines(doc)) + "\n"


def _lines_of(text) -> list[str]:
    # cv_store.cv_lines, without importing it here: a line break is a line
    # feed, a carriage return and line feed, or a lone carriage return, and a
    # final one does not make another line. The tests hold the two together.
    text = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def parse(text) -> dict:
    """The doc a text in the editor's layout describes. Strict, no guessing.

    Every text gives a doc. Only a text render wrote gives one that renders
    back to the same text, and load_doc uses that to tell the two apart.
    Blank lines are not kept, render puts them back. A line before the first
    section heading is a headline line, whatever it looks like, so that it
    renders back as it was.
    """
    doc = {"name": "", "headline": [], "sections": []}
    sec = entry = None
    named = False
    for raw in _lines_of(text):
        if raw == "":
            continue
        found = _HEADING.match(raw)
        level = len(found.group(1)) if found else 0
        if level == 1 and not named and sec is None and not doc["headline"]:
            doc["name"], named = found.group(2), True
            continue
        if not named and sec is None and not doc["headline"]:
            # The first line is not the name, so this text is not in the
            # layout. It still has to land somewhere.
            named = True
        if level in (1, 2):
            sec = {"title": found.group(2), "lines": [], "entries": []}
            doc["sections"].append(sec)
            entry = None
            continue
        if sec is None:
            doc["headline"].append(raw)
            continue
        if level == 3:
            entry = {"heading": found.group(2), "lines": []}
            sec["entries"].append(entry)
            continue
        item = ({"b": True, "t": raw[2:]} if raw.startswith("- ")
                else {"b": False, "t": raw})
        (entry["lines"] if entry is not None else sec["lines"]).append(item)
    return doc


def normalize(doc: dict) -> dict:
    """A doc made tidy: every string cleaned, empty lines dropped, and the
    four standard sections present.

    An entry with no heading and no lines is dropped. One with lines and no
    heading is kept for validate to refuse, because guessing where its lines
    belong would be guessing. A missing standard section is added, empty, at
    the end. Nothing else moves.
    """
    out = {"name": clean(doc.get("name")), "headline": [], "sections": []}
    out["headline"] = [t for t in (clean_plain(h, allow_bullet=False)
                                   for h in _list(doc.get("headline"))) if t]
    for sec in _list(doc.get("sections")):
        if not isinstance(sec, dict):
            continue
        lines = [it for it in (_line(i) for i in _list(sec.get("lines"))) if it["t"]]
        entries = []
        for entry in _list(sec.get("entries")):
            if not isinstance(entry, dict):
                continue
            heading = clean(entry.get("heading"))
            elines = [it for it in (_line(i) for i in _list(entry.get("lines")))
                      if it["t"]]
            if heading or elines:
                entries.append({"heading": heading, "lines": elines})
        title = clean(sec.get("title"))
        if title in STANDARD_SECTIONS or lines or entries:
            out["sections"].append({"title": title, "lines": lines,
                                    "entries": entries})
    for title in STANDARD_SECTIONS:
        if all(sec["title"] != title for sec in out["sections"]):
            out["sections"].insert(_standard_place(out["sections"], title),
                                   {"title": title, "lines": [], "entries": []})
    return out


def _standard_place(sections: list, title: str) -> int:
    """Where a missing standard section goes: after the last standard section
    that comes before it in the usual order, else before the first that comes
    after it, else at the end. Only the editor sees where, because an empty
    section is not in the text."""
    rank = STANDARD_SECTIONS.index(title)
    ranks = [STANDARD_SECTIONS.index(s["title"]) if s["title"] in STANDARD_SECTIONS
             else None for s in sections]
    earlier = [i for i, r in enumerate(ranks) if r is not None and r < rank]
    if earlier:
        return earlier[-1] + 1
    later = [i for i, r in enumerate(ranks) if r is not None and r > rank]
    return later[0] if later else len(sections)


def validate(doc: dict) -> str:
    """The text of a tidy doc, or CvError saying what is wrong with it."""
    if not doc.get("name"):
        raise CvError("The CV has no name on its first line.")
    for sec in doc["sections"]:
        if not sec["title"]:
            raise CvError("A section has no title.")
        for entry in sec["entries"]:
            if not entry["heading"]:
                raise CvError(f"An entry in {sec['title']} has lines but no "
                              "heading. Give it one, or delete its lines.")
    lines = render_lines(doc)
    text = "\n".join(lines) + "\n"
    if len(lines) > CV_LINES_MAX:
        raise CvError(f"The CV runs to {len(lines)} lines, and the most it may "
                      f"have is {CV_LINES_MAX}.")
    long = max(lines, key=len)
    if len(long) > CV_LINE_MAX:
        raise CvError(f"One line is {len(long):,} characters long, and the "
                      f"most a line may have is {CV_LINE_MAX:,}: "
                      f"{long[:60]}...")
    if len(text) > CV_TEXT_MAX:
        raise CvError(f"The CV is {len(text):,} characters long, and the most "
                      f"it may be is {CV_TEXT_MAX:,}.")
    return text


# ------------------------------------------------- reading another layout

def section_title(words) -> str | None:
    """The standard section a heading names, by the usual words for it."""
    text = clean(words).casefold().replace("&", " and ")
    key = " ".join(re.sub(r"[^\w\s]", " ", text).split())
    return _ALIAS.get(key)


def _bare_heading(line: str) -> str | None:
    """The title a line on its own gives, when a Word CV wrote its section
    headings as plain text: the usual word for a section, or a short line in
    capitals that does not end a sentence. None for any other line, so an
    abbreviation that ends a wrapped sentence, "SSE.", is not a heading."""
    words = line.rstrip(":").strip()
    if len(words) > 60:
        return None
    if section_title(words):
        return words
    letters = [ch for ch in words if ch.isalpha()]
    if (len(letters) >= 4 and len(words) <= 40 and words == words.upper()
            and not words.endswith(".")
            and not any(ch.isdigit() for ch in words)):
        return words.capitalize()
    return None


def _section_for(doc: dict, title: str) -> dict:
    """The section to add to. A standard one is found again if it is there
    already, so Education and Certifications become one section."""
    if title in STANDARD_SECTIONS:
        for sec in doc["sections"]:
            if sec["title"] == title:
                return sec
    sec = {"title": title, "lines": [], "entries": []}
    doc["sections"].append(sec)
    return sec


def import_text(text) -> dict:
    """A doc for any text, the way a person would read the layout.

    For a CV the editor did not write, a version imported from Word or a
    hand-made cv.txt. The first heading or line is the name, the lines before
    the first section are the headline, a heading or a line on its own that
    is the usual word for a section starts that section, deeper headings start
    entries, and the usual bullet marks are bullets. What comes out is
    normalized, and is shown to be checked before it is saved.
    """
    doc = {"name": "", "headline": [], "sections": []}
    sec = entry = None
    for raw in _lines_of(text):
        line = clean(raw)
        if not line:
            continue
        level, content = 0, line
        found = _ANY_HEADING.match(line)
        if found:
            level, content = len(found.group(1)), clean(found.group(2))
            if not content:
                continue
        elif doc["name"] and _bare_heading(line):
            level, content = 2, _bare_heading(line)
        if level == 1 and not doc["name"] and sec is None:
            doc["name"] = content
            continue
        if level and level <= 2:
            sec = _section_for(doc, section_title(content) or content)
            entry = None
            continue
        if level:
            if sec is None:
                sec = _section_for(doc, "Experience")
            entry = {"heading": content, "lines": []}
            sec["entries"].append(entry)
            continue
        bullet = _ANY_BULLET.match(line)
        body = clean(line[bullet.end():]) if bullet else line
        if not doc["name"] and sec is None:
            doc["name"] = body
            continue
        if sec is None:
            doc["headline"].append(body)
            continue
        item = {"b": bool(bullet), "t": body}
        (entry["lines"] if entry is not None else sec["lines"]).append(item)
    return normalize(doc)


def is_layout(text) -> bool:
    """Was this text written in the editor's layout, to the byte."""
    return render(parse(text)) == "\n".join(_lines_of(text)) + "\n"


def load_doc(text) -> dict:
    """The doc for a stored CV. Strict for the editor's own layout, so a CV
    saved here reads back exactly, and import_text for anything else."""
    if is_layout(text):
        return normalize(parse(text))
    return import_text(text)


def accept(text) -> tuple[dict, str]:
    """A CV as a request sent it, made tidy and checked. (doc, text).

    The text is what will be scored, saved or downloaded. CvError says why a
    CV cannot be taken, before anything is written or paid for.
    """
    if not isinstance(text, str):
        raise CvError("The CV must be sent as text.")
    if len(text) > RAW_TEXT_MAX:
        raise CvError(f"The CV is over {RAW_TEXT_MAX:,} characters, which no "
                      "CV needs to be.")
    doc = load_doc(text)
    return doc, validate(doc)


# --------------------------------------------------- the saved copy's name

def _name_part(value, limit: int) -> str:
    text = clean(_NAME_BAD.sub(" ", clean(value)))
    if len(text) > limit:
        cut = text[:limit + 1]
        space = cut.rfind(" ")
        text = (cut[:space] if space > limit // 2 else text[:limit]).strip()
    return text.strip(" .-")


def company_known(company) -> bool:
    return (clean(company).casefold().strip(" .")
            not in UNKNOWN_COMPANIES)


def saved_name(company, title) -> str:
    """What a saved copy is called, safe to be a file name.

    "CV - Luke Keogh - <Company> - <Job title>", or without the company where
    the advert does not say who it is. Characters a file name cannot hold
    become spaces, the parts are cut to fit on a word, and the name never
    ends in a dot or a space.
    """
    parts = ["CV", OWNER]
    firm = _name_part(company, COMPANY_PART_MAX) if company_known(company) else ""
    if firm:
        parts.append(firm)
    parts.append(_name_part(title, TITLE_PART_MAX) or "Untitled role")
    return " - ".join(parts)[:NAME_MAX].rstrip(" .-")


# ------------------------------------------- finding a cited line again

def line_key(line) -> str:
    """How a cited CV line is found again in a copy that has been edited.

    Its words, without the mark a layout puts in front of a line, so a bullet
    written "* " in an imported master is the same line as "- " in the copy
    made from it, and only the first KEY_CHARS characters, because a working
    keeps 240 of a cited line and the white space in those may not survive.
    """
    text = _LINE_MARK.sub("", clean(line), count=1)
    return clean(text)[:KEY_CHARS].rstrip()


def find_line(cited: dict, lines: list[str]) -> int | None:
    """The number of the line a cited line is now, or None if it has gone.

    Its own number if the text there is still the text cited, else the
    nearest line with that text, else None. lines are numbered from one.
    """
    key = line_key(cited.get("text"))
    if not key:
        return None
    n = cited.get("n")
    if isinstance(n, int) and 1 <= n <= len(lines) and line_key(lines[n - 1]) == key:
        return n
    hits = [i for i, line in enumerate(lines, 1) if line_key(line) == key]
    if not hits:
        return None
    anchor = n if isinstance(n, int) else 0
    return min(hits, key=lambda i: (abs(i - anchor), i))


def place_asks(asks: list, lines: list[str] | None) -> list[dict]:
    """Each ask with where its cited lines are now, and whether it is stale.

    An ask is stale when it was met or partly and a line it cited is no
    longer in the text, because its status was judged on text that is not
    there any more. A missing ask and an unanchored one are never stale. With
    no text to place them in, nothing is placed and nothing is stale.
    """
    out = []
    for ask in asks or []:
        if not isinstance(ask, dict):
            continue
        placed = []
        for cited in ask.get("lines") or []:
            if not isinstance(cited, dict):
                continue
            now = find_line(cited, lines) if lines is not None else cited.get("n")
            placed.append({"n": cited.get("n"), "text": cited.get("text"),
                           "now": now, "key": line_key(cited.get("text"))})
        stale = (lines is not None and ask.get("status") in ("met", "partly")
                 and any(p["now"] is None for p in placed))
        out.append({**ask, "placed": placed, "stale": stale})
    return out


# --------------------------------------------- what changed between scores

_RANK = {"missing": 0, "partly": 1, "met": 2}


def _ask_words(ask) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", clean(ask.get("ask")).casefold()).split())


def ask_changes(before: list, after: list) -> list[dict]:
    """The asks whose status moved between two workings, and the ones that
    came or went.

    An ask is the same ask when its words are the same once case and
    punctuation are set aside, or failing that when they are nine tenths the
    same, because a scorer asked twice words an ask a little differently.
    Each entry is {"ask", "from", "to"}, from None for an ask that is new and
    to None for one no longer listed.
    """
    before = [a for a in before or [] if isinstance(a, dict)]
    after = [a for a in after or [] if isinstance(a, dict)]
    unused = list(range(len(before)))
    words = [_ask_words(a) for a in before]
    out = []
    for ask in after:
        mine = _ask_words(ask)
        match = next((i for i in unused if words[i] == mine), None)
        if match is None:
            scored = sorted(((difflib.SequenceMatcher(None, words[i], mine).ratio(), -i)
                             for i in unused), reverse=True)
            if scored and scored[0][0] >= 0.9:
                match = -scored[0][1]
        if match is None:
            out.append({"ask": ask.get("ask"), "from": None, "to": ask.get("status")})
            continue
        unused.remove(match)
        if before[match].get("status") != ask.get("status"):
            out.append({"ask": ask.get("ask"), "from": before[match].get("status"),
                        "to": ask.get("status")})
    out += [{"ask": before[i].get("ask"), "from": before[i].get("status"), "to": None}
            for i in unused]
    return out


def change_word(change: dict) -> str:
    """better, worse, new or gone, for how a change is shown."""
    if change.get("from") is None:
        return "new"
    if change.get("to") is None:
        return "gone"
    up = _RANK.get(change.get("to"), 0) - _RANK.get(change.get("from"), 0)
    return "better" if up > 0 else "worse"
