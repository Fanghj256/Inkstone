# 002-references · 外部输入层

这里只有**别人写的东西**，而且必须是原文。

| 子目录 | 来源 |
|---|---|
| `raw/x/` | X 推文与长文（长文走真浏览器，需登录态） |
| `raw/github/` | 仓库 README、issue、PR |
| `raw/zhihu/` | 知乎回答与专栏 |
| `raw/wechat/` | 微信公众号文章 |
| `raw/xiaohongshu/` | 小红书笔记（需登录，且正文往往很短） |
| `raw/web/` | 其他博客、网页 |
| `raw/video/` | 视频转写稿 |
| `files/` | 你直接发过来的 PDF / Word / 文本 |
| `failed/` | 抓失败的记录，附 URL 和错误原因 |

每个文件头部都有 YAML frontmatter，至少含 `url / title / source / fetched_at / chars`。
`chars` 是用来判断抓取是否完整的：如果一篇文章只有 200 字，多半是被反爬拦了。

## 怎么清理

重复抓取的判断靠文件名末尾的 8 位 hash（URL 的指纹）。同一个链接重复发送会被跳过。
想批量看有没有垃圾：

```powershell
Get-ChildItem raw -Recurse -Filter *.md | Measure-Object -Property Length -Sum
```
