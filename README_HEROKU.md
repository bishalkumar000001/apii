# Heroku Deployment

This version is prepared for Heroku. It does not run `install.sh` during startup, does not create a Python virtualenv, and uses Heroku's `$PORT`.

## Required buildpacks

Add these buildpacks to the Heroku app:

```bash
heroku buildpacks:clear
heroku buildpacks:add heroku/python
heroku buildpacks:add heroku/nodejs
heroku buildpacks:add https://github.com/heroku/heroku-buildpack-apt
```

The `Aptfile` installs FFmpeg. `package.json` provides Node.js for yt-dlp's EJS JavaScript runtime.

## Config Vars

```text
COOKIE_URL=https://raw.githubusercontent.com/themagmalord333-oss/COOKIE/main/cookies.txt
DOWNLOAD_DIR=downloads
CACHE_EXPIRE_HOURS=24
MAX_VIDEO_QUALITY=720
DOWNLOAD_WORKERS=4
CONCURRENT_FRAGMENT_DOWNLOADS=15
HTTP_CHUNK_SIZE=10485760
SOCKET_TIMEOUT=15
RETRIES=5
FRAGMENT_RETRIES=5
```

Do **not** set `PORT`; Heroku supplies it automatically.

## Deploy

```bash
git add .
git commit -m "Prepare API for Heroku"
git push heroku main
```

If deploying from GitHub, connect the repository and deploy the branch normally after adding the buildpacks and Config Vars.

## Health check

After deployment:

```text
https://YOUR-APP-NAME.herokuapp.com/health
```

## Important

Heroku's dyno filesystem is ephemeral. `downloads/` and `cache.db` can be removed when the dyno restarts or is redeployed. Use external object storage/database if downloaded files or cache must persist.

`cookies.txt` is intentionally not committed to this package. The app downloads it at startup from `COOKIE_URL` when that Config Var is set.

## API Key Authentication (v2.3.0)

The API now protects `/search`, `/thumbnail`, `/download`, `/video`, and `/files/{filename}` with an API key.

### 1. Create a strong API key

On Windows PowerShell:

```powershell
[Convert]::ToBase64String((1..48 | ForEach-Object { Get-Random -Maximum 256 }))
```

Or use any cryptographically random 32+ character secret.

### 2. Add it to Heroku

```bash
heroku config:set API_KEY="YOUR_GENERATED_KEY" --app music-api-021d06c29284
```

Do not put the key in GitHub or share it publicly.

### 3. Use the key from your Music Bot

Send the key in the HTTP header:

```text
X-API-Key: YOUR_GENERATED_KEY
```

A Bearer token is also accepted:

```text
Authorization: Bearer YOUR_GENERATED_KEY
```

### Public endpoints

`GET /` and `GET /health` remain public so uptime/health checkers can verify that the API is online.

### Protected endpoints

- `GET /search`
- `GET /thumbnail`
- `GET /download`
- `GET /video`
- `GET /files/{filename}`

Without a valid key these return HTTP `401`.
If `API_KEY` is missing from Heroku, protected endpoints return HTTP `503` so an accidentally unsecured deployment is not possible.
