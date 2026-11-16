"""Small stdlib HTML helpers for ASP.NET WebForms pages."""
import html
import re
from html.parser import HTMLParser


class FormPage(HTMLParser):
    """Collects hidden inputs, <select> options and one table (rows of cells + data-url links)."""

    def __init__(self, text, table_id=None):
        super().__init__(convert_charrefs=True)
        self.hidden = {}
        self.selects = {}
        self.table_id = table_id
        self.table_found = False
        self.rows = []
        self.pager_args = set()
        self._select = None
        self._option = None
        self._tdepth = 0
        self._row = None
        self._cell = None
        self.feed(text)
        self.close()

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "input" and a.get("type") == "hidden" and a.get("name"):
            self.hidden[a["name"]] = a.get("value") or ""
        elif tag == "select":
            self._end_option()
            self._select = a.get("id") or a.get("name")
            self.selects.setdefault(self._select, [])
        elif tag == "option" and self._select:
            self._end_option()
            self._option = [a.get("value") or "", "", "selected" in a]
        elif tag == "table":
            if self._tdepth:
                self._tdepth += 1
            elif self.table_id and a.get("id") == self.table_id:
                self.table_found = True
                self._tdepth = 1
        elif self._tdepth == 1 and tag == "tr":
            self._row = {"cells": [], "links": []}
        elif self._tdepth == 1 and tag in ("td", "th") and self._row is not None:
            self._cell = []
            self._row["is_header"] = tag == "th"
        if self._tdepth and tag == "a":
            if self._row is not None and "data-url" in a:
                self._row["links"].append((a["data-url"] or "").strip())
            m = re.search(r"__doPostBack\('([^']*)','(Page\$[^']*)'\)", a.get("href") or "")
            if m:
                self.pager_args.add(m.group(2))

    def _end_option(self):
        # Browsers accept <option> without </option>; so do we.
        if self._option is not None and self._select is not None:
            self.selects[self._select].append((self._option[0], " ".join(self._option[1].split()),
                                               self._option[2]))
        self._option = None

    def handle_endtag(self, tag):
        if tag == "select":
            self._end_option()
            self._select = None
        elif tag == "option":
            self._end_option()
        elif tag == "table" and self._tdepth:
            self._tdepth -= 1
        elif self._tdepth == 1 and tag in ("td", "th") and self._cell is not None:
            self._row["cells"].append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif self._tdepth == 1 and tag == "tr" and self._row is not None:
            self.rows.append(self._row)
            self._row = None

    def selected(self, ddl):
        return next((v for v, _, sel in self.selects.get(ddl, []) if sel), None)

    def handle_data(self, data):
        if self._option is not None:
            self._option[1] += data
        if self._cell is not None:
            self._cell.append(data)


def label_text(text, label_id):
    m = re.search(r'<label[^>]*id="%s"[^>]*>(.*?)</label>' % re.escape(label_id), text, re.S)
    return html.unescape(re.sub(r"<[^>]+>", "", m.group(1))).strip() if m else None
