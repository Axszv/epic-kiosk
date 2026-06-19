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

定时运行：由 `Epic Kiosk Claim Scheduled` 触发，每周三、周五北京时间 08:23 和 20:23。

workflow 会将运行日志、页面截图和 `github_actions_summary.json` 上传为 artifact，保留 14 天。
