# BiliSonic 维护手册

这份文档解决的核心问题：**B 站接口、播放页和 CDN 规则会变，如何让下载器坏得慢、坏得可发现、修得快。**

## 1. 设计上如何对抗变化

```text
URL 输入
  │ urls.py（纯函数、域名白名单、BV/av/b23 解析）
  ▼
ExtractorChain（extractors/__init__.py）
  ├─ 1. BilibiliApiExtractor   官方 JSON API：view + playurl（首选，质量信息最全）
  └─ 2. YtDlpExtractor         yt-dlp 兼容解析（接口/页面变化时的兜底；番剧也走这里）
  ▼
quality.py（Hi-Res FLAC > Dolby > 最高码率 AAC > 混合流）
  ▼
downloader.py（流式、Range 续传、备用 CDN、按质量降级）
  ▼
converter.py（ffmpeg 无损封装/转码）
```

四个关键原则：

1. **不自己爬 HTML。** B 站播放页是混淆过的 JS，改版频繁。官方 JSON API 才是低维护面；HTML 解析交给维护活跃的 yt-dlp。
2. **双解析路径。** 官方 API 抛 `SchemaChangedError` 或非致命错误时自动调用 yt-dlp；UI 仍能出结果。`doctor` 会明确告诉你“官方契约失败、已降级”。
3. **契约测试 + 脱敏 fixture。** `tests/fixtures/*.json` 保存真实响应的**结构**（URL 全部替换为 `example.invalid`），字段消失会直接让测试失败；新增字段则通过刷新 fixture 的 Git diff 被发现。
4. **诊断命令是一等公民。** `biliaudio doctor` 把本机问题、上游接口、Cookie 权限分开报告。排障从它开始，不靠猜。

## 2. 故障速查表

| 症状 | 最可能原因 | 动作 |
|---|---|---|
| `doctor` 说 ffmpeg 未就绪 | 未安装/路径错 | `brew install ffmpeg`；`.env` 设置 `BILI_FFMPEG` |
| `doctor` 在线接口失败，提示风控/412 | B 站风控、IP 异常、Cookie 缺失 | 稍后重试；配置 SESSDATA；不要高频批量请求 |
| 官方 API 契约失败但 yt-dlp 成功 | B 站字段/接口变化 | 更新 yt-dlp 可先恢复；再按 §5 更新 parser/fixture |
| 两个解析器都失败 | 页面类型不支持、地区限制、接口大改 | 先 `pip install -U yt-dlp`；抓取响应结构；按 §5 修 |
| 任务失败 “媒体链接已过期/403” | CDN 链接过期，或并发下载冲突 | 重试；程序会重新解析；确认系统时间正确 |
| 只有常规 AAC、flac 为 null | 视频没有 Hi-Res，或账号无权限 | 属正常；在 UI 登录账号后再试，不要承诺用户一定有“无损” |
| 登录提示 Cookie 无效/过期（B 站 code -101） | SESSDATA 过期或复制错误 | UI 重新登录；确认浏览器仍处于登录状态；不要复用旧 Cookie |
| 登录后仍看不到 Hi-Res | 该视频没有 Hi-Res，或账号会员等级不足 | 属正常；检查视频本身与会员权益 |
| FLAC 文件很大 | AAC 源被无损封装 | 正常；建议用户改选“原始音轨” |
| 文件名异常/过长 | 新标题形态（含 emoji、超长中文） | `naming.py` + `test_naming.py` 已限制 200 UTF-8 字节；加样例 |
| 队列重启后为空 | 设计如此（内存队列） | 如需持久化，按 README API 扩展；不要悄悄落库 Cookie/URL |
| SSE 断开但下载继续 | 浏览器/代理断流 | 前端自动降级轮询；下载在后端继续 |

## 3. 账号登录与 Cookie 维护

UI 登录链路（`auth.py` → `POST /api/auth/cookie`）只做三件事：

1. 用用户提供的 Cookie 请求 `https://api.bilibili.com/x/web-interface/nav` 验证 `isLogin`；
2. 只保留 `SESSDATA`（以及匿名 `buvid3/4`，如输入里带有），丢弃 `bili_jct` 等写操作凭据；验证通过后写入 `~/.config/bilisonic/cookie.json`（目录 0700、文件 0600），并更新内存中的 `AuthStore` 与共享 httpx cookie jar；
3. 返回不含 Cookie 值的账号摘要（uname/mid/会员状态/来源）。

维护要点：

- **不要改成自动登录/扫码轮询/密码登录**。这些会增加凭据面与账号风险，且需要针对 passport 接口做长期维护；当前方案只复用浏览器已有会话，是刻意的最小化设计。
- 登录校验依赖 `nav` 接口。若 B 站修改该接口：`POST /api/auth/cookie` 会报错但不会保存 Cookie；先更新 `auth.py:NAV_URL`/字段映射，补 `tests/test_auth.py` 的 mock。
- Cookie 优先级：**UI 保存的 stored > `BILI_COOKIE`/`BILI_SESSDATA` > `BILI_COOKIE_FILE`**。UI 退出会删除 stored 并回退到环境配置；不要在没有提示的情况下改这个优先级。
- 文件名/路径：`BILI_CONFIG_DIR` 可改配置目录；写文件使用 0600 临时文件 + `os.replace`，不要在日志、API、异常里输出 Cookie。
- 失效处理：`-101` 表示未登录；412 表示风控。两者都不要自动重试/自动刷新。UI 展示“重新登录/稍后再试”即可。
- 账号风险：任何第三方客户端都无法保证零风险。保持 `BILI_SAFE_MODE=true`（单任务 + 随机间隔）；不要在维护中为了“快”提高并发或缩短 `BILI_TASK_INTERVAL`，这会直接增加风控概率。
- 不要记录账号 Cookie 到 Git、issue、截图；文档中的示例只能是占位符。`AuthStore.public_state()` 是唯一允许返回给前端的账号结构，审阅 PR 时重点检查它没有泄露 `cookie` 字段。

## 4. 日常巡检（建议每周/每次依赖升级后）

```bash
# 1) 离线回归（CI 必跑，不访问网络）
.venv/bin/python -m pytest -q
.venv/bin/python -m bili_audio doctor --offline

# 2) 在线契约探测（只解析，不下载）
.venv/bin/python -m bili_audio doctor
.venv/bin/python -m bili_audio doctor --url 'https://www.bilibili.com/video/BV...'

# 3) 真实短样端到端（手动，确认下载+转码+命名）
./run.sh --no-browser --output-dir /tmp/bilis-e2e
# 在 UI/API 添加一个短视频，确认 output 下出现 .flac 且可播放
```

`doctor --json` 适合接入 CI/监控；`doctor --strict` 在出现 warning（例如官方契约失败但 yt-dlp 兜底成功）时返回退出码 2。检查项包括：Python、ffmpeg/ffprobe、输出目录可写/磁盘余量、Cookie 是否配置、yt-dlp 版本、离线 fixture 契约、在线 view/playurl 解析、最终音轨与解析器。

**基线记录**：每次升级前把 `doctor --json` 输出和当前 `pytest` 结果存档，变化更容易定位。

## 5. 官方 API 变化时的修复流程（重点）

### Step 0：确认不是本机问题

- `doctor` 中 ffmpeg/目录/网络失败 → 先修环境。
- 只有在线接口失败，本地 fixture 正常 → 上游变化，继续。
- 官方解析失败但 yt-dlp 成功 → 应用仍可用；属于“可延迟修复”的契约变化。

### Step 1：先更新兼容解析器（通常 1 分钟恢复）

```bash
.venv/bin/pip install -U yt-dlp
.venv/bin/python -m bili_audio doctor
```

yt-dlp 社区通常比本项目更快跟进 B 站变化。更新后若 `doctor` 显示“解析器 yt_dlp”且能选到正常音质，用户侧已恢复；再安排官方解析器修复以获得更完整质量选项。

### Step 2：抓取并对比响应结构

```bash
# 更新脱敏 fixture；默认根据是否有 Hi-Res 选出文件名
.venv/bin/python scripts/update_fixtures.py --url 'https://www.bilibili.com/video/BV...'

git diff tests/fixtures/
```

- 只看 **字段/结构差异**；脚本会把所有签名 URL 替换成 `example.invalid`，不会提交 Cookie。
- 若需要记录受权限保护的 Hi-Res，先配置 `.env` Cookie，再运行脚本。
- 新增字段 → 考虑要不要支持；字段改名/消失 → 修改 parser。

### Step 3：定位与修改

| 变化位置 | 改这里 | 同步测试 |
|---|---|---|
| URL 形式/新域名/短链 | `src/bili_audio/urls.py` | `tests/test_urls.py` |
| view 字段（title/cid/pages/owner） | `extractors/bilibili.py: _fetch_view` | `tests/test_extractor.py` |
| playurl 字段（dash/flac/dolby/durl） | `extractors/bilibili.py: parse_playurl` | `tests/test_quality.py` + fixture |
| 音质 id/优先级 | `quality.py` | `tests/test_quality.py` |
| 错误码/风控 | `_get_api` / `_BILI_ERRORS` | 手动 doctor + 单测按需 |
| 新签名/WBI/鉴权 | `_get_api`；或暂由 yt-dlp 兜底 | 增加 mock 测试 |
| 页面类型（番剧等） | `YtDlpExtractor` 或新增 extractor | `tests/test_extractor.py` |
| CDN 行为（Range/403/HTML） | `downloader.py` | `tests/test_downloader.py` |
| ffmpeg 输出/容器 | `converter.py` | `tests/test_converter.py` |

修改原则：

- **容错解析**：同时接受 camelCase/snake_case；可选字段缺失不崩；关键字段缺失必须抛 `SchemaChangedError`，让链路切到 yt-dlp，而不是 500。
- **错误信息要可行动**：中文说明 + 下一步（配置 Cookie、重试、更新依赖）。错误码稳定（`upstream_schema_changed` 等），方便日志检索。
- **不打印签名 URL/Cookie**：日志只记质量 id、大小、错误类型。

### Step 4：补测试并锁定契约

1. 刷新/新增 fixture（尽量小；媒体 URL 必须是唯一的 `example.invalid`，否则去重逻辑会误删音轨）。
2. 在 `test_quality.py` 断言新的字段形态仍能选出正确最佳音轨。
3. 在 `test_extractor.py` 用 `respx` 模拟 view/playurl，断言 title、最佳 id、错误映射。
4. `pytest -q` 全绿，再 `doctor --offline`。
5. 在 PR/提交信息写清 B 站变化、影响范围、验证视频 BV 号（不要写 Cookie）。

### Step 5：发布检查清单

- [ ] `pytest -q` 全绿
- [ ] `doctor --offline` 全绿
- [ ] `doctor`（在线）正常，且明确记录使用的解析器
- [ ] 手动跑一个短视频，输出文件名/格式/可播放性正常
- [ ] 检查 `git diff` 没有 Cookie、签名 URL、大媒体文件、`output/`
- [ ] 更新 `CHANGELOG`/README（如行为变化）
- [ ] 依赖版本（尤其是 yt-dlp）在 README/提交中说明

## 6. 依赖更新策略

| 依赖 | 策略 | 原因 |
|---|---|---|
| `yt-dlp` | 每月或出现故障时 `pip install -U yt-dlp` | 兼容解析主力，更新最快 |
| `fastapi` / `starlette` | 季度，跑完整测试 | Web 层，变化较慢 |
| `httpx` | 季度 | 下载/接口客户端 |
| `ffmpeg` | 半年或需要新编码器时 `brew upgrade ffmpeg` | 转码正确性 |
| Python | 跟随 3.10+；当前开发 3.14 | 类型语法/异步行为 |

`pyproject.toml` 故意使用宽松上界；**不要**为了“可复现”把 yt-dlp 锁死在老版本——它承担了对抗 B 站变化的主要职责。若生产环境需要锁版本，请把 yt-dlp 单列为可热更新的部署项。

## 7. 观测与日志

- 终端日志按模块输出，HTTP 请求日志已降噪；任务失败会记录 `error code + 消息`，下载候选失败记录质量 id。
- UI 的 warning 字段保留：降级、无 Cookie、无 Hi-Res 权限、flac 不提升音质等。
- 建议排查顺序：UI `warnings/error` → 账号状态（`/api/auth`）→ 终端日志 → `doctor` → 手工 curl B 站 API（带相同 UA/Referer）→ fixture diff。
- 不要把 SESSDATA 贴到 issue/聊天；日志中的 Cookie 已脱敏，但命令行历史仍需注意。

## 8. 安全维护

- 只允许 B 站域名（`urls.py` 白名单）；新增域名必须同时改测试，防止 SSRF。
- 媒体下载不带 Cookie；若未来 CDN 要求 Cookie，必须重新评估泄露面并限制域名。
- 前端所有用户可控文本用 `textContent`/`esc()`；新增动态 HTML 先过 XSS 检查。
- CSP 禁止内联脚本/外部资源；新增依赖/字体前先确认不需要 CSP 放宽。
- `reveal` 只能打开输出目录内文件；`/audio` 通过 task id 查找并校验 `is_inside`。
- 默认只监听 `127.0.0.1`；如果要远程访问，必须加认证/反代与 TLS，而不是改 host 了事。
- `server.py` 中间件校验 `Host`（防 DNS rebinding）和写操作的 `Origin`（防跨站 POST）。修改访问方式或引入反向代理时，同步更新 `_host_is_allowed` 并补测试。
- 日志安装了 `_RedactFilter`，即使未来代码误把 Cookie 放入日志也会被替换为 `***`；不要因此放松代码审查。
- 账号 Cookie 属于高敏感数据：只允许存在 `AuthStore` 内存与 `~/.config/bilisonic/cookie.json`（0600）；新增 API/日志/异常时必须检查不会回显它。登录校验使用独立 httpx client，不要把 Cookie 写进共享默认 headers。
- UI 的账号接口同样受 Host/Origin 中间件保护；新增登录相关 mutation 不要绕过。

## 9. 扩展点

- **新增站点**：实现 `Extractor` 风格的类，接入 `ExtractorChain`；保持“解析”和“下载/转码”分离。
- **持久化历史**：可在 `TaskManager` 外围加 SQLite/JSON，但不要持久化 Cookie 和带签名的媒体 URL（会过期且敏感）。
- **更多输出格式**：在 `FORMAT_META`、`naming.extension_for`、`converter._build_args` 三处同步修改，并补转换测试。
- **重试/调度策略**：当前固定 1 并发 + 线性队列；如需并发，注意 `_reserved_paths`、SSE 节流和 CPU 风扇。
- **代理**：`BILI_PROXY` 已贯通 API 和下载客户端；新增请求时记得使用统一 `create_client`/`media_headers`。

## 10. 已知限制

- 队列状态不持久化（设计取舍：无数据库、无敏感 URL 落盘）。
- 番剧/PGC 走 yt-dlp，质量选项可能少于官方 API；付费/地区限制不会绕过。
- 没有 WBI 签名实现；若 playurl 未来要求 WBI，优先依赖 yt-dlp 兜底或按 §5 增补。
- 只支持 B 站；不包含视频下载，只抽音频。
- 临时文件在 `output/.work/`；异常断电后启动会清理 24 小时前的残留，正在运行的实例不要并行启动第二个。

## 11. 版本与变更记录

- `0.1.0`：首个可用版本；官方 API + yt-dlp 双解析、FLAC/WAV/M4A、SSE UI、doctor、契约测试。
- 版本号在 `src/bili_audio/__init__.py` 与 `pyproject.toml` 同步修改。
