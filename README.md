# kb-inbox · 个人知识库收件箱

手机分享一条链接 → 自动抓原文 → 写进本地 Markdown → 自动 git commit → 回你一条通知。

**不调用任何大模型改写正文**，入库的永远是作者原文。

---

## 三种用法

```powershell
cd D:\Files\？-Learning\kb-inbox

python inbox.py                            # 常驻，收 Telegram 消息
python inbox.py --url https://...          # 手动测一个链接
python inbox.py --text "临时想到的一句话"    # 写进 001-inbox
```

Telegram 里怎么用：

| 你发什么 | 会发生什么 |
|---|---|
| 一个链接 | 抓正文 → `002-references/raw/<来源>/` |
| B站 / YouTube 视频链接 | 下载 → 字幕或本地 Whisper → 转录 + 关键截图，存入 `002-references/raw/video/` |
| `/video <视频链接>` | 强制按视频处理（非视频站也交给 yt-dlp），同样进 `raw/video/` |
| `/learn <视频编号>` | 为已入库视频在 `003-notes/` 建学习笔记草稿 |
| `/note 一段文字` | → `003-notes/`（你自己写的东西） |
| `/video 转写稿` | → `003-notes/video trans notes/`（已经整理好的视频笔记） |
| `/video` + 一个文件 | 文件存进 `003-notes/video trans notes/` |
| `/idea 一句话` | → `001-inbox/`（和默认行为一样，写不写都行） |
| 不带前缀的一句话 | → `001-inbox/` |
| 不带前缀的一个文件 | → `002-references/files/` |
| 一条消息里多个链接 | 逐个处理，逐个回执 |
| 单独发 `/video` | 接下来 30 分钟内的文件或文字 → `003-notes/video trans notes/` |
| 「这两个存入视频转写稿」 | 把最近收到的文件挪到对应目录 |
| 回复文件消息发 `/video` | 只挪那一个文件 |

前缀只认开头第一个词，不区分大小写。链接优先于前缀——带链接就一定会去抓取。

「这两个/这几篇……」这类指令只认最近 30 分钟收到的文件；「两个/三篇」决定取几个，不写数量就取全部。bot 重启后不记得重启前的文件，重新发一次即可。

### 命令菜单（可选，一次配好）

回 @BotFather 发 `/setcommands`，选你的 bot，粘贴：

```
note - 存为我的笔记
video - 存为视频转写稿
learn - 为视频建学习笔记草稿
idea - 存为随手记
```

以后聊天框左下角就有这几个按钮，不用手打。

---

## 四类来源，各自的脾气

| 来源 | 走什么 | 坑 |
|---|---|---|
| X 推文/长文 | ego-browser 真浏览器 | 必须登录；长文正文在 `twitterArticleReadView` 里 |
| GitHub 仓库/issue/PR | `gh api` | 需要 `gh auth login`；比爬页面稳得多 |
| 知乎 | ego-browser 真浏览器 | 要先点开"阅读全文"，否则只抓到前几段 |
| 微信公众号 | requests + trafilatura | 静态页，最好抓的一类 |
| B站/YouTube 视频 | yt-dlp + 字幕/Whisper + OCR 选帧 | 字幕常要登录；CPU 转写慢 |
| 抖音视频 | yt-dlp（需 cookies.txt） | 不带 fresh cookies 直接失败；短链会自动展开 |
| Hugging Face Space | HF API + README | 页面本体是 iframe，抓的是 README 和元信息 |
| 小红书 | ego-browser 真浏览器 | 要登录；`xsec_token` 会过期；图文笔记正文往往只有几句话 |
| 其他网页 | requests + trafilatura | 抽不到正文时自动退回真浏览器再试一次 |

视频链接自动转录的原始稿在 `002-references/raw/video/`；已经整理好的视频笔记用
`/video` 存到 `003-notes/video trans notes/`。AI 生成的总结、改写稿不要混进 `raw/`。

判断抓没抓全很简单：看回执里的字数。技术长文低于 1000 字基本就是被拦了，
文件头部会带 `incomplete: true`，失败的原始记录在 `002-references/failed/`。

程序还会识别"假正文"——页面被拦时会返回页脚、法律声明、"请打开 App 查看"这类文字，
这些一律判失败，不会污染库。

---

## 视频：完整转录 + 关键画面

把 B站/YouTube 视频链接直接发给 bot（或 `/video <链接>`），它会：

1. 下载视频（默认 ≤720p，长视频降到 480p）
2. 平台字幕优先；没有字幕再用本地 `faster-whisper` 转写
3. 固定间隔 + 场景变化 + 每段转写起点三种候选，用 OCR 文字密度、场景差、清晰度、文字新颖度打分；去重后先按时间分桶各选一张，再按分数补，默认 8/16/22/28 张
4. 落到 `002-references/raw/video/<日期>-<标题>-<指纹>/`：

| 文件 | 用途 |
|---|---|
| `transcript.md` | 带时间戳和截图的 Markdown，适合在库里读 |
| `transcript.txt` | 纯文本，直接复制给 AI 做总结/问答 |
| `note.html` | 自包含 HTML（图片内嵌），手机看最方便，会作为文件发回 Telegram |
| `frames/` | 关键画面原图 |
| `meta.json` | 元信息 + 选帧记录 |

依赖：`ffmpeg`、`yt-dlp`、`faster-whisper`、`rapidocr-onnxruntime`；`zhconv` 可选（繁转简）。

**不做摘要**，转录就是原文。Whisper 在 CPU 上会有识别错误，想更准：

- **B站**：用浏览器扩展导出 `cookies.txt`，设 `VIDEO_COOKIES=路径`，优先拿 B站 AI 字幕（比 ASR 准得多）
- **抖音**：yt-dlp 现在必须要 fresh cookies，不带 cookies 会报 `Fresh cookies ... are needed`。同样用浏览器扩展导出 `cookies.txt`，设 `VIDEO_COOKIES=路径`；`v.douyin.com` 短链会自动展开成 `douyin.com/video/<id>`。抖音 cookies 通常只能撑几天到几周，失效后重新导出；想免手动可以装 Firefox 登录抖音，设 `VIDEO_COOKIES_FROM_BROWSER=firefox`，yt-dlp 每次自动读。尽量固定代理节点，浏览器和 bot 用同一个出口 IP。
- `WHISPER_MODEL=medium`：更准，但 CPU 上更慢（默认 `small`）
- `WHISPER_LANG=zh`：跳过自动检测，强制按中文转写
- `VIDEO_MAX_DURATION=7200`：长视频上限，默认 2 小时
- `VIDEO_MAX_FRAMES=16`：强制关键画面数量（默认按视频长度 8/16/22/28）
- `VIDEO_SCENE_THRESHOLD=0.30`：场景变化灵敏度，调低会抓更多切换点
- `VIDEO_MAX_ANCHORS=30`：最多补多少个“转写段落起点”锚点帧

视频任务在后台单线程排队，不会卡住 Telegram 轮询。CPU 上 20 分钟视频大约要 10-20 分钟，完成后会主动发结果。

### 空间占用

- **模型缓存**：`C:\Users\<你>\.cache\huggingface\hub`。`small` 约 464MB，`base.en` 约 141MB；只下载一次，不随视频数量增长。`base.en` 是英文模型，当前流程不用，想腾空间可以删。
- **每个视频的临时下载/音频/候选帧**：处理完自动删除（`tempfile` + `rmtree`），不会留在磁盘。
- **成品**：`002-references/raw/video/<日期>-<标题>-<指纹>/` 大约 **2-4MB/条**，其中 `note.html` 占一半（图片以 base64 内嵌），`frames/` 占一半，文本只有几 KB。
- 如果空间紧张，可以只删 `note.html`，保留 `frames/` + `transcript.md/txt`；手机上看过的 HTML 也可以随时重发。

```powershell
# 删除不用的英文 base 模型（约 141MB）
Remove-Item -Recurse "$env:USERPROFILE\.cache\huggingface\hub\models--Systran--faster-whisper-base.en"

# 只删所有视频的 note.html，保留截图和文本
Get-ChildItem "D:\Files\？-Learning\knowledge-base\002-references\raw\video" -Recurse -Filter note.html | Remove-Item
```

---

## 从视频到学习笔记

视频入库后，回执里会带一行：

```
整理成学习笔记：/learn BV1VS4i6fEw2
```

1. 发 `/learn BV1VS4i6fEw2`（也支持 YouTube ID、视频链接、文件夹指纹）。
2. bot 在 `003-notes/` 建一篇 `<日期>-<标题>-<指纹>-学习笔记.md`：
   - frontmatter 带 `draft: true`
   - `source_video` 指回 `002-references/raw/video/...`
   - 正文带原文、关键画面的链接，以及「一句话总结 / 我学到了什么 / 关键概念 / 待验证」几个空 section
3. 打开这篇笔记，和 AI 一起读 `transcript.md`，把内容填进去；定稿后删掉 `draft: true`。
4. 同一视频已有笔记时不会覆盖；要新版本就改文件名或删掉旧的再建。

`/note` 是存任意文字，`/learn` 是专门建「和 raw 视频关联」的笔记草稿。如果是在 OpenCode 里直接聊，也可以让 agent 按同一模板写 `003-notes/`，不必经过 Telegram。

---

## 开机自动跑

**已经配好了。** 现在登录 Windows 就会自动启动，窗口隐藏，日志写到 `inbox.log`。

用的是「启动」文件夹方案，最轻，不需要管理员权限：

```
C:\Users\<你>\AppData\Roaming\Microsoft\Windows\Start Menu\Programs\Startup\kb-inbox.lnk
```

- **手动启动**：双击 `start-inbox.vbs`（重复双击不会开第二个，会自动退出）
- **停止**：双击 `stop-inbox.bat`
- **看日志**：`notepad inbox.log`
- **取消自启**：删掉上面那个 `.lnk` 就行

进程有守护：代理没起来、网络抖动、Telegram 报错，它都不会退出，而是等一会儿重试。所以开机时先开代理还是先登录，顺序无所谓。

> 电脑关机期间收到的消息，Telegram 只保留 24 小时。
> 关机超过一天，那段时间的链接就真没了——这是这套方案唯一的硬伤。

---

## 换电脑 / 加设备

知识库本身就是一个 git 仓库：

```powershell
git -C "D:\Files\？-Learning\knowledge-base" remote add origin <私有仓库地址>
git -C "D:\Files\？-Learning\knowledge-base" push -u origin main
```

另一台机器 `git clone` 下来，把 `.env` 里的 `KB_ROOT` 指过去就行。

---

## 想扩展

- **视频**：`raw/video/` 已经留好。有字幕的用 `yt-dlp --write-auto-sub`，没字幕的接 faster-whisper，转完把文本走 `--text` 或直接发文件。
- **每周巡检**：用 `opencode run --auto` 让 agent 扫一遍 `001-inbox/`，把值得展开的挑出来。
- **重试**：`002-references/failed/` 里的 URL 可以批量重跑，`inbox.py --url` 支持多次传参。

---

## 常见问题

| 现象 | 原因 |
|---|---|
| `ego-browser 退出码 1` | 浏览器服务没起来，或页面 20 秒内没加载完 |
| `gh api 失败：401` | `gh auth login` 过期了 |
| 知乎只抓到几百字 | 没登录，或那条回答本身很短 |
| `409 Conflict` | 有两个 inbox.py 在同时收消息 |
| `ConnectTimeout` / `TLS handshake timeout` | 代理没起来，或 `.env` 里的 `PROXY` 不通。抓取和 Telegram 共用这个代理 |
| 抓到的 X 是登录页 | ego 浏览器里没有 X 的登录态，手动登录一次 |
