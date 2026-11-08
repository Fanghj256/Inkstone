"""知乎：回答与专栏。

默认只显示回答的前几段，必须先点开所有"阅读全文"，否则只能抓到几百字。
"""

from urllib.parse import urlparse

from .base import browser

_BODY = r'''
  // 知乎默认只显示回答的前几段，先点开所有"阅读全文"
  await page.evaluate(() => {
    const nodes = Array.from(document.querySelectorAll("button, .ContentItem-expandButton"));
    nodes.filter((el) => /展开阅读全文|阅读全文|显示全部/.test(el.innerText || ""))
      .slice(0, 6)
      .forEach((el) => { try { el.click(); } catch (e) {} });
  });
  await page.waitForTimeout(1500);

  await page.evaluate(() => new Promise((resolve) => {
    let n = 0;
    const timer = setInterval(() => {
      window.scrollBy(0, window.innerHeight * 0.9);
      if (++n >= 8) { clearInterval(timer); resolve(true); }
    }, 500);
  }));
  await page.waitForTimeout(1000);
  data = await page.evaluate(() => {
    const pick = (sel) => { const el = document.querySelector(sel); return el ? el.innerText.trim() : ""; };
    return {
      title: pick("h1.Post-Title") || pick(".QuestionHeader-title") || document.title,
      author: pick(".AuthorInfo-name") || pick(".UserLink-link") || "",
      text: pick(".Post-RichText") || pick(".RichText.ztext") || pick("article") || "",
      kind: "answer",
      url: location.href,
    };
  });
'''


def match(url):
    host = (urlparse(url).netloc or "").lower().split(":")[0]
    return host.endswith("zhihu.com")


def fetch(url):
    data = browser(url, _BODY, wait=3000, space_prefix="kb-zhihu")
    if not data.get("text") or len(data["text"]) < 30:
        raise RuntimeError("没抓到正文，可能没登录或链接已失效")
    data.setdefault("extra", {})["extractor"] = f"ego-browser/{data.get('kind', 'answer')}"
    return data
