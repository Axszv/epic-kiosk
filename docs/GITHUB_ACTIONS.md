# GitHub Actions 自动领取

该 workflow 会以一次性任务运行 Epic Kiosk，不提供 Web 后台。

建议使用私有仓库。workflow 会缓存 `app/volumes` 下的浏览器 profile 和 Cookie，这些文件应视为账号敏感状态。

## 必需 Secrets

在 `Settings -> Secrets and variables -> Actions -> Repository secrets` 中添加：

- `API_BASE_URL`：OpenAI-compatible API 地址。
- `API_KEY`：模型服务商 API Key。
- `EPIC_ACCOUNTS_JSON`：Epic 账号列表。所有账号放在同一个 Secret 的 JSON 数组中，例如：

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

账号按数组顺序编号。JSON 必须使用英文双引号，不能包含注释或尾随逗号；不要为每个账号分别创建 Secret。

可选：

- `SERVERCHAN_SENDKEY`：Server酱 SendKey。配置后，运行结束会推送中文领取结果。
- `EPIC_BROWSER_PROXY`：仅供手动网络对照使用的代理 URL，支持 HTTP、HTTPS、SOCKS5，以及 VLESS/TLS/WebSocket 节点；例如 `http://username:password@proxy.example:3128`。用户名或密码中的特殊字符需做 URL 编码。代理必须能从 GitHub runner 访问，本机 `127.0.0.1` 地址不可直接用于托管 runner。

## 运行方式

手动运行：打开 `Actions -> Epic Kiosk Claim -> Run workflow`。

定时运行：`Epic Kiosk Claim` 每周三、周五北京时间 08:23 和 20:23 自动运行。

workflow 会将运行日志、页面截图和 `github_actions_summary.json` 上传为 artifact，保留 14 天。

## 登录事务的网络对照

若 hCaptcha 已通过，但 Epic 仍返回 `captcha_invalid`，不能据此认定领取成功，也不能只凭该错误断定是 IP 问题。可在同一修复分支、同一账号上保持其他输入一致，只改变网络出口进行对照。

先用 `gh secret set EPIC_BROWSER_PROXY --repo OWNER/REPO` 交互式保存代理地址，再在手动运行中设置 `use_browser_proxy=true`。该选项缺省为 `false`，不改变现有定时任务的出口；启用但没有 Secret 时会明确失败。代理只传给 Epic 浏览器，模型 API 保持原有连接配置。不要把代理密码填进 workflow 输入或提交到仓库。

Camoufox 使用代理时会通过同一代理查询出口，并用 GeoIP 数据同步时区、语言和 WebRTC 地址。此设置不能固定代理服务本身轮换的出口，也不能保证 Epic 接受验证码。

对于 dae 原来使用的 VLESS/TLS/WebSocket 节点，保存完整节点 URL 即可。Actions 会下载官方固定版本 Xray 并校验 SHA-256，在 runner 的 `127.0.0.1` 启动 HTTP 代理；节点的连接地址、TLS SNI、WebSocket Host 和路径分别保留。TLS 证书验证保持开启，不支持的协议参数会明确拒绝。节点配置只写入权限受限的临时目录，启动后删除，任务结束时停止代理进程；不会上传配置或客户端日志。

还可同时设置 `proxy_check_only=true`，仅检查代理连接和访问 Cloudflare/Amazon 两个公开检测端点时的出口一致性。每个端点取两次样本，只记录样本数量、出口数量及是否一致，不记录 IP。此模式不安装浏览器、不登录 Epic、不调用模型，也不改浏览器缓存或发送领取通知。不同站点的出口不一致只能说明该节点不提供统一出口，不能单凭这一结果断定 Epic 拒绝验证码的原因。

验收仍需电脑端和移动端的实际 `DESKTOP_RESULT` / `MOBILE_RESULT` 入库证据；登录成功和验证码通过均不足以通过验收。

## 移动端周免

每次 workflow 开始时会通过 Epic 官方移动端接口分别发现 Android 和 iOS 本周免费游戏。发现只执行一次，结果会传给所有账号。

每个账号使用同一个登录会话依次执行：

1. 领取电脑端周免。
2. 领取 Android 周免。
3. 领取 iOS 周免。

移动端每个 offer 在一次账号尝试中最多提交一次结账，随后刷新商品页并要求出现 `In Library` 或 `Owned`。如果只完成结账提交但未确认入库，该账号会进入现有的下一轮重试；已成功的账号不会重跑。锁区游戏记为“锁区跳过（未领取）”，不会导致账号整体失败。
