"""通用网页：trafilatura 抽正文，抽不到再退回真浏览器。

微信公众号也走这条路，只是作者要从页面里的 js_name 补。
"""

import re

import requests
import trafilatura

from .base import HEADERS, browser

_WEB_BODY = r'''
  await page.evaluate(() => new Promise((resolve) => {
    let n = 0;
    const timer = setInterval(() => {
      window.scrollBy(0, window.innerHeight);
      if (++n >= 5) { clearInterval(timer); resolve(true); }
    }, 400);
  }));
  data = await page.evaluate(() => {
    const pick = (sel) => { const el = document.querySelector(sel); return el ? el.innerText.trim() : ""; };
    return {
      title: (document.title || "").trim(),
      author: pick('meta[name="author"]') ? "" : "",
      text: pick("article") || pick("main") || document.body.innerText.trim(),
      kind: "page",
      url: location.href,
    };
  });
'''


def fetch_page(url, wechat=False):
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    html = resp.content.decode(resp.apparent_encoding or "utf-8", errors="replace")

    text = trafilatura.extract(
        html, url=url, output_format="markdown",
        include_links=True, include_tables=True, favor_recall=True,
    )
    meta = trafilatura.extract_metadata(html, default_url=url)
    author = (meta.author if meta and meta.author else "") or ""

    # 公众号的作者是公众号名，trafilatura 抓不到，从页面里补
    if wechat and not author:
        m = re.search(r'id="js_name"[^>]*>\s*([^<]+)', html)
        if m:
            author = m.group(1).strip()

    if not text or len(text) < 200:
        data = browser(url, _WEB_BODY, wait=2500, space_prefix="kb-web")
        if not data.get("text"):
            raise RuntimeError("正文抽取为空，可能被反爬拦住了")
        return {
            "title": (meta.title if meta and meta.title else data.get("title")) or url,
            "author": author,
            "text": data["text"],
            "extra": {"extractor": "browser-fallback"},
        }

    return {
        "title": (meta.title if meta and meta.title else "") or url,
        "author": author,
        "text": text,
        "extra": {"extractor": "trafilatura"},
    }


def fetch(url):
    return fetch_page(url)
