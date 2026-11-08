"""小红书：图文笔记。

要登录，链接里的 xsec_token 会过期，正文往往只有几句话。
能抓，但纯文本存档的价值有限。
"""

import re
from urllib.parse import urlparse

from .base import browser

_BODY = r'''
  await page.evaluate(() => {
    const btns = Array.from(document.querySelectorAll("button, span, div"));
    btns.filter((el) => (el.innerText || "").trim() === "展开")
      .slice(0, 3)
      .forEach((el) => { try { el.click(); } catch (e) {} });
  });
  await page.waitForTimeout(1200);
  data = await page.evaluate(() => {
    const pick = (sel) => { const el = document.querySelector(sel); return el ? el.innerText.trim() : ""; };
    const desc = pick("#detail-desc") || pick(".note-text") || pick(".desc");
    return {
      title: pick("#detail-title") || document.title,
      author: pick(".author-container .username") || pick(".author-wrapper .name") || "",
      text: desc,
      kind: "note",
      url: location.href,
    };
  });
'''


def match(url):
    host = (urlparse(url).netloc or "").lower().split(":")[0]
    return host.endswith("xiaohongshu.com") or host.endswith("xhslink.com")


def fetch(url):
    data = browser(url, _BODY, wait=4000, space_prefix="kb-xhs")
    text = (data.get("text") or "").strip()
    if len(text) < 30:
        raise RuntimeError("小红书没抓到正文：多半是没登录，或笔记已失效（xsec_token 会过期）")
    data.setdefault("extra", {})["extractor"] = "ego-browser/note"
    return data
