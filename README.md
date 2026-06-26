# QueueStorm Investigator — Microservice Edition

**bKash presents SUST CSE Carnival 2026 — Codex Community Hackathon**
AI/API SupportOps Challenge · Online Preliminary Round

---

## What It Does

QueueStorm Investigator is an internal AI copilot for digital finance support agents. It receives one customer complaint at a time — along with the customer's recent transaction history — and returns a structured JSON response that:

- **Matches** the complaint to the specific transaction it refers to
- **Verdicts** whether the data supports, contradicts, or cannot determine the complaint
- **Classifies** the case type and routes it to the right department
- **Drafts** a safe, professional reply for the customer and a concise summary for the agent

The service never requests credentials, never promises refunds it cannot authorize, and always escalates ambiguous or high-risk cases for human review.

---

## Architecture

```
Internet / Judge Harness
        │
        ▼ :80
┌─────────────────────┐
│       Nginx          │  least_conn load balancing
│   Load Balancer      │
└──┬──────┬──────┬────┘
   ▼      ▼      ▼
 GW-1   GW-2   GW-3     ← 3 Gateway instances (FastAPI, :8000)
   └──────┴──────┘
          │ async HTTP (httpx)
    ┌─────┴──────────────────────┐
    ▼          ▼         ▼       ▼
Investigator Classifier Safety  LLM
  :8001       :8002     :8003   :8004
```

### Services

| Service | Port | Responsibility |
|---|---|---|
| Nginx | 80 | Load balancer — least_conn across 3 gateway instances |
| Gateway (×3) | 8000 | Orchestrates the full pipeline via async HTTP |
| Investigator | 8001 | Transaction matching — rule-based regex scoring |
| Classifier | 8002 | case_type, evidence_verdict, department, severity, human_review |
| Safety | 8003 | Injection detection, reply sanitization, fallback templates |
| LLM | 8004 | Groq text generation for 3 human-readable fields only |

### Design Principle

All decisions (transaction match, verdict, classification, routing, severity, escalation) are made by **deterministic rule-based logic**. The LLM only writes three text fields: `agent_summary`, `recommended_next_action`, and `customer_reply`. If Groq fails, pre-built safe fallback templates are used automatically — the service never crashes.

---

## API Reference

### `GET /health`

```json
{"status": "ok"}
```

### `POST /analyze-ticket`

**Request:**
```json
{
  "ticket_id": "TKT-001",
  "complaint": "I sent 5000 taka to a wrong number around 2pm today.",
  "language": "en",
  "channel": "in_app_chat",
  "user_type": "customer",
  "campaign_context": "boishakh_bonanza_day_1",
  "transaction_history": [
    {
      "transaction_id": "TXN-9101",
      "timestamp": "2026-04-14T14:08:22Z",
      "type": "transfer",
      "amount": 5000,
      "counterparty": "+8801719876543",
      "status": "completed"
    }
  ]
}
```

**Response:**
```json
{
  "ticket_id": "TKT-001",
  "relevant_transaction_id": "TXN-9101",
  "evidence_verdict": "consistent",
  "case_type": "wrong_transfer",
  "severity": "high",
  "department": "dispute_resolution",
  "agent_summary": "Customer reports sending 5000 BDT via TXN-9101 to the wrong recipient.",
  "recommended_next_action": "Initiate wrong-transfer dispute workflow for TXN-9101. Verify recipient details with customer.",
  "customer_reply": "We have noted your concern about TXN-9101. Our dispute team will review and contact you through official channels. Please do not share your PIN or OTP with anyone.",
  "human_review_required": true,
  "confidence": 0.95,
  "reason_codes": ["wrong_transfer", "transaction_match", "evidence_supported"]
}
```

**HTTP Codes:**

| Code | Meaning |
|---|---|
| 200 | Successful analysis |
| 400 | Malformed JSON or missing required fields |
| 422 | Valid schema but semantically invalid (empty complaint) |
| 500 | Internal error — no stack traces or secrets exposed |

---

## Local Setup

**Prerequisites:** Docker Desktop

```bash
git clone https://github.com/Oishee53/Sust_preli_alnitak.git
cd Sust_preli_alnitak

# create .env with your Groq key
cp .env.example .env
# edit .env and add: GROQ_API_KEY=your_actual_key

# build and run all services
docker compose up --build

# verify
curl http://localhost:80/health
```

The service works without a `GROQ_API_KEY` — fallback templates are used. With the key, Groq generates the text fields.

---

## Deployment on Poridhi Lab VM

```bash
# 1. Clone repo on the VM terminal
git clone https://github.com/Oishee53/Sust_preli_alnitak.git
cd Sust_preli_alnitak

# 2. Set your Groq key
echo "GROQ_API_KEY=your_actual_key" > .env

# 3. Deploy
docker compose up -d --build

# 4. Get your wt0 IP
ifconfig
# look for wt0 interface → inet value (e.g. 100.84.70.96)

# 5. Expose port 80 via Poridhi Load Balancer UI
# Enter wt0 IP + port 80 → click Expose
# You get a public URL like: xxxx.lb.poridhi.io

# 6. Verify
curl https://xxxx.lb.poridhi.io/health
```

---

## MODELS

| Model | Where it runs | Why chosen |
|---|---|---|
| `llama-3.3-70b-versatile` via Groq API | Groq cloud (LLM service) | Free tier, sub-2s response, strong multilingual including Bangla/Banglish, no GPU needed, keeps images tiny |

The model is used **only** for generating three human-readable text fields. All decisions are made by deterministic rule-based logic that does not depend on the LLM.

---

## AI Approach

**Hybrid rule + AI system:**

**Transaction Matching (Investigator Service)**
Regex extracts amount mentions (English and Bangla digits), phone numbers, time hints, and transaction type signals from the complaint. Each transaction in history is scored. The highest-scoring transaction above a threshold is selected. Ambiguous matches return `null` and `insufficient_data`.

**Evidence Verdict (Classifier Service)**
Deterministic rules compare complaint claims against transaction status. A claim of "failed" against a `completed` transaction returns `inconsistent`. A `pending` cash-in with a non-receipt complaint returns `consistent`.

**Case Classification (Classifier Service)**
Keyword and signal matching with priority ordering — phishing checked first, then merchant/agent-specific, then financial disputes.

**Text Generation (LLM Service)**
Groq receives pre-decided facts and writes only the three text fields. Temperature set to 0.3 for consistency and safety.

---

## Safety Logic

Three hard safety rules enforced by two layers:

**Layer 1 — LLM Prompt**
The prompt explicitly forbids asking for PIN/OTP/password and promising refunds or reversals.

**Layer 2 — Post-Generation Filter (Safety Service)**
Regex patterns scan every `customer_reply` and `recommended_next_action` after LLM generation. Violations are stripped and replaced with safe language.

**Prompt Injection Protection**
The Safety Service scans complaint text for embedded instructions ("ignore previous instructions", "act as", "override"). Detected injections are logged and ignored — the complaint is still processed normally.

**Penalty Avoidance**

| Violation | Penalty | Our Handling |
|---|---|---|
| Asks for PIN/OTP/password | -15 pts | Blocked by both layers |
| Promises refund/reversal | -10 pts | Replaced with safe language |
| Suspicious third party redirect | -10 pts | Never generated |
| 2+ critical violations | Disqualified | Double layer prevents this |

---

## Folder Structure

```
queuestorm-microservices/
├── docker-compose.yml
├── nginx/
│   └── nginx.conf           ← least_conn load balancing
├── gateway/
│   ├── Dockerfile
│   ├── main.py              ← pipeline orchestrator
│   ├── models.py
│   └── requirements.txt
├── investigator/
│   ├── Dockerfile
│   ├── main.py              ← transaction matching
│   ├── models.py
│   └── requirements.txt
├── classifier/
│   ├── Dockerfile
│   ├── main.py              ← classification + routing
│   ├── models.py
│   └── requirements.txt
├── safety/
│   ├── Dockerfile
│   ├── main.py              ← guardrails + fallbacks
│   ├── models.py
│   └── requirements.txt
├── llm/
│   ├── Dockerfile
│   ├── main.py              ← Groq text generation
│   ├── models.py
│   └── requirements.txt
├── shared/
│   └── models.py            ← shared Pydantic schemas
├── .env.example
└── .gitignore
```

---

## Known Limitations

- **Render free tier** does not support Docker Compose with multiple containers. Deploy on Poridhi Lab VM, Oracle Cloud free tier, or Railway instead.
- **Cold starts** on free hosting — if all containers sleep, first request may be slow. Keep-alive pings recommended.
- **Bangla amount detection** handles Bangla digits (০-৯) with and without টাকা suffix. Mixed-script edge cases may occasionally miss amounts — the service falls back to `insufficient_data` safely.
- **No persistent state** — each request is fully independent. Multi-turn conversation context is not tracked.
- **LLM dependency for text quality** — without `GROQ_API_KEY`, fallback templates are used. They are safe and correct but less personalized.
- **Nginx load balancing** is active only in Docker Compose deployment. On Render separate services, the gateway runs as a single instance with no Nginx layer.

---

## Sample Output

Input from `SUST_Preli_Sample_Cases.json` case SAMPLE-01:

```json
{
  "ticket_id": "TKT-001",
  "relevant_transaction_id": "TXN-9101",
  "evidence_verdict": "consistent",
  "case_type": "wrong_transfer",
  "severity": "high",
  "department": "dispute_resolution",
  "agent_summary": "Customer reports sending money to the wrong recipient via TXN-9101. Dispute review required.",
  "recommended_next_action": "Initiate wrong-transfer dispute workflow for transaction TXN-9101. Verify recipient details.",
  "customer_reply": "We have noted your concern regarding ticket TKT-001. Our dispute resolution team will review the transaction details and contact you through official channels. Please do not share your PIN or OTP with anyone.",
  "human_review_required": true,
  "confidence": 0.95,
  "reason_codes": ["wrong_transfer", "transaction_match", "completed_transaction", "evidence_supported"]
}

## Live Deployment

**Deployed on:** Poridhi Lab VM

**Public Endpoint:** `https://6a36acda950c78444441f603_7a9bd37d.lb.poridhi.io`

**Health Check:** `https://6a36acda950c78444441f603_7a9bd37d.lb.poridhi.io/health`

**Analyze Ticket:** `https://6a36acda950c78444441f603_7a9bd37d.lb.poridhi.io/analyze-ticket`

**GitHub Repository:** `https://github.com/Oishee53/Sust_preli_alnitak`
```

---

*QueueStorm Investigator · bKash presents SUST CSE Carnival 2026 · Codex Community Hackathon*


