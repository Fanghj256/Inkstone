"""X（推特）：推文与长文。

长文正文在 twitterArticleReadView 里，普通推文在 tweetText 里。
标题优先取 document.title，因为正文容器里的第一个 h1 往往只是小节标题。
"""

from urllib.parse import urlparse

from .base import browser

_BODY = r'''
  data = await page.evaluate(() => {
    const pick = (sel) => { const el = document.querySelector(sel); return el ? el.innerText.trim() : ""; };
    const isNum = (s) => /^[\d,.\s]+$/.test(s);

    const article = pick('[data-testid="twitterArticleReadView"]');
    const tweets = Array.from(document.querySelectorAll('[data-testid="tweetText"]'))
      .map((el) => el.innerText.trim()).filter(Boolean);

    let body = article || tweets.join("\n\n---\n\n");

    let title = (document.title || "")
      .replace(/^X 上的 [^：]*：/, "")
      .replace(/^.*? on X: /, "")
      .replace(/ \/ X$/, "")
      .replace(/^[“"「]/, "").replace(/[”"」]$/, "")
      .trim();

    // 文章头部会混进互动数（19 / 15 / 60 / 8,488），连同重复的标题行一起剥掉
    const lines = body.split("\n");
    while (lines.length) {
      const head = lines[0].trim();
      if (!head || isNum(head) || (title && head === title)) { lines.shift(); continue; }
      break;
    }
    body = lines.join("\n").trim();

    if (!title) title = body.split("\n")[0] || "";
    if (title.length > 80) title = title.slice(0, 80).trim() + "…";

    const nameBlock = pick('[data-testid="User-Name"]');
    return {
      title: title,
      author: nameBlock.split("\n").filter(Boolean)[1] || "",
      text: body,
      kind: article ? "article" : "tweet",
      url: location.href,
    };
  });
'''


def match(url):
    host = (urlparse(url).netloc or "").lower().split(":")[0]
    return host.endswith("x.com") or host.endswith("twitter.com")


def fetch(url):
    data = browser(url, _BODY, wait=4000, space_prefix="kb-x")
    if not data.get("text") or len(data["text"]) < 30:
        raise RuntimeError("没抓到正文，可能没登录或链接已失效")
    data.setdefault("extra", {})["extractor"] = f"ego-browser/{data.get('kind', 'x')}"
    return data
