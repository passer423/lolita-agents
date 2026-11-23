# 公网演示：无登录 / 无 Cookie / 无付费 Key 的召回来源实测

- 实测时间：2026-11-23
- 实测环境：Windows + 无痕 headless Edge（Playwright `channel=msedge`）+ 家庭宽带网络；所有请求均**未带任何 Cookie、未登录、未使用任何付费 Key**
- 浏览器类延迟含页面打开与 2–2.5s 渲染等待；HTTP 类延迟含完整响应体
- 目的：为简历里可点击的公网演示挑选能长期使用的来源，不改代码、不部署、不安装依赖

## 一句话结论

**公网演示保留「微博免登录直抓 + Bing 浏览器检索」两路：小红书整体放弃（站内搜索、笔记正文/图片、搜索引擎索引都拿不到），付费搜索、DuckDuckGo/Google/Baidu 不作为依赖；微博只取首屏、靠 6 小时缓存和软失败兜底，不能当唯一来源。**

## 对照表

| 路 | 候选来源 | 能拿到什么 | 拿不到什么 | 是否需要凭证 | 单次延迟 | 风控 / 封禁风险 | 部署代价 |
|---|---|---|---|---|---|---|---|
| 小红书 | 网页搜索页（无登录） | 登录框；0 张笔记卡、0 个笔记链接 | 笔记列表、正文、图片、价格 | 不需要 Key，但**强制登录** | 2.4–3.0s | 低（直接登录墙） | 低（浏览器） |
| 小红书 | 单篇笔记页 / 分享落地 | 无；跳 `login` 或 `404/sec_*` | 标题、正文、图片、价格 | 需要登录态 | 2–3s | 低 | 低 |
| 小红书 | 搜索引擎索引（`site:xiaohongshu.com`） | 0 条小红书笔记；只有抖音/淘宝/贴吧/JD 等站外替代 | 小红书标题、摘要、封面、价格 | 无 | 0.4–3s | 中（Bing 可能验证码） | 低 |
| 小红书 | Spider_XHS | 无 Cookie 时在 readiness 阶段直接失败 | 全部 | `XHS_COOKIE`（a1 + web_session） | 未发起 | 高（461/登录过期） | Node + 外部仓库 |
| 小红书 | Playwright provider | 无 `.logged_in` 扫码档案时不可用 | 全部 | 扫码登录态 | 2–3s | 高 | 浏览器 |
| 微博 | `m.weibo.cn` 裸 HTTP API | 200 但 `ok=-100`，跳 passport 登录 | 帖子、图片、时间 | 游客不行 | 0.2s | 高 | 低 |
| 微博 | Playwright 游客搜索（现有 `weibo_direct`） | 首屏 5 条：正文、`created_at`、作者、`pics`、转评赞；长文可补 | 翻页、评论、历史全量 | 无 | 2.4–3.1s | 中（游客配额/IP 频率） | Chromium 约 200–400MB/次；Linux 需换浏览器内核 |
| 微博 | 付费搜索 API（智谱/博查） | 现状配额已耗尽 | — | Key | — | — | — |
| 网页 | DuckDuckGo html / lite | 无（TCP 连接超时） | — | 无 | >8s 超时 | 网络层不可达 | 低 |
| 网页 | Bing（浏览器渲染） | 10 条标题/URL/摘要，摘要含价格（如 578、885） | 完整正文、小红书笔记 | 无 | 2.4s（3 次稳定） | 中（机房 IP 可能验证码；抓取属灰色） | 浏览器（微博已需） |
| 网页 | Bing（HTTP / RSS，无浏览器） | 中文查询退化：多词 0 条，生僻词只返回「四」字典 | 可用结果 | 无 | 0.3–0.9s | 中 | 低 |
| 网页 | 360 搜索 | 7 条标题/摘要，生僻品牌词也相关 | 可点击 URL（跳转链接返回 400）、小红书 | 无 | 0.38s | 中 | 低 |
| 网页 | Sogou | 通用问题 9 条且链接可解析；生僻品牌词 0 条，连续请求第 2 次起 0 条 | 稳定结果 | 无 | 0.2–0.95s | 中高 | 低 |
| 网页 | Baidu | 第 2 次起滑块验证 | — | 无 | 0.9s | 高 | 中 |
| 网页 | 自建 SearXNG（仅评估） | 只是聚合上述引擎；国内可用上游=360/Bing/Sogou/Baidu，无新增索引 | 独立内容 | 无 | 上游延迟 + 200–500ms | 中 | 约 300MB 镜像、512MB 内存、持续维护 |
| 网页 | Google / Brave / Startpage / Mojeek / Yandex | 超时 / 403 / 验证码 | — | 无 | >8s | 高 | — |

## 1. 小红书：三层都走不通

### 1.1 网页搜索页（无登录）

Headless Edge 打开：

`https://www.xiaohongshu.com/search_result?keyword=四时花 价格&type=51`

结果：

- 最终 URL 仍是搜索页，`<title>` 正常；
- `section.note-item` = **0**，`a[href*='/explore/']` = **0**；
- 正文首屏直接是「登录后查看搜索结果 / 小红书或微信扫码 / 手机号登录」。

不是“结果少”，是**一条都不给**。

### 1.2 单篇笔记页（含 share 落地）

用真实笔记 URL（带 `xsec_token`）测试：

- 浏览器最终跳到 `https://www.xiaohongshu.com/login?redirectPath=...`，0 张卡片、0 个笔记链接；
- 裸 HTTP / 移动 UA 跳到 `website-login/error` 或 `404/sec_*`；
- 伪造 `Googlebot / Baiduspider / facebookexternalhit / Twitterbot` 也一样被送到 `website-login/error` 或 `404/sec_*`，`og:title`、`og:description` 均为空。

关于 share 链接：手头没有真实的 `xhslink.com` token，**没有做端到端实测**；但分享链接最终落到同一个笔记 URL，而该 URL 已实测跳登录/404，因此预期同一堵墙。这一项是本次唯一没有直接样本的测试。

### 1.3 搜索引擎索引

- Bing 浏览器：`site:xiaohongshu.com 四时花 价格` 返回 0 条小红书结果（甚至返回「四」的字典页）；普通查询 `四时花 小红书 价格` 也不返回任何 `xiaohongshu.com` 链接。
- Bing HTTP/RSS：多词中文查询 0 条；单品牌词退化。
- 360：`site:` 查询返回商品/图片聚合，没有笔记；`xiaohongshu.com` 只出现在查询串里。
- Sogou：生僻品牌词 0 条。
- Baidu：第 2 次请求起出现滑块验证。

结论：**没有可用的 SEO 索引层**。索引粒度（标题/摘要/封面/价格）无从谈起，因为索引里根本没有这些笔记。

### 1.4 现有 spider 路径具体失败在哪

- `app/services/xhs_spider.py:61` `readiness()`：没有 `XHS_COOKIE` 时直接返回「缺少 XHS_COOKIE（Spider_XHS 需要登录态）」，**不发任何网络请求**。
- 带过期 Cookie 时：`XHSPcAuth.from_cookie()` / `search_some_note()` 阶段失败，实际报过「bootstrap user/me failed: 登录已过期」。
- Playwright provider：`xhs_search.py` 要求 `data/xhs/profile/.logged_in`，缺失时报「未登录（先运行 python scripts/xhs_search.py --login 扫码）」；即使有 profile，网页搜索本身同样要登录。

## 2. 微博：可以当主路，但只能是“首屏 + 缓存”的主路

### 2.1 实测结果（无 Cookie、无登录）

现有 `scripts/weibo_search.py`（Playwright 游客浏览器）实测：

- 单次约 **2.4–3.1s**；连续 3 次、间隔 3s，均 `ok=true`、5 条结果，没有验证码；
- 返回字段完整：正文、`created_at`、作者、`pics`、转发/评论/点赞、`is_long_text`；
- 样例（`四时花 价格`）里同时拿到：
  - 官方上新号的价格清单：「长op=578；灰白长op=608；短op=558…」；
  - 带 9 张图的帖子；
  - 一条「避雷/倒卖」长文（`is_long_text=true`）；
  - 一条 2023 年的二手价格讨论（888 元）。
- 裸 HTTP 直接打 `m.weibo.cn/api/container/getIndex`（无浏览器）返回 `{"ok":-100,"url":"...passport.weibo.com/sso/signin..."}`，**不能用**；浏览器执行 JS 后拿到的游客态才是可用路径。

### 2.2 能否长期当主路

可以当「实时 UGC 主路」，但定位应是“首屏采样 + 6 小时缓存”，不是全量搜索：

- 只有第一页、约 5 条；翻页会跳登录，不做；
- 不取评论；
- 游客搜索没有官方配额承诺，属于 IP/fingerprint 级风控；本次 3 次连续请求稳定，但不能据此认为可以高频跑；
- 建议：单实例 ≤1 次/2–3s，同一 query 走现有 6h 缓存，失败按通道 skip；监控 `432` / `ok=-100`，出现就退避。

### 2.3 部署到 Linux 需要什么

- 当前脚本写死 `channel="msedge"`（`scripts/weibo_search.py`），**Linux 没有 Edge**。二选一：
  1. 改用 Playwright 自带 Chromium：`playwright install --with-deps chromium`，并把 `channel` 改为 `chromium`（需要一行代码改动）；
  2. 在 Linux 安装 Microsoft Edge 包，继续用 `channel="msedge"`（更重，不推荐）。
- 依赖：Playwright Python 包 + Chromium 内核 + 系统库（`--with-deps` 会装）；磁盘约 250–350MB。
- 内存实测（Windows Edge headless，含 Python/驱动进程）：启动后约 **539MB**，打开搜索页后约 **780MB**，关闭后回落到 **157MB**。Linux Chromium 通常略低，但**建议 2GB 内存**；1GB 机器会很紧。
- 每次查询启动/关闭一个浏览器，进程不常驻；不要把并发查询叠在一起。

## 3. 网页检索：无 Key 方案里只有 Bing 浏览器可用

### 3.1 DuckDuckGo

`html.duckduckgo.com`、`lite.duckduckgo.com` 均 **TCP 连接超时（>8s）**，本网络不可达；无法评估结果质量。若部署在海外，DDG 可通，但对中文 lolita 小众内容覆盖弱，且小红书不允许抓取，仍解决不了本项目最缺的 UGC。

### 3.2 Bing（浏览器渲染）

同一查询 `四时花 预约价` 连续 3 次：

- 延迟 **2418 / 2358 / 2435ms**；
- 每次 10 条结果，前三条完全一致；
- 摘要里直接出现价格：`预约价，jsk短款548，长款568…`、`op加罩纱888`、`578🥕` 等；
- 结果 URL 可直接点击（抖音、Lo研社、贴吧、淘宝、JD）。

这是本次测到的**质量最好、最稳定的无 Key 网页源**。风险：Bing 对机房 IP 可能弹验证码；抓取 HTML 属于灰色地带；没有 SLA。

Bing 的 HTTP/RSS 路径不可用：中文多词查询返回 0 条，生僻词退化成单字字典；必须是浏览器渲染。

### 3.3 360 搜索

- 延迟稳定 **368–387ms**，7 条结果；
- 生僻词也能给出相关标题/摘要（抖音、贴吧、淘宝）；
- 但结果链接是 `so.com/link?m=...` 跳转，带 Referer/Session 仍返回 **400**，**拿不到可点击原 URL**；
- 只适合当“快速文本兜底”，不能当主要引用来源。

### 3.4 Sogou

- 通用问题（`lolita 裙子 怎么洗`）9 条结果，链接可解析到 200；
- 生僻品牌词 0 条；同一查询连续请求，第 2、3 次直接 0 条（约 200ms 返回空）；
- 稳定性不足以做长期依赖。

### 3.5 Baidu

第 1 次能返回结果页，第 2 次起出现滑块/安全验证；不适合无人值守的公网演示。

### 3.6 自建 SearXNG（只评估，未安装）

- 部署：Docker 镜像约 300MB，常驻 200–500MB 内存，需要维护 `settings.yml` 与引擎可用性；
- 它自己不产生索引，只是聚合上游。国内服务器上能用的上游仍是 360/Bing/Sogou/Baidu；Bing 中文需浏览器、Baidu 有验证码、Sogou 不稳定，**自建不会比直接解析 360/Bing 更好**；
- 结论：本次不推荐为演示专门部署 SearXNG。

## 4. 推荐给公网演示的配置

1. **微博**：保留 `weibo_direct`（Playwright 游客首屏）。Linux 部署时换成自带 Chromium；保留 6h 缓存与超时 skip；UI 上标注“实时来源仅首屏、可能不完整”。
2. **网页**：新增/使用 **Bing 浏览器检索** 作为唯一无 Key 网页源；360 只做文本兜底，Sogou/Baidu/DDG/Google 不接入。
3. **小红书**：整体关闭。站内搜索、笔记正文/图片、分享页、搜索引擎索引都无法在无登录下拿到；继续保留只会让演示看起来“有通道但永远空”。代码、配置、`xhs_account` 登记都保留，只由 `XHS_ENABLED=false` 关闭；关闭时召回直接跳过并如实报状态。
4. **品牌范围**：`BRAND_SCOPE=jp`。国牌品牌条目、译名、账号登记仍在数据文件里，但不参与识别与检索；评测集不删题，`scope=cn` 与整条关闭通道题跳过并单列命中。
5. **网页兜底**：只有官网/微博都没拿到证据才补网页，硬超时 `WEB_FALLBACK_TIMEOUT=6` 秒。
6. **明确放弃**：小红书笔记与图片、微博翻页/评论、任何需要登录或付费 Key 的搜索；不要为了演示去保存真实 Cookie/Key。

## 5. 残留风险与未测项

- 本次是家庭宽带 IP；部署到阿里云/腾讯云等机房 IP 后，Bing 验证码、微博 `432` 的概率会上升，**上线前必须在目标服务器复测一次**。
- 未测海外服务器：海外可通 DDG/Google，但小红书/微博对海外 IP 的策略可能更严，不能直接外推。
- 未拿到真实 `xhslink.com` 分享 token，分享落地页未端到端实测（预期与笔记页同墙）。
- 未做并发/压测；浏览器路径一次只能跑一个查询。

## 6. 复现方式（不含任何凭证）

```powershell
# 微博：无 Cookie 游客浏览器
python scripts/weibo_search.py --query "四时花 价格" --limit 5 --timeout 25

# 小红书：直接打开搜索页/笔记页观察登录墙（无需登录）
# https://www.xiaohongshu.com/search_result?keyword=四时花%20价格&type=51
# https://www.xiaohongshu.com/explore/<note_id>?xsec_token=...

# 网页：Bing 浏览器渲染
# https://cn.bing.com/search?q=四时花+预约价
```

本次实测未改动任何代码、未安装依赖、未写入任何真实 Cookie 或 Key；本文档也未提交。
