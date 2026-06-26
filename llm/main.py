import os
import json
import logging
from typing import Optional
from fastapi import FastAPI
from pydantic import BaseModel
from groq import Groq
from models import CaseType, EvidenceVerdict, Department, Severity

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="LLM Service")

_client = None

def get_client():
    global _client
    if _client is None:
        key = os.getenv("GROQ_API_KEY")
        if not key:
            return None
        _client = Groq(api_key=key)
    return _client


# ── Request/Response ──────────────────────────────────────────────────────────

class GenerateRequest(BaseModel):
    complaint: str
    case_type: CaseType
    evidence_verdict: EvidenceVerdict
    department: Department
    severity: Severity
    matched_txn_id: Optional[str] = None
    matched_txn_amount: Optional[float] = None
    matched_txn_status: Optional[str] = None
    matched_txn_counterparty: Optional[str] = None
    language: str = "en"
    user_type: str = "customer"
    human_review_required: bool = False
    ticket_id: str = "this ticket"

class GenerateResponse(BaseModel):
    agent_summary: str
    recommended_next_action: str
    customer_reply: str
    used_llm: bool


# ── LLM generation ────────────────────────────────────────────────────────────

def build_prompt(req: GenerateRequest) -> str:
    txn_info = "No specific transaction matched."
    if req.matched_txn_id:
        txn_info = (
            f"Transaction ID: {req.matched_txn_id}, "
            f"Amount: {req.matched_txn_amount} BDT, "
            f"Status: {req.matched_txn_status}, "
            f"Counterparty: {req.matched_txn_counterparty}"
        )

    lang_note = {
        "bn": "IMPORTANT: The customer wrote in Bangla. Write the customer_reply in Bangla only.",
        "mixed": "The customer used mixed Bangla-English. Reply in clear English.",
    }.get(req.language, "Reply in English.")

    return f"""You are an internal AI copilot for a digital finance support team.
Write exactly three text fields for this support case. All decisions are already made — you only write the text.

CASE:
- Complaint: {req.complaint}
- Case Type: {req.case_type.value}
- Evidence Verdict: {req.evidence_verdict.value}
- Matched Transaction: {txn_info}
- Department: {req.department.value}
- Severity: {req.severity.value}
- Human Review Required: {req.human_review_required}
- User Type: {req.user_type}

{lang_note}

Respond ONLY in valid JSON, no markdown:
{{
  "agent_summary": "...",
  "recommended_next_action": "...",
  "customer_reply": "..."
}}

RULES:
1. agent_summary: 1-2 sentences, factual, include transaction ID if available.
2. recommended_next_action: specific operational next step for the agent.
3. customer_reply:
   - NEVER ask for PIN, OTP, password, or card number.
   - NEVER promise refund/reversal/unblock. Use: "any eligible amount will be returned through official channels".
   - NEVER direct to suspicious third parties.
   - ALWAYS include: "Please do not share your PIN or OTP with anyone." (or Bangla equivalent).
   - For phishing: reassure that we never ask for credentials.
   - Keep it 2-4 sentences, warm but professional.
"""


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/generate", response_model=GenerateResponse)
def generate(req: GenerateRequest):
    client = get_client()
    if client is None:
        logger.warning("GROQ_API_KEY not set — LLM disabled")
        return GenerateResponse(
            agent_summary="", recommended_next_action="", customer_reply="", used_llm=False
        )

    try:
        resp = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": build_prompt(req)}],
            max_tokens=600,
            temperature=0.3,
        )
        raw = resp.choices[0].message.content.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        parsed = json.loads(raw.strip())

        return GenerateResponse(
            agent_summary=parsed.get("agent_summary", "").strip(),
            recommended_next_action=parsed.get("recommended_next_action", "").strip(),
            customer_reply=parsed.get("customer_reply", "").strip(),
            used_llm=True,
        )
    except Exception as e:
        logger.error(f"LLM generation failed: {e}")
        return GenerateResponse(
            agent_summary="", recommended_next_action="", customer_reply="", used_llm=False
        )
