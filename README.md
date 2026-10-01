# hudong-rss · A股互动问答关键词监控

把**深交所互动易**和**上证e互动**的全市场董秘问答抓下来，用 AI 产业链词库过滤，生成一份 RSS。
一次抓取 = 全市场最新问答 → 命中词库才留下 → 写进 `feed/rss.xml` → 部署到 GitHub Pages。

和隔壁 [cninfo-ann-rss](https://github.com/liuchao88/cninfo-ann-rss)（巨潮公告 + 调研记录表）是互补的两条管线：
那边管"公司主动发的公告/调研"，这边管"投资者问、董秘答的问答"。

## 一、产出（订阅地址，三个都可用）

| 地址 | 说明 |
|---|---|
| https://unreallover.com/hudong-rss/rss.xml | GitHub Pages（绑了自定义域名） |
| https://gcore.jsdelivr.net/gh/liuchao88/hudong-rss@main/feed/rss.xml | **国内推荐**（jsdelivr CDN；国内服务器直连 Pages 的 185.199.x.x 会间歇性挂死） |
| https://raw.githubusercontent.com/liuchao88/hudong-rss/main/feed/rss.xml | 原始文件（快，但国内偶发超时） |

- 最多保留 **300 条**（`fetch_qna.py` 的 `MAX_ITEMS`）
- 每条标题：`[平台][公司名代码][命中词] 问题全文`
- 每条正文：`<b>平台</b> | 公司 (代码) | 命中关键词: 词1、词2` + `<b>问:</b> 问题` + `<b>答:</b> 董秘回答`
  → **这个"问/答"标记很重要**：下游推送脚本（`/opt/freshrss-ai/wecom_push.py`）就是靠它把问答切开的
  （FreshRSS 存条目时会把 `<b>` 标签连同里面的字一起清洗掉，所以推送脚本还要按链接回这份上游 feed 取原文）

## 二、数据源（都是公开接口，无需登录）

1. 深交所互动易：`POST https://irm.cninfo.com.cn/newircs/index/search`，`keyWord` 留空 = 全市场最新回答流（JSON）
2. 上证e互动：`GET https://sns.sseinfo.com/ajax/feeds.do?type=11&page=1&pageSize=10&lastid=-1&show=1`（HTML 片段）

每个平台翻 2 页 × 50 条（`MAX_PAGES` / `PAGE_SIZE`），配合 `state.json` 里的 `seen` 做 id 去重。
只抓增量、不回溯历史（全市场历史问答约 8.5 万条，回溯无意义）。

## 三、词库：不在这里，在独立仓库

词库已抽到 **[liuchao88/a-share-keywords](https://github.com/liuchao88/a-share-keywords)**（唯一真源，每周一自动补词）。
本仓库运行时读它的 `keywords/index.json` → 逐个取 `enabled: true` 的行业文件 → 合并词表与权重。

- 取不到就**这一轮不抓**（返回 None 直接退出）：宁可空一轮，也不用过期词库硬筛 —— 那是"隐性漏"（新词命中的问答会被静默丢掉），下一轮自动补上
- 原来的本地词库与 `keywords.txt` 已删除；周任务 `update-keywords.yml` 也已搬去那个仓库
- **加/删词、开关行业，都去 a-share-keywords 仓库改**，本仓库不再存词库副本

匹配规则（`scripts/fetch_qna.py`）：
- 中文词走子串匹配（中文没有词边界）；纯 ASCII 词走词边界（否则 `PD` 会命中 `update`、`IB` 会命中 `subscribe`）
- 命中词按 `signal_weights` 权重排序，**标题里最多挂 4 个**（`top_keywords(limit=4)`，挂一串反而看不清）

## 四、定时任务

### `qna-watch.yml` —— 抓取与发布
名义每 10 分钟（`cron: */10 * * * *`）：抓取 → 词库过滤 → 写 `feed/rss.xml` + `state.json` → 提交 → 通知 jsdelivr 刷新缓存 → 部署 Pages。
单 job 结构（历史上双 job 会发布到旧 HEAD，已修）。

### 每周补词：见 a-share-keywords 的 `update-keywords.yml`
每周一 10:00（北京）从"命中的互动问答 + 财经新闻要闻"里给词库补新词，只加不删，
结果写进行业文件的 `changelog` / `heat` / `weekly_note`，并把周报推到企微群。
本仓库的 `state.json` 里的 `kw_hits`（按 ISO 周的命中统计）是那个任务的输入之一。

## 五、文件清单

| 文件 | 作用 |
|---|---|
| `scripts/fetch_qna.py` | 抓两个平台 → 拉远程词库过滤 → 写 RSS + 状态 |
| `feed/rss.xml` | 产物，被 Pages 发布、被 FreshRSS 订阅 |
| `state.json` | 已推过的条目 id（`seen`）+ 按 ISO 周累计的命中统计（`kw_hits`，补词任务读它算"什么在变热"） |

## 六、谁在消费这份 feed

- **FreshRSS**（阿里云 ECS 上自建）+ 旁路 **AI 语义层**：读条目 → DeepSeek 判 importance/增量 → 打 `AI-高/中/低` 标签，低档自动标已读
- **企微推送**（`/opt/freshrss-ai/wecom_push.py`）：把"AI-高"的问答汇总成一条消息推到企业微信群，正文是问答原文 + 可点击的公司互动页链接
- **Folo**（若作为 GReader 客户端连的是同一套 FreshRSS，会同步看到）

## 七、已知限制（别当成故障）

- **GitHub 免费账户的 schedule 是 best-effort**：名义 10 分钟一次，实测常是几小时一次（有时一天只有 5~6 次）。
  要立刻更新：Actions → qna-watch → Run workflow。
- **关键词层有假阳性**：泛词（如"量化""客户验证"）会误命中，靠下游 AI 层标低档兜住，不要指望关键词层精准。
- **条目不严格按时间排序**：互动易搜索接口是"相关度 + 时间"混合排序。
- 只抓每个平台前 2 页，超过的部分靠下一轮增量补（宁可漏一点，也不全量翻页）。

## 八、常用操作

- **加/删关键词**：去 [a-share-keywords](https://github.com/liuchao88/a-share-keywords) 改（那个仓库的周任务还会自动补充新词）；本仓库不再存词库
- **立刻跑一次抓取**：Actions → `qna-watch` → Run workflow
- **看历史**：`feed/rss.xml`，或在 FreshRSS / Folo 里看

相关文档（本机）：`C:\Users\Liu Chao\Desktop\Hermes\` 下的
`2026-08-30_RSS信息监控系统项目文档.md`、`2026-09-26_RSS信息监控系统速览.md`、`2026-10-01_企微推送v8v9与巨潮调研.md`
