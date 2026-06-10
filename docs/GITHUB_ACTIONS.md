# GitHub Actions deployment

This workflow runs Epic Kiosk as a scheduled one-shot job. It does not provide
the web dashboard.

Use a private repository. The workflow caches browser profiles and cookies under
`app/volumes`, and those files should be treated as account-sensitive state.

## Required repository secrets

Add these in `Settings -> Secrets and variables -> Actions -> Repository secrets`:

- `API_BASE_URL`: OpenAI-compatible API base URL.
- `API_KEY`: API key for the model provider.
- `EPIC_ACCOUNTS_JSON`: Epic accounts, for example:

```json
[
  {
    "email": "account@example.com",
    "password": "your-password"
  }
]
```

## Running

Open `Actions -> Epic Kiosk Claim -> Run workflow`.

The workflow also runs daily at `20:20 UTC` (`04:20 Asia/Shanghai`). Browser
profiles and cookies are cached under `app/volumes` to reduce fresh-login
friction between runs.
