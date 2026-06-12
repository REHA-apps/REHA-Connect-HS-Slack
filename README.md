# REHA Connect 🚀
### Modern HubSpot → Slack / WhatsApp / MS Teams Integration Framework
Built with **FastAPI** · **Supabase** · **uv** · **Docker** · **Nginx**

---

## 🌟 Overview

**REHA Connect** is the powerhouse behind REHA Apps. It provides a modular, high-performance integration framework designed to bridge **HubSpot CRM** with modern communication platforms.

Whether your team uses **Slack**, **WhatsApp**, or **Microsoft Teams**, this backend handles the smart routing, record insights, and real-time syncing that keeps your business moving.

### Why REHA Connect?
- **⚡ Lightning Fast**: Built with `async` Python for sub-second response times.
- **🧩 Extensible**: Plug-and-play architecture for adding new channels in hours, not weeks.
- **🛡️ Secure**: OAuth 2.0 flow with encrypted token persistence via Supabase.
- **📊 Record-Ready**: Integrated hooks for deep analysis of CRM data and messages.

---

## 📦 Project Structure

```text
crm-connectors/
├── app/
│   ├── api/            # Route handlers (Webhook listeners)
│   ├── clients/        # 3rd-party API clients (HubSpot, Slack, etc.)
│   ├── connectors/     # Business logic for specific integrations
│   ├── services/       # Shared backend services (AI, Auth)
│   ├── db/             # Database & Storage (Supabase)
│   └── main.py         # App entry point
├── slack_manifest.yml  # Slack App Configuration
├── Dockerfile          # Containerization
└── pyproject.toml      # Dependency management (uv)
```

---

## 🏛️ Architectural Decisions

### Identity Bridging (Multi-Portal Support)
To support teams with multiple HubSpot portals linked to a single Slack workspace, we use a **Triple-Key Trace** mechanism:
- **`hs_` Prefix**: All internal workspace IDs originating from HubSpot are prefixed with `hs_`. This prevents ID collisions with legacy Slack-native IDs and allows the `IntegrationService` to distinguish between "Identity Parent" (Slack) and "Data Source" (HubSpot) records.
- **Identity Resolving**: Webhooks arriving from HubSpot use the `portalId` to pivot to the primary Slack connection, ensuring notifications are routed to the correct Slack team even in complex multi-tenant scenarios.

---

## 🧩 Supported Integrations

### 🟠 HubSpot CRM
- Bi-directional contact & deal syncing.
- Task management and record search.
- Robust webhook handling for real-time updates.

### 💬 Slack (Live)
- Slash commands (`/hs-find`, `/hs-create`).
- Rich interactive Block Kit UI.
- Real-time event notifications for deal stages.
- **Advanced Deal Execution**: Log Next Steps, Change Close Date, Reassign Owners, and use the Pricing Calculator directly from Slack.
- **Help Desk Continuity**: True 1:1 threaded synchronization for HubSpot tickets, supporting both internal notes and outbound customer replies.
- **Scheduled Reports**: Automated delivery of pipeline summaries and team metrics directly into your Slack channels.

### 🟢 WhatsApp (Coming Soon)
- Direct messaging from HubSpot timelines.
- Automated message logging and template support.

### 🟣 MS Teams (Coming Soon)
- Channel notifications for new leads and won deals.
- Collaborative ticket management within Teams.

---

## 🛠️ Local Development

### 1. Install Dependencies
We use [uv](https://github.com/astral-sh/uv) for extremely fast package management.
```bash
uv sync
```

### 2. Environment Setup
Copy `.env.example` to `.env` and fill in your credentials.
```bash
cp .env.example .env
```

### 3. Run the Server
```bash
just dev
# OR
uv run fastapi dev app/main.py
```

---

## 🐳 Docker Production

Run the entire stack with Nginx and SSL support:
```bash
docker-compose up --build -d
```

---

## 🚢 Deployment

Optimized for deployment on **Render**, **Railway**, or any **Docker-compatible** cloud provider.

---

© 2026 [REHA Apps](https://github.com/REHA-apps/REHA-Home). Built for teams that move fast.
