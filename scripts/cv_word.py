#!/usr/bin/env python
"""The saved CV as a Word file, from one fixed template that an applicant
tracking system reads as plainly as a person does.

One column. No tables, no text boxes, no images, no shapes, no fields, and no
header or footer, so nothing sits where a parser does not look. One plain font
for every style, black, on A4. The name is Heading 1, the sections are Heading
2 under their standard names, an entry is Heading 3, a bullet is a real Word
list paragraph in the List Bullet style with its numbering on the paragraph,
and every other line is a Normal paragraph. The order is the order of the
text, line for line, and the text is the text: nothing is added, reworded or
left out, and blank lines are the spaces between paragraphs.

python-docx builds it, from its own default template with the thumbnail image
taken out of the package. It is imported here and nowhere else, and only the
dashboard's Download route imports this module, so nothing that n8n runs needs
python-docx. cv_store's own stdlib reader reads the file back in the tests,
which hold its text and order to the saved copy and its XML to the rules above.
"""

from __future__ import annotations

import io
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import cv_doc  # noqa: E402

FONT = "Arial"
SIZES = {"Normal": 10.5, "Heading 1": 18, "Heading 2": 13, "Heading 3": 11,
         "List Bullet": 10.5}
MIME = ("application/vnd.openxmlformats-officedocument."
        "wordprocessingml.document")

_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_THUMBNAIL = ("http://schemas.openxmlformats.org/package/2006/relationships/"
              "metadata/thumbnail")


class WordUnavailable(RuntimeError):
    """python-docx is not installed in this Python."""


def _docx():
    try:
        import docx
    except ImportError as err:
        raise WordUnavailable(
            "python-docx is not installed in the Python the dashboard runs, "
            "so the Word file cannot be made. pip install -r requirements.txt "
            "installs it.") from err
    return docx


def _plain_style(style, size: float, bold: bool) -> None:
    """One font, black, no theme colour, no caps, no spacing tricks."""
    from docx.oxml.ns import qn
    from docx.shared import Pt, RGBColor
    font = style.font
    font.name = FONT
    font.size = Pt(size)
    font.bold = bold
    font.italic = False
    font.color.rgb = RGBColor(0, 0, 0)
    rpr = style.element.get_or_add_rPr()
    fonts = rpr.find(qn("w:rFonts"))
    if fonts is None:
        fonts = rpr.makeelement(qn("w:rFonts"), {})
        rpr.append(fonts)
    for attr in list(fonts.attrib):
        del fonts.attrib[attr]
    for which in ("w:ascii", "w:hAnsi", "w:cs", "w:eastAsia"):
        fonts.set(qn(which), FONT)


def _bullet_number(document) -> str:
    """The numbering the template's List Bullet style points at."""
    from docx.oxml.ns import qn
    ppr = document.styles["List Bullet"].element.pPr
    num = ppr.find(qn("w:numPr")) if ppr is not None else None
    num_id = num.find(qn("w:numId")) if num is not None else None
    if num_id is None:
        raise RuntimeError("the template's List Bullet style has no numbering")
    return num_id.get(qn("w:val"))


def _list_paragraph(paragraph, num_id: str) -> None:
    """Put the list numbering on the paragraph itself, level 0, as Word does
    when a bullet is typed, so any reader sees a list item and not only a
    style name."""
    from docx.oxml.ns import qn
    ppr = paragraph._p.get_or_add_pPr()
    num = ppr.makeelement(qn("w:numPr"), {})
    level = num.makeelement(qn("w:ilvl"), {qn("w:val"): "0"})
    ident = num.makeelement(qn("w:numId"), {qn("w:val"): num_id})
    num.append(level)
    num.append(ident)
    ppr.append(num)


def build(text: str, title: str = "") -> bytes:
    """The Word file for a saved copy, as bytes.

    text is the saved copy, in the editor's layout. title goes in the file's
    properties, with the CV's owner as its author, and nothing else of the
    machine's is written into it.
    """
    docx = _docx()
    from docx.enum.section import WD_ORIENT
    from docx.shared import Cm, Pt

    document = docx.Document()
    package = document.part.package
    for rid, rel in list(package.rels.items()):
        if rel.reltype == _THUMBNAIL:
            del package.rels[rid]

    section = document.sections[0]
    section.orientation = WD_ORIENT.PORTRAIT
    section.page_width, section.page_height = Cm(21.0), Cm(29.7)
    section.left_margin = section.right_margin = Cm(2.0)
    section.top_margin = section.bottom_margin = Cm(1.8)

    for name, size in SIZES.items():
        _plain_style(document.styles[name], size, name.startswith("Heading"))
    for name in ("Heading 1", "Heading 2", "Heading 3"):
        fmt = document.styles[name].paragraph_format
        fmt.space_before = Pt(12 if name != "Heading 1" else 0)
        fmt.space_after = Pt(4)
        fmt.keep_with_next = True
    document.styles["Normal"].paragraph_format.space_after = Pt(4)
    num_id = _bullet_number(document)

    for line in cv_doc._lines_of(text):
        if not line.strip():
            continue
        heading = re.match(r"^(#{1,3}) (.*)$", line)
        if heading:
            document.add_paragraph(heading.group(2).strip(),
                                   style=f"Heading {len(heading.group(1))}")
        elif line.startswith("- "):
            para = document.add_paragraph(line[2:].strip(), style="List Bullet")
            _list_paragraph(para, num_id)
        else:
            document.add_paragraph(line.strip(), style="Normal")

    props = document.core_properties
    props.title = cv_doc.clean(title)
    props.author = cv_doc.OWNER
    props.last_modified_by = cv_doc.OWNER
    props.comments = ""
    props.subject = ""
    props.keywords = ""
    props.category = ""
    now = datetime.now(timezone.utc).replace(microsecond=0, tzinfo=None)
    props.created = props.modified = now
    props.revision = 1

    out = io.BytesIO()
    document.save(out)
    return out.getvalue()
