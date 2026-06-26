import os
import logging
import traceback
import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import ValidationError
from models import TicketRequest, TicketResponse

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="QueueStorm Investigator — Gateway")

app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# Service URLs from environment (default to docker-compose service names)
INVESTIGATOR_URL = os.getenv("INVESTIGATOR_URL", "http://investigator:8001")
CLASSIFIER_URL   = os.getenv("CLASSIFIER_URL",   "http://classifier:8002")
SAFETY_URL       = os.getenv("SAFETY_URL",       "http://safety:8003")
LLM_URL          = os.getenv("LLM_URL",          "http://llm:8004")

TIMEOUT = 25.0  # leave buffer under 30s judge timeout


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/analyze-ticket", response_model=TicketResponse)
async def analyze_ticket(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "Invalid JSON body"})

    try:
        ticket = TicketRequest(**body)
    except ValidationError as e:
        return JSONResponse(status_code=400, content={"error": "Validation failed", "details": e.errors()})

    if not ticket.complaint or not ticket.complaint.strip():
        return JSONResponse(status_code=422, content={"error": "Complaint text cannot be empty"})

    try:
        return await _pipeline(ticket)
    except Exception as e:
        logger.error(f"Pipeline error: {traceback.format_exc()}")
        return JSONResponse(status_code=500, content={"error": "Internal processing error"})


async def _pipeline(ticket: TicketRequest) -> TicketResponse:
    language  = ticket.language.value if ticket.language else "en"
    user_type = ticket.user_type.value if ticket.user_type else "unknown"
    history   = ticket.transaction_history or []
    is_bangla = (language == "bn")

    logger.info(f"Processing {ticket.ticket_id} | lang={language} | txns={len(history)}")

    async with httpx.AsyncClient(timeout=TIMEOUT) as client:

        # ── Step 1: Injection check ───────────────────────────────────────────
        inj_resp = await client.post(f"{SAFETY_URL}/check-injection", json={"complaint": ticket.complaint})
        injection_detected = inj_resp.json().get("is_injection", False)
        if injection_detected:
            logger.warning(f"Prompt injection detected in {ticket.ticket_id}")

        # ── Step 2: Transaction matching ──────────────────────────────────────
        inv_payload = {
            "complaint": ticket.complaint,
            "transaction_history": [t.model_dump() for t in history],
        }
        inv_resp = await client.post(f"{INVESTIGATOR_URL}/investigate", json=inv_payload)
        inv_data = inv_resp.json()

        matched_txn_id  = inv_data.get("relevant_transaction_id")
        match_confidence = inv_data.get("match_confidence", 0.0)
        is_duplicate    = inv_data.get("is_duplicate", False)

        matched_txn = next((t for t in history if t.transaction_id == matched_txn_id), None) if matched_txn_id else None

        # ── Step 3: Classification ────────────────────────────────────────────
        cls_payload = {
            "complaint": ticket.complaint,
            "transaction_history": [t.model_dump() for t in history],
            "matched_txn_id": matched_txn_id,
            "user_type": user_type,
            "is_duplicate": is_duplicate,
        }
        cls_resp = await client.post(f"{CLASSIFIER_URL}/classify", json=cls_payload)
        cls_data = cls_resp.json()

        case_type        = cls_data["case_type"]
        department       = cls_data["department"]
        severity         = cls_data["severity"]
        evidence_verdict = cls_data["evidence_verdict"]
        human_review     = cls_data["human_review_required"]
        confidence       = cls_data["confidence"]
        reason_codes     = cls_data["reason_codes"]

        if injection_detected:
            reason_codes.append("prompt_injection_attempt_ignored")

        # ── Step 4: LLM text generation ───────────────────────────────────────
        llm_payload = {
            "complaint": ticket.complaint,
            "case_type": case_type,
            "evidence_verdict": evidence_verdict,
            "department": department,
            "severity": severity,
            "matched_txn_id": matched_txn_id,
            "matched_txn_amount": matched_txn.amount if matched_txn else None,
            "matched_txn_status": matched_txn.status.value if matched_txn else None,
            "matched_txn_counterparty": matched_txn.counterparty if matched_txn else None,
            "language": language,
            "user_type": user_type,
            "human_review_required": human_review,
            "ticket_id": ticket.ticket_id,
        }
        llm_resp = await client.post(f"{LLM_URL}/generate", json=llm_payload)
        llm_data = llm_resp.json()

        agent_summary = llm_data.get("agent_summary", "")
        next_action   = llm_data.get("recommended_next_action", "")
        customer_reply = llm_data.get("customer_reply", "")
        used_llm      = llm_data.get("used_llm", False)

        # ── Step 5: Fallback if LLM failed or returned empty ─────────────────
        if not used_llm or not agent_summary or not customer_reply:
            fb_resp = await client.post(f"{SAFETY_URL}/fallback", json={
                "case_type": case_type,
                "ticket_id": ticket.ticket_id,
                "matched_txn_id": matched_txn_id,
                "is_bangla": is_bangla,
            })
            fb_data = fb_resp.json()
            agent_summary  = fb_data["agent_summary"]
            next_action    = fb_data["recommended_next_action"]
            customer_reply = fb_data["customer_reply"]

        # ── Step 6: Safety sanitize LLM output only ───────────────────────────
        if used_llm:
            san_resp = await client.post(f"{SAFETY_URL}/sanitize", json={
                "customer_reply": customer_reply,
                "recommended_next_action": next_action,
                "is_bangla": is_bangla,
            })
            san_data = san_resp.json()
            customer_reply = san_data["customer_reply"]
            next_action    = san_data["recommended_next_action"]

    logger.info(f"Done | {ticket.ticket_id} | {case_type} | {evidence_verdict} | {department}")

    return TicketResponse(
        ticket_id=ticket.ticket_id,
        relevant_transaction_id=matched_txn_id,
        evidence_verdict=evidence_verdict,
        case_type=case_type,
        severity=severity,
        department=department,
        agent_summary=agent_summary,
        recommended_next_action=next_action,
        customer_reply=customer_reply,
        human_review_required=human_review,
        confidence=confidence,
        reason_codes=reason_codes,
    )