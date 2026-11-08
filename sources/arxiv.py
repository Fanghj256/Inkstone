"""arXiv：官方 API 拿元数据，再下 PDF 抽全文。

比任何爬虫都干净：一次能拿四万字，标题作者日期都是结构化的。
"""

import re
from io import BytesIO
from urllib.parse import urlparse
from xml.etree import ElementTree as ET

import requests
from pypdf import PdfReader

from .base import HEADERS


def match(url):
    host = (urlparse(url).netloc or "").lower().split(":")[0]
    return host.endswith("arxiv.org")


def fetch(url):
    m = re.search(r"arxiv\.org/(?:abs|pdf)/([0-9]{4}\.[0-9]{4,5}|[a-zA-Z\-]+/\d{7})", url)
    if not m:
        raise ValueError("没从链接里认出 arXiv 编号")
    aid = m.group(1)

    ns = {"a": "http://www.w3.org/2005/Atom"}
    api = requests.get(
        "http://export.arxiv.org/api/query",
        params={"id_list": aid}, headers=HEADERS, timeout=40,
    )
    api.raise_for_status()
    entry = ET.fromstring(api.content).find("a:entry", ns)
    if entry is None:
        raise RuntimeError("arXiv API 没有返回这篇论文")

    def txt(tag):
        node = entry.find(f"a:{tag}", ns)
        return " ".join(node.text.split()) if node is not None and node.text else ""

    title = txt("title")
    abstract = txt("summary")
    published = txt("published")[:10]
    authors = [a.find("a:name", ns).text for a in entry.findall("a:author", ns)
               if a.find("a:name", ns) is not None]

    body = ""
    try:
        pdf = requests.get(f"https://arxiv.org/pdf/{aid}", headers=HEADERS, timeout=90)
        pdf.raise_for_status()
        reader = PdfReader(BytesIO(pdf.content))
        pages = [p.extract_text() or "" for p in reader.pages[:40]]
        body = "\n\n".join(pages).strip()
    except Exception as exc:  # noqa: BLE001
        body = f"（PDF 全文抽取失败：{exc}）"

    text = "\n".join([
        f"> {', '.join(authors[:6])}{' 等' if len(authors) > 6 else ''} · {published}",
        "",
        "## 摘要",
        "",
        abstract,
        "",
        "## 全文",
        "",
        body[:120000] or "（没有抽到正文）",
    ])

    return {
        "title": f"[arXiv] {title}",
        "author": ", ".join(authors[:3]),
        "text": text,
        "extra": {
            "arxiv_id": aid,
            "pages": len(body) // 1800 or None,
            "extractor": "arxiv-api + pypdf",
        },
    }
