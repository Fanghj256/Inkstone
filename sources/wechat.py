"""微信公众号：静态页，最好抓的一类。作者是公众号名，要从页面里补。"""

from urllib.parse import urlparse

from . import web


def match(url):
    host = (urlparse(url).netloc or "").lower().split(":")[0]
    return host.endswith("mp.weixin.qq.com")


def fetch(url):
    return web.fetch_page(url, wechat=True)
