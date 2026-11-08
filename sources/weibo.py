"""微博：单条正文。

直连会被"新浪访客系统"拦，m.weibo.cn 也拦——必须走真浏览器。
标题优先取【】里的那句话，没有就取第一行并去掉 #话题#。
"""

import re
from urllib.parse import urlparse

from .base import browser

_BODY = r'''
  await page.waitForTimeout(2500);
  data = await page.evaluate(() => {
    const pick = (sel) => { const el = document.querySelector(sel); return el ? el.innerText.trim() : ""; };
    const text = pick(".weibo-text") || pick(".card");
    const first = text.split("\n").map((s) => s.trim()).find((s) => s.length > 4) || "";
    const bracket = text.match(/【([^】]{4,80})】/);
    let title = bracket ? bracket[1] : first.replace(/#[^#\s]{1,30}#/g, "").trim();
    title = title.replace(/^[#\s]+/, "").slice(0, 60);
    return {
      title: title || first.slice(0, 60),
      author: pick(".m-text-cut"),
      text: text,
      kind: "post",
      url: location.href,
    };
  });
'''


def match(url):
    host = (urlparse(url).netloc or "").lower().split(":")[0]
    return host.endswith("weibo.com") or host.endswith("weibo.cn")


def _post_id(url):
    m = re.search(r"/(?:detail|status)/([A-Za-z0-9]+)", url)
    return m.group(1) if m else None


def fetch(url):
    wid = _post_id(url)
    target = f"https://m.weibo.cn/detail/{wid}" if wid else url
    data = browser(target, _BODY, wait=3500, space_prefix="kb-weibo")
    if not data.get("text") or len(data["text"]) < 20:
        raise RuntimeError("微博没抓到正文：可能被访客系统拦了，或这条微博已删除")
    data.setdefault("extra", {})["extractor"] = "ego-browser/weibo"
    return data
