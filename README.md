# Financial Review Docker Repo

Docker-ready version of the Financial Review app.

## First-time setup

```bash
cp .env.example .env
nano .env
mkdir -p data/users
docker compose up -d --build
```

Open:

```text
http://SERVER_IP:8001
```

Change `APP_PORT` in `.env` if you want a different host port.

## Safe update process

From inside this project folder:

```bash
./update.sh
```

The script creates a timestamped backup of `data/` before rebuilding the app.

## Important data rule

Persistent app data is stored here on the host:

```text
./data
```

Do not delete this folder. Docker rebuilds will not erase it.

## Recommended deployment flow

```bash
git init
git add .
git commit -m "Initial Docker repo"
docker compose up -d --build
```

For future updates:

```bash
git pull
./update.sh
```

## Files intentionally not committed

- `.env`
- `data/`
- SQLite databases
- uploaded statements
- Python cache files
