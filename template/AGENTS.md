# AGENTS.md · 给 AI 助手的规矩

这个目录是个人知识库。任何 agent（opencode / Claude Code / Codex）在这里干活之前，先读这份文件。

## 三条铁律

1. **`002-references/raw/` 只读。** 那是原文存档。一旦被改写，"当时作者到底怎么说的"就再也无法核查。抓取有问题就写进 `002-references/failed/`，不要动原文。
2. **不把模型生成的内容混进 raw。** 摘要、翻译、润色后的版本，一律不许写进 `raw/`。需要留存就写 `002-references/wiki/`，并且标 `ai_generated: true`。
3. **`003-notes/` 是人写的地方。** 你可以起草，但文首必须标 `draft: true`，并且不许覆盖已有的观点性内容——那是本人判断的痕迹。

删除 `raw/` 里的文章不要在编辑器里直接删：用 `cd kb-inbox && python manage.py drop <路径|URL> --reason "..."`。
它会把删除和原因记进 `002-references/dropped.md`，原文仍能从 git 历史找回。pre-commit 钩子会拦住直接删除。

## 可以做

- 读 `002-references/` 回答提问，引用时给出文件名和大致段落
- 往 `001-inbox/` 追加内容（只追加，不改旧的）
- 在 `004-archive/` 的归档里补"反馈"小节
- 批量检查：字数异常、重复链接、失效引用

## 回答方式

- 分清「原文说的」和「我的推断」，不要混在同一段里
- 不确定就说不确定，不要用通顺的句子掩盖空白
- 长答案分点写，不要小作文

## 写文件的规矩

- 文件名：`YYYYMMDD-短标题-<url指纹8位>.md`
- frontmatter 至少包含 `url / title / source / fetched_at / chars`
- 一次改动一个 commit，message 以 `add:` / `fix:` / `chore:` 开头
- 落盘用原子写（先写 `.tmp` 再改名），不要直接覆盖

## 默认不做的事

- 不主动生成"全文摘要"塞进库里
- 不替用户判断一篇文章"值不值得读"
- 不因为用户没问，就去整理、重命名、归类已有文件
