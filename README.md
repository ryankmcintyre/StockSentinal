# StockSentinal

StockSentinal is a FastAPI app for tracking stock positions and surfacing sell, trim, or hold recommendations.

## Docker

The repository includes a production-oriented `Dockerfile` and a `docker-compose.yml` for local development.

### Build the image

```bash
docker build -t stocksentinal .
```

### Run with Docker

The container reads configuration from environment variables. By default it serves the app on port `8000` and stores the SQLite database in `/data/stocksentinal.db`.

```bash
docker run --rm \
  -p 8000:8000 \
  -v stocksentinal-data:/data \
  -e PORT=8000 \
  -e LOG_LEVEL=INFO \
  -e MARKET_DATA_PROVIDER=alphavantage \
  -e ALPHA_VANTAGE_API_KEY=your-key \
  stocksentinal
```

To use a different database, override `DATABASE_URL`:

```bash
docker run --rm \
  -p 8000:8000 \
  -e DATABASE_URL=postgresql://user:password@host:5432/stocksentinal \
  stocksentinal
```

### Run with Docker Compose

Docker Compose is configured for local development with live reload and a named volume that persists the SQLite database.

```bash
docker compose up --build
```

Compose reads values from your shell environment or a local `.env` file automatically. Supported environment variables include:

- `PORT` — Uvicorn listen port (defaults to `8000`)
- `LOG_LEVEL` — Python logging level (defaults to `INFO`)
- `MARKET_DATA_PROVIDER` — `alphavantage` (default) or `twelvedata`
- `ALPHA_VANTAGE_API_KEY` — optional API key when `MARKET_DATA_PROVIDER=alphavantage`
- `TWELVE_DATA_API_KEY` — optional API key when `MARKET_DATA_PROVIDER=twelvedata`
- `DATABASE_URL` — database connection string (defaults to `sqlite:////data/stocksentinal.db`)
- `SUPABASE_URL` — optional Supabase project URL for hosted OAuth sign-in
- `SESSION_SECRET_KEY` — required when Supabase auth is enabled; signs app session cookies
- `SUPABASE_JWT_SECRET` — optional only for legacy Supabase HS256 projects; modern projects use JWKS automatically

To stop the development stack:

```bash
docker compose down
```
