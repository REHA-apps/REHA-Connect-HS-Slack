# REHA Connect: Professional Specification & Compliance Manifesto (2026)

## 1. Executive Summary
REHA Connect is an enterprise-grade middleware suite designed to unify HubSpot CRM intelligence with mission-critical messaging platforms (Slack, WhatsApp, MS Teams). By utilizing a "Single Source of Truth" architecture, it enables decentralized teams to manage complex CRM lifecycles without leaving their primary communication hubs.

The platform is distinguished by its **REHA Pulse AI Intelligence**, which provides deep record insights and behavioral diagnostics without the privacy risks or latency associated with third-party Large Language Models (LLMs).

---

## 2. Infrastructure & Technical Architecture
The application follows a **Hexagonal (Clean) Architecture** pattern, ensuring core business logic is decoupled from specific CRM or Messaging providers.

### 2.1 Global Infrastructure Topology (EEA Sovereignty)
*   **Compute (AWS Lambda):** High-concurrency Serverless execution in **Dublin, IE** (eu-west-1).
*   **Container Registry (Amazon ECR):** Secure, private Docker image storage for bit-perfect environment parity.
*   **Persistence (Supabase):** Hosted in **Dublin, IE** (eu-west-1).
*   **Edge Layer (Cloudflare):** Provides WAF, DDoS mitigation, and SSL/TLS 1.3 termination.
*   **Data Sovereignty:** 100% of data processing and storage remains within the **European Economic Area (EEA)**, satisfying GDPR compliance.

### 2.2 Performance Engineering & Latency Mitigation
To manage the high-volume CRM event stream, REHA Connect employs:
*   **Serverless Scaling:** AWS Lambda scales automatically to handle thousands of concurrent webhooks without pre-provisioning.
*   **Burst Memoization:** A 15-second TTL cache for CRM object fetches ensures that redundant webhooks (e.g., 50 updates to one Deal) result in only a single HubSpot API call.
*   **Verify-then-Acknowledge:** Webhooks are acknowledged within <200ms; logic is deferred to asynchronous background workers.

---

## 3. Technology Stack
| Category | Tooling | Strategic Justification |
| :--- | :--- | :--- |
| **Backend** | Python 3.12 | Asyncio and Advanced Type Hinting for high-performance concurrent I/O. |
| **Compute** | AWS Lambda (ARM64) | Serverless, high-density compute with 1ms billing granularity. |
| **Runtime** | Docker (ECR) | Immutable container images using ARM64 architecture for performance efficiency. |
| **ML Engine** | Transformers (SST-2) | Baked-in DistilBERT model for sub-second offline sentiment analysis. |
| **Database** | Supabase (PostgreSQL) | Strict Row-Level Security (RLS) for multi-tenant data isolation. |
| **Networking** | Cloudflare WAF | Enterprise-grade perimeter security, origin hiding, and DDoS protection. |
| **Billing** | Stripe Checkout | Global, tax-compliant SaaS billing integration. |
| **Package Mgr** | `uv` | Rapid, reproducible dependency resolution for deterministic builds. |

---

## 4. Functional Capabilities

### 4.1 REHA Pulse AI Intelligence (Zero-Transfer AI)
REHA Connect uses a proprietary, rule-based **REHA Pulse** engine combined with local Transformers.
*   **Polymorphic Scoring:** Evaluates 18+ weighted variables (e.g., Ticket velocity, Persona/Title match, Deal Value-to-Time ratio).
*   **Sovereign Processing:** 100% of sentiment analysis and narrative record recaps are performed locally within the Docker container—data is **never** sent to third-party AI providers (OpenAI/Anthropic).
*   **Offline Transformers:** The `distilbert-base-uncased-finetuned-sst-2-english` model is baked into the Lambda image, ensuring zero external network calls for AI processing.

### 4.2 Unified Helpdesk & CRM Sync
*   **Intelligent Threading & Continuity:** True 1:1 mapping between a HubSpot ticket and a Slack thread. Replies in Slack sync to HubSpot as either internal notes or direct outbound customer responses based on the channel context.
*   **Helpdesk Management:** Claim, Close, and Reopen tickets directly via Slack Block Kit.
*   **Advanced Deal Execution:** Standalone actions for Deal Stage, Close Date, Log Next Step, Pricing Calculator, and Owner Reassignment directly from the Slack deal card.
*   **Chat Transcripts:** One-click synchronization of entire Slack threads back to the HubSpot timeline as clean, formatted notes.
*   **Authenticated Unfurls:** Private CRM links only reveal rich previews to users with a validated, individual OAuth token.

---

## 5. Security & Data Privacy

### 5.1 Data Protection & Encryption
*   **Encryption at Rest:** All OAuth tokens and PII are encrypted using **AES-256-GCM** before persistence in Supabase.
*   **Encryption in Transit:** Enforced **Full (Strict) TLS 1.3** via Cloudflare-to-AWS proxying.
*   **Secret Management:** AWS Lambda Environment Variables and Cloudflare Origin Shield ensure zero exposure of API keys.
*   **CSRF Protection:** OAuth flows utilize high-entropy, salted state parameters to prevent session hijacking.

### 5.2 Auditability & Accountability
*   **Correlation ID (`corr_id`):** Every transaction is assigned a unique ID for end-to-end forensic debugging.
*   **90-Day Retention:** Audit logs (searches, clicks, installs) are maintained for 90 days to satisfy SOC 2 forensic requirements.
*   **Automated Pruning:** Background workers handle daily database maintenance to purge expired logs and maintain performance.

---

## 6. Commercial Model
*   **Free Tier:** 7-Day Full-Feature Pro Trial.
*   **Pro Tier ($49/month):** Unlimited notifications, full REHA Pulse access, and 90-day audit logging.
*   **Enterprise (Custom):** Multi-platform (WhatsApp/Teams) and extended log retention policies.

---

## 7. IT Manager & Reviewer Defense (FAQ)

**Q: In which geographic region does the data reside?**
*   **Response:** All Compute (AWS Lambda) and Persistence (Supabase) are located strictly within the **EEA (Ireland, eu-west-1)**. We can provide data residency documentation for Cloudflare's global edge nodes upon request.

**Q: Does any CRM data leave the EU?**
A: No. All processing and persistence are located within the EEA. We do not use US-based third-party AI for data processing; our Transformers model is baked into our local Docker image.

**Q: How do you handle the Slack 3-second timeout?**
A: We use a "Verify-then-Acknowledge" pattern. We respond `200 OK` to Slack instantly and use AWS Lambda background processing to update the message with REHA Pulse insights once calculated.

**Q: Why 90 days for audit logs?**
A: 90 days is the industry standard for forensic investigations and SOC 2 sampling. Our automated pruning cycles remove logs older than 90 days to protect user privacy and optimize system resources.

**Q: What happens if a user is offboarded?**
A: We support Immediate Revocation. If a user is removed from Slack or HubSpot, our system detects the revocation webhook and performs a hard delete of that specific user’s OAuth tokens and mapping data in Supabase.

---

> [!NOTE]
> **Technical Implementation Note:** REHA Connect leverages Stripe Checkout for tax-compliant global billing and uses Cloudflare WAF as the primary defense against SQLi and XSS. All code is managed via Docker (ECR) for reproducible, secure deployments.
