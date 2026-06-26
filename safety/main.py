import re
import logging
from typing import Optional
from fastapi import FastAPI
from pydantic import BaseModel
from models import CaseType

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Safety Service")


# ── Request/Response ──────────────────────────────────────────────────────────

class SafetyCheckRequest(BaseModel):
    customer_reply: str
    recommended_next_action: str
    is_bangla: bool = False

class SafetyCheckResponse(BaseModel):
    customer_reply: str
    recommended_next_action: str
    violations_found: bool

class FallbackRequest(BaseModel):
    case_type: CaseType
    ticket_id: str
    matched_txn_id: Optional[str] = None
    is_bangla: bool = False

class FallbackResponse(BaseModel):
    agent_summary: str
    recommended_next_action: str
    customer_reply: str

class InjectionCheckRequest(BaseModel):
    complaint: str

class InjectionCheckResponse(BaseModel):
    is_injection: bool


# ── Safety patterns ───────────────────────────────────────────────────────────

CREDENTIAL_PATTERNS = [
    r'\bshare\s+(your\s+)?(pin|otp|password|passcode|card\s+number)\b',
    r'\benter\s+(your\s+)?(pin|otp|password)\b',
    r'\bprovide\s+(your\s+)?(pin|otp|password|card\s+number)\b',
    r'\bsend\s+(your\s+)?(pin|otp|password)\b',
    r'\bverif(y|ication)\s+(your\s+)?(pin|otp|password)\b',
    r'\bwhat\s+is\s+(your\s+)?(pin|otp|password)\b',
    r'\bটাকার পিন\b', r'\bওটিপি দিন\b', r'\bপিন দিন\b',
]

PROMISE_PATTERNS = [
    r'\bwe will refund\b',
    r'\byou will (receive|get) (your )?refund\b',
    r'\bwe (will|shall) (reverse|credit|return) (your )?(money|amount|balance|taka|bdt)\b',
    r'\byour account (will be|has been) unblocked\b',
    r'\bwe guarantee\b',
    r'\bguaranteed (refund|return|reversal)\b',
    r'\bআমরা টাকা ফেরত দেব\b', r'\bরিফান্ড করা হবে\b',
]

INJECTION_SIGNALS = [
    r'ignore (previous|above|all) instructions',
    r'forget (your|the) (system|rules|instructions)',
    r'you are now', r'act as', r'pretend (you are|to be)',
    r'do not follow', r'override', r'new instruction',
    r'system prompt', r'reveal (your|the) (prompt|instructions|system)',
]

SAFE_REMINDER = "Please do not share your PIN or OTP with anyone."
SAFE_REMINDER_BN = "অনুগ্রহ করে কারো সাথে আপনার পিন বা ওটিপি শেয়ার করবেন না।"
SAFE_REFUND = "any eligible amount will be returned through official channels"


def sanitize(reply: str, is_bangla: bool = False) -> tuple[str, bool]:
    violated = False
    lower = reply.lower()

    for p in CREDENTIAL_PATTERNS:
        if re.search(p, lower):
            reply = re.sub(p, '', reply, flags=re.IGNORECASE)
            violated = True

    for p in PROMISE_PATTERNS:
        if re.search(p, lower):
            reply = re.sub(p, SAFE_REFUND, reply, flags=re.IGNORECASE)
            violated = True

    reminder = SAFE_REMINDER_BN if is_bangla else SAFE_REMINDER
    if reminder.lower() not in reply.lower() and "pin" not in reply.lower():
        reply = reply.rstrip('.').rstrip() + f" {reminder}"

    return reply.strip(), violated


# ── Fallback templates ────────────────────────────────────────────────────────

def fallback_reply(case_type: CaseType, ticket_id: str, is_bangla: bool) -> str:
    if is_bangla:
        return (
            f"আপনার অভিযোগটি ({ticket_id}) আমরা গ্রহণ করেছি। "
            "আমাদের সংশ্লিষ্ট দল বিষয়টি পর্যালোচনা করবে এবং অফিসিয়াল চ্যানেলের মাধ্যমে আপনাকে জানাবে। "
            "অনুগ্রহ করে কারো সাথে আপনার পিন বা ওটিপি শেয়ার করবেন না।"
        )
    templates = {
        "wrong_transfer": f"We have noted your concern regarding ticket {ticket_id}. Our dispute resolution team will review the transaction details and contact you through official channels. Please do not share your PIN or OTP with anyone.",
        "payment_failed": f"We have received your report about a failed transaction ({ticket_id}). Our payments team will investigate and any eligible amount will be returned through official channels. Please do not share your PIN or OTP with anyone.",
        "refund_request": f"We have received your refund request ({ticket_id}). Refund eligibility depends on the applicable policy. Our team will review and respond through official channels. Please do not share your PIN or OTP with anyone.",
        "duplicate_payment": f"We have noted the possible duplicate payment ({ticket_id}). Our payments team will verify and any eligible amount will be returned through official channels. Please do not share your PIN or OTP with anyone.",
        "merchant_settlement_delay": f"We have received your settlement inquiry ({ticket_id}). Our merchant operations team will check the batch status and update you. Please do not share your PIN or OTP with anyone.",
        "agent_cash_in_issue": f"We have noted your cash-in concern ({ticket_id}). Our agent operations team will verify and resolve within standard timelines. Please do not share your PIN or OTP with anyone.",
        "phishing_or_social_engineering": "Thank you for reaching out before sharing any information. We never ask for your PIN, OTP, or password under any circumstances. Please do not share these with anyone. Our fraud team has been notified.",
        "other": f"We have received your concern ({ticket_id}). Our team will review and reach out through official channels. Please do not share your PIN or OTP with anyone.",
    }
    return templates.get(case_type.value, templates["other"])


def fallback_summary(case_type: CaseType, txn_id: Optional[str]) -> str:
    ref = f" via {txn_id}" if txn_id else ""
    summaries = {
        "wrong_transfer": f"Customer reports sending money to the wrong recipient{ref}. Dispute review required.",
        "payment_failed": f"Customer reports a failed payment{ref} with possible balance deduction.",
        "refund_request": f"Customer has submitted a refund request{ref}.",
        "duplicate_payment": f"Customer reports a duplicate payment{ref}. Two identical transactions detected.",
        "merchant_settlement_delay": f"Merchant reports delayed settlement{ref} beyond expected window.",
        "agent_cash_in_issue": f"Customer reports cash-in{ref} not reflected in account balance.",
        "phishing_or_social_engineering": "Customer reports suspicious contact attempting to obtain credentials.",
        "other": "Customer has raised a general concern. Further investigation needed.",
    }
    return summaries.get(case_type.value, "Customer has raised a concern.")


def fallback_action(case_type: CaseType, txn_id: Optional[str]) -> str:
    ref = f" {txn_id}" if txn_id else ""
    actions = {
        "wrong_transfer": f"Initiate wrong-transfer dispute workflow for transaction{ref}. Verify recipient details.",
        "payment_failed": f"Investigate ledger status for transaction{ref}. Check if balance was deducted.",
        "refund_request": "Inform customer that refund eligibility depends on policy. Guide through official process.",
        "duplicate_payment": f"Verify duplicate status of transaction{ref} with payments ops.",
        "merchant_settlement_delay": f"Route to merchant operations to check settlement batch status{ref}.",
        "agent_cash_in_issue": f"Investigate cash-in transaction{ref} with agent operations.",
        "phishing_or_social_engineering": "Escalate to fraud risk team immediately. Log reported contact.",
        "other": "Route to customer support for further clarification.",
    }
    return actions.get(case_type.value, "Route to customer support for review.")


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/check-injection", response_model=InjectionCheckResponse)
def check_injection(req: InjectionCheckRequest):
    lower = req.complaint.lower()
    for sig in INJECTION_SIGNALS:
        if re.search(sig, lower):
            return InjectionCheckResponse(is_injection=True)
    return InjectionCheckResponse(is_injection=False)


@app.post("/sanitize", response_model=SafetyCheckResponse)
def sanitize_output(req: SafetyCheckRequest):
    clean_reply, reply_violated = sanitize(req.customer_reply, req.is_bangla)
    clean_action = req.recommended_next_action
    action_lower = clean_action.lower()
    action_violated = False
    for p in PROMISE_PATTERNS:
        if re.search(p, action_lower):
            clean_action = re.sub(p, 'review and process per official policy', clean_action, flags=re.IGNORECASE)
            action_violated = True

    return SafetyCheckResponse(
        customer_reply=clean_reply,
        recommended_next_action=clean_action,
        violations_found=reply_violated or action_violated,
    )


@app.post("/fallback", response_model=FallbackResponse)
def get_fallback(req: FallbackRequest):
    return FallbackResponse(
        agent_summary=fallback_summary(req.case_type, req.matched_txn_id),
        recommended_next_action=fallback_action(req.case_type, req.matched_txn_id),
        customer_reply=fallback_reply(req.case_type, req.ticket_id, req.is_bangla),
    )
