# Epic Kiosk - GitHub Actions 自动领取

![GitHub Actions](https://img.shields.io/badge/GitHub_Actions-Enabled-2088FF?logo=githubactions&logoColor=white)
![Python](https://img.shields.io/badge/Python-3.12-yellow?logo=python)
![Status](https://img.shields.io/badge/Status-Stable-green)
![License](https://img.shields.io/badge/License-GPL--3.0-blue)

通过 GitHub Actions 为多个 Epic 账号自动领取电脑端、Android 和 iOS 每周免费游戏。workflow 会复用浏览器登录状态、处理 hCaptcha、严格验证入库结果，并在运行结束后发送中文通知。

建议使用私有仓库。Actions Cache 中保存了 Epic 登录 Cookie 和浏览器会话，应视为敏感账号数据。

## 功能

| 功能 | 说明 |
|------|------|
| 电脑端周免 | 自动发现并领取 Epic Games Store 电脑端每周免费游戏 |
| 移动端周免 | 通过 Epic 官方接口发现并领取 Android、iOS 每周免费游戏 |
| 多账号 | 使用一个 `EPIC_ACCOUNTS_JSON` Secret 按顺序运行多个账号 |
| 会话复用 | 按账号保存浏览器 profile，后续运行尽量复用 Cookie 和 Session |
| 验证码处理 | 使用 OpenAI-compatible 视觉模型处理登录和结账 hCaptcha |
| 失败重试 | 每个账号默认最多尝试 3 次，只重试尚未完成的账号 |
| 严格校验 | 必须确认已拥有、已入库或因账号地区锁区跳过，账号才算完成 |
| 中文通知 | 可通过 Server酱发送电脑端、Android、iOS 分组结果 |
| 排查证据 | 保存运行日志、页面截图、HTML 快照和结构化结果 artifact |

## 自动领取流程

每次 workflow 按以下顺序执行：

1. 通过 Epic 官方移动端接口分别发现 Android 和 iOS 本周免费游戏，发现结果只请求一次并传给所有账号。
2. 从 Actions Cache 恢复每个账号的浏览器 profile 和登录状态。
3. 账号 1 使用同一个浏览器会话依次领取电脑端、Android、iOS 周免。
4. 按相同顺序处理后续账号。
5. 对失败账号启动新的浏览器进程重试；已经完成的账号不会重跑。
6. 保存最新浏览器状态，上传排查 artifact，并发送 Server酱通知。

## 配置 Secrets

打开仓库：

`Settings -> Secrets and variables -> Actions -> Repository secrets`

添加以下 Secrets：

| Secret | 必需 | 说明 |
|--------|------|------|
| `API_BASE_URL` | 是 | OpenAI-compatible API 地址，例如 `https://api.example.com/v1` |
| `API_KEY` | 是 | API Key，所用服务必须支持视觉输入 |
| `EPIC_ACCOUNTS_JSON` | 是 | Epic 多账号 JSON 数组 |
| `SERVERCHAN_SENDKEY` | 否 | Server酱 SendKey，用于接收中文运行结果 |

当前 workflow 使用 `agnes-2.0-flash` 处理页面判断和 hCaptcha。`API_BASE_URL` 对应的服务必须提供该模型。

### 多账号格式

点击 `New repository secret`：

- Name：`EPIC_ACCOUNTS_JSON`
- Secret：填写完整的 JSON 数组

```json
[
  {
    "email": "account1@example.com",
    "password": "account-1-password"
  },
  {
    "email": "account2@example.com",
    "password": "account-2-password"
  },
  {
    "email": "account3@example.com",
    "password": "account-3-password"
  }
]
```

填写要求：

- 所有账号放在同一个 Secret 中，不要为每个账号分别创建 Secret。
- `email` 和 `password` 必须使用英文双引号。
- 最后一项后面不能有逗号，JSON 中不能添加注释。
- 账号按数组顺序编号：第一项是账号 1，第二项是账号 2，以此类推。
- 如果密码包含 `"` 或 `\`，在 JSON 中分别写成 `\"` 或 `\\`。

## 运行方式

### 定时运行

`Epic Kiosk Claim` 当前在以下时间自动运行：

- 每周三北京时间 08:23、20:23
- 每周五北京时间 08:23、20:23

GitHub Actions 的 schedule 可能延迟投递，实际开始时间不保证精确到分钟。

### 手动运行

打开：

`Actions -> Epic Kiosk Claim -> Run workflow`

可选参数：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `account_index` | 空 | 只运行指定的账号编号，优先级高于 `account_limit` |
| `account_limit` | 空 | 只运行数组中的前 N 个账号 |
| `execution_timeout` | `120` | hCaptcha 单次等待秒数 |
| `login_result_timeout` | `180` | Epic 登录结果等待秒数 |
| `account_max_attempts` | `3` | 每个账号最多尝试次数 |

## 结果判定

以下状态算作成功证据：

- `already_owned`：商品已经在账号库中。
- `verified_owned`：本次领取后已确认入库。
- `region_unavailable`：账号所在地区锁区，记录为“锁区跳过（未领取）”。

以下情况不会算成功：

- 只点击了领取或提交了结账，但没有确认 `In Library` / `Owned`。
- 登录、浏览器或验证码流程异常。
- 没有找到购买按钮或无法确认游戏免费。

电脑端或移动端任意游戏缺少严格证据时，本次账号尝试失败并进入下一轮。最终通知会分别显示电脑端、Android、iOS 和重试记录。

## 缓存与安全

workflow 使用 `app/volumes/user_data/<账号邮箱>` 保存每个账号的浏览器 profile，并通过 Actions Cache 在不同运行之间恢复。

- profile 中包含有效 Cookie、Session 和本地存储数据，不要用于公开仓库或不可信 PR workflow。
- GitHub 默认每个仓库提供 10 GB Actions Cache。超出默认上限时会按最近最少使用顺序删除旧缓存。
- 每次运行会恢复上一份最新快照，并用新的 run ID 保存更新后的浏览器状态。
- 缓存失效、被清理或 Epic 主动注销会话后，workflow 会使用 Secret 中的账号密码重新登录。

## 日志与 artifact

每次运行结束后会上传 `epic-kiosk-logs` artifact，默认保留 14 天，主要包括：

- `logs/runtime-*.log`：完整运行日志。
- `logs/error-*.log`：错误日志。
- `runtime/github_actions_summary.json`：每个账号、每次尝试及各平台证据。
- `runtime/mobile_discovery_summary.json`：Android/iOS 周免发现结果。
- `runtime/*.html`、`*.png`、`*.json`：失败页面快照和调试信息。

## 故障排查

### workflow 没有按计划时间运行

确认 workflow 文件位于默认分支且 Actions 已启用。GitHub schedule 可能延迟数分钟甚至更久；应根据最终是否出现 `Scheduled` 运行记录判断，不以设定分钟是否准时开始作为唯一依据。

### `EPIC_ACCOUNTS_JSON` 解析失败

检查 Secret 是否为合法 JSON 数组，重点检查英文双引号、尾随逗号以及密码中的 `"`、`\` 转义。

### `ERROR_TYPE:unknown` 或 `FINAL_ERROR:unknown`

通常表示 Epic/Cloudflare 登录页面状态不明确、浏览器连接异常或登录表单超时。账号会在下一轮使用新的浏览器进程重试。

### `Wait for captcha response timeout`

表示 hCaptcha 在设置的等待时间内没有完成。检查模型 API、额度、网络出口和 Epic 风控情况。

### `missing_desktop_ownership_evidence`

电脑端游戏没有找到严格入库证据。下载 artifact，检查商品页截图、HTML 和完整运行日志。

### `missing_mobile_ownership_evidence`

Android 或 iOS 游戏没有找到严格入库证据。查看通知中的具体平台，并检查 `MOBILE_RESULT` 与移动端页面快照。

### `This content is currently unavailable in your platform or region.`

该游戏对账号地区锁区。workflow 会记录“锁区跳过（未领取）”，继续处理其他游戏，不会因此让账号整体失败。

## 相关文件

```text
.github/workflows/epic-claim.yml       # schedule、Secrets 和 Actions 执行步骤
app/deploy.py                          # 单账号浏览器入口
app/services/epic_authorization_service.py  # Epic 登录和会话验证
app/services/epic_games_service.py     # 电脑端发现、领取和入库验证
app/services/epic_mobile_service.py    # Android/iOS 页面领取和结果标记
scripts/mobile_offer_discovery.py      # 官方移动端周免接口发现
scripts/github_actions_claim_once.py   # 多账号调度、重试和严格证据汇总
scripts/serverchan_notify.py           # Server酱中文通知
```

更多细节参见 [GitHub Actions 自动领取文档](docs/GITHUB_ACTIONS.md)。

## 免责声明

本项目仅供学习和技术研究使用。请合理使用并遵守 Epic Games 服务条款。使用者应自行承担账号、网络、API 费用和自动化操作相关风险。
