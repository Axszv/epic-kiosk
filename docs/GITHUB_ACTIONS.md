# GitHub Actions 自动领取

该 workflow 会以一次性任务运行 Epic Kiosk，不提供 Web 后台。

建议使用私有仓库。workflow 会缓存 `app/volumes` 下的浏览器 profile 和 Cookie，这些文件应视为账号敏感状态。

## 必需 Secrets

在 `Settings -> Secrets and variables -> Actions -> Repository secrets` 中添加：

- `API_BASE_URL`：OpenAI-compatible API 地址。
- `API_KEY`：模型服务商 API Key。
- `EPIC_ACCOUNTS_JSON`：Epic 账号列表，例如：

```json
[
  {
    "email": "account@example.com",
    "password": "your-password"
  }
]
```

可选：

- `SERVERCHAN_SENDKEY`：Server酱 SendKey。配置后，运行结束会推送中文领取结果。

## 运行方式

手动运行：打开 `Actions -> Epic Kiosk Claim -> Run workflow`。

定时运行：`Epic Kiosk Claim` 每周三、周五北京时间 08:23 和 20:23 自动运行。

workflow 会将运行日志、页面截图和 `github_actions_summary.json` 上传为 artifact，保留 14 天。

## 移动端周免

每次 workflow 开始时会通过 Epic 官方移动端接口分别发现 Android 和 iOS 本周免费游戏。发现只执行一次，结果会传给所有账号。

每个账号使用同一个登录会话依次执行：

1. 领取电脑端周免。
2. 领取 Android 周免。
3. 领取 iOS 周免。

移动端每个 offer 在一次账号尝试中最多提交一次结账，随后刷新商品页并要求出现 `In Library` 或 `Owned`。如果只完成结账提交但未确认入库，该账号会进入现有的下一轮重试；已成功的账号不会重跑。锁区游戏记为“锁区跳过（未领取）”，不会导致账号整体失败。
