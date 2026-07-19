# 🎵 ShadeMusicBot

> A modern, scalable Telegram Voice Chat Music Bot built with Python 3.12, Pyrogram, and MongoDB Atlas.

[![Python](https://img.shields.io/badge/Python-3.12-blue.svg)](https://python.org)
[![Pyrogram](https://img.shields.io/badge/Pyrogram-2.0-blue.svg)](https://pyrogram.org)
[![MongoDB](https://img.shields.io/badge/MongoDB-Atlas-green.svg)](https://mongodb.com/atlas)
[![Deploy on Render](https://img.shields.io/badge/Deploy-Render-46E3B7.svg)](https://render.com)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

---

## 📋 Table of Contents

- [Overview](#overview)
- [Architecture](#architecture)
- [Prerequisites](#prerequisites)
- [Quick Start](#quick-start)
- [Environment Variables](#environment-variables)
- [Deployment](#deployment)
- [Project Structure](#project-structure)
- [Development Phases](#development-phases)
- [Commands](#commands)

---

## Overview

ShadeMusicBot is a production-grade Telegram bot designed to stream music in Telegram Voice Chats. It is being built phase-by-phase, with a clean modular architecture from day one.

**Current Phase: 0 — Foundation**

Phase 0 establishes the complete production foundation:
- ✅ Async Pyrogram bot client
- ✅ MongoDB Atlas integration with typed repositories
- ✅ FastAPI health endpoint (Render health checks)
- ✅ Structured logging with log rotation
- ✅ Pydantic settings with startup validation
- ✅ Docker multi-stage build
- ✅ Render deployment configuration

---

## Architecture

```
ShadeMusicBot/
├── main.py                        # Entry point
├── app/
│   ├── core/
│   │   ├── config.py              # Pydantic settings (env vars)
│   │   ├── logger.py              # Loguru console + file logging
│   │   └── startup.py             # ApplicationLifecycle orchestration
│   ├── database/
│   │   ├── connection.py          # Motor (async MongoDB) manager
│   │   └── repositories/
│   │       ├── base.py            # Generic CRUD base repository
│   │       ├── users.py           # User domain repository
│   │       └── chats.py           # Chat domain repository
│   ├── bot/
│   │   ├── client.py              # Pyrogram client wrapper
│   │   └── handlers/
│   │       └── base.py            # /start /help /ping /info
│   ├── api/
│   │   └── health.py              # GET /health  GET /metrics
│   └── utils/
│       └── helpers.py             # Shared utilities
├── Dockerfile                     # Multi-stage, non-root, optimised
├── docker-compose.yml             # Local development
├── render.yaml                    # Render deployment blueprint
├── requirements.txt
└── .env.example                   # Full variable reference
```

### Key Design Decisions

| Decision | Rationale |
|----------|-----------|
| **Pyrogram** over python-telegram-bot | pytgcalls (voice streaming) integrates natively with Pyrogram — one library from the start |
| **Motor** for MongoDB | Fully async, official async driver for MongoDB |
| **FastAPI** for health | Minimal overhead; Render requires a responding HTTP port |
| **Loguru** for logging | Structured logs, rotation, compression — zero config |
| **Pydantic Settings** | Validation at startup, not at first use — fail fast |
| **Repository Pattern** | Decouples business logic from DB queries; easy to test and extend |
| **asyncio.gather** | Bot + web server in one event loop — no threads needed |

---

## Prerequisites

| Tool | Version | Purpose |
|------|---------|---------|
| Python | 3.12+ | Runtime |
| Docker | 24+ | Containerisation |
| MongoDB Atlas | Free tier | Database |
| Telegram API credentials | — | From my.telegram.org |

### Getting Telegram Credentials

1. **API ID & Hash** — visit [my.telegram.org/apps](https://my.telegram.org/apps), log in, and create an application.
2. **Bot Token** — open [@BotFather](https://t.me/BotFather) on Telegram, send `/newbot`, and follow the prompts.
3. **Owner ID** — send any message to [@userinfobot](https://t.me/userinfobot) to get your numeric user ID.

---

## Quick Start

### Local (Docker)

```bash
# 1. Clone the repository
git clone https://github.com/your-username/ShadeMusicBot.git
cd ShadeMusicBot

# 2. Copy and fill in the environment file
cp .env.example .env
# Edit .env with your credentials

# 3. Build and start
docker compose up --build

# 4. Verify health
curl http://localhost:8080/health
```

### Local (Python)

```bash
# 1. Create a virtual environment
python3.12 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Configure environment
cp .env.example .env
# Edit .env with your credentials

# 4. Run
python main.py
```

---

## Environment Variables

See [`.env.example`](.env.example) for full documentation. Required variables:

| Variable | Required | Description |
|----------|----------|-------------|
| `API_ID` | ✅ | Telegram API ID from my.telegram.org |
| `API_HASH` | ✅ | Telegram API Hash |
| `BOT_TOKEN` | ✅ | Bot token from @BotFather |
| `OWNER_ID` | ✅ | Your Telegram numeric user ID |
| `MONGO_URI` | ✅ | MongoDB Atlas connection string |
| `MONGO_DB_NAME` | ❌ | Database name (default: `shademusicbot`) |
| `LOG_LEVEL` | ❌ | `INFO` / `DEBUG` / `WARNING` (default: `INFO`) |
| `APP_PORT` | ❌ | Health server port (Render sets `PORT` automatically) |

---

## Deployment

### Render (Recommended)

1. Push this repository to GitHub.
2. On [Render Dashboard](https://dashboard.render.com), click **New → Blueprint**.
3. Connect your GitHub repository — Render will detect `render.yaml` automatically.
4. In the **Environment** tab, add your secret values (`API_ID`, `API_HASH`, `BOT_TOKEN`, `OWNER_ID`, `MONGO_URI`).
5. Click **Deploy**. Render will build the Docker image and run health checks against `/health`.

### MongoDB Atlas Setup

1. Create a free cluster at [cloud.mongodb.com](https://cloud.mongodb.com).
2. Under **Network Access**, add `0.0.0.0/0` (allow all IPs) — required for Render's dynamic IPs.
3. Under **Database Access**, create a user with read/write permissions.
4. Copy the connection string and set it as `MONGO_URI`.

---

## Project Structure

The project is intentionally structured so that future phases add new files/folders without modifying existing ones.

```
Phase 0 (current):  core + database + basic handlers + health API
Phase 1:            voice_chat/ streaming/ pytgcalls integration
Phase 2:            queue/ playlist/ inline controls
Phase 3:            search/ lyrics/ recommendations
Phase 4:            admin/ stats/ rate limiting / broadcast
```

---

## Development Phases

| Phase | Name | Status |
|-------|------|--------|
| **0** | Foundation | ✅ Complete |
| 1 | Voice Chat Streaming | 🔜 Planned |
| 2 | Queue & Playlist System | 🔜 Planned |
| 3 | Search & Inline Controls | 🔜 Planned |
| 4 | Admin, Stats & Rate Limiting | 🔜 Planned |

---

## Commands

| Command | Description |
|---------|-------------|
| `/start` | Welcome message; registers the user |
| `/help` | List available commands |
| `/ping` | Check bot latency and uptime |
| `/info` | Show bot name, ID, and phase |

---

## License

MIT © ShadeMusicBot contributors
