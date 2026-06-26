import re
import logging
from typing import Optional, List
from fastapi import FastAPI
from pydantic import BaseModel
from models import (
    Transaction, CaseType, Department, Severity,
    EvidenceVerdict, TransactionStatus
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Classifier Service")


# ── Request/Response ──────────────────────────────────────────────────────────

class ClassifyRequest(BaseModel):
    complaint: str
    transaction_history: List[Transaction] = []
    matched_txn_id: Optional[str] = None
    user_type: str = "unknown"
    is_duplicate: bool = False

class ClassifyResponse(BaseModel):
    case_type: CaseType
    department: Department
    severity: Severity
    evidence_verdict: EvidenceVerdict
    human_review_required: bool
    confidence: float
    reason_codes: List[str]


# ── Classification logic ──────────────────────────────────────────────────────

CASE_TO_DEPT = {
    CaseType.wrong_transfer: Department.dispute_resolution,
    CaseType.payment_failed: Department.payments_ops,
    CaseType.refund_request: Department.customer_support,
    CaseType.duplicate_payment: Department.payments_ops,
    CaseType.merchant_settlement_delay: Department.merchant_operations,
    CaseType.agent_cash_in_issue: Department.agent_operations,
    CaseType.phishing_or_social_engineering: Department.fraud_risk,
    CaseType.other: Department.customer_support,
}


def detect_case_type(complaint: str, history: List[Transaction], user_type: str, is_duplicate: bool) -> CaseType:
    text = complaint.lower()

    if is_duplicate:
        return CaseType.duplicate_payment

    phishing = ['otp', 'pin', 'password', 'asked for my', 'called me', 'someone called',
                'fake', 'fraud', 'scam', 'পিন', 'ওটিপি', 'ফোন করে', 'প্রতারণা']
    if any(s in text for s in phishing):
        return CaseType.phishing_or_social_engineering

    if (any(s in text for s in ['settlement', 'সেটেলমেন্ট']) or user_type == 'merchant'
            or any(t.type.value == 'settlement' for t in history)):
        if any(t.type.value == 'settlement' and t.status.value == 'pending' for t in history):
            return CaseType.merchant_settlement_delay
        if user_type == 'merchant':
            return CaseType.merchant_settlement_delay

    cash_in = ['cash in', 'cash-in', 'agent', 'ক্যাশ ইন', 'এজেন্ট', 'জমা']
    if any(s in text for s in cash_in) or any(t.type.value == 'cash_in' for t in history):
        not_recv = ['not received', 'not credited', 'not reflected', 'আসেনি', 'পাইনি']
        if any(w in text for w in not_recv):
            return CaseType.agent_cash_in_issue
        if any(t.type.value == 'cash_in' and t.status.value in ('pending', 'failed') for t in history):
            return CaseType.agent_cash_in_issue

    dup_words = ['twice', 'two times', 'double', 'duplicate', 'charged twice', 'deducted twice', 'দুইবার']
    if any(w in text for w in dup_words):
        return CaseType.duplicate_payment

    failed = ['failed', 'not working', 'unsuccessful', 'error', 'ব্যর্থ']
    payment = ['paid', 'pay', 'payment', 'recharge', 'bill', 'পেমেন্ট']
    if any(f in text for f in failed) and any(p in text for p in payment):
        return CaseType.payment_failed
    if any(t.type.value == 'payment' and t.status.value == 'failed' for t in history):
        return CaseType.payment_failed

    refund = ['refund', 'return my money', 'money back', 'রিফান্ড', 'ফেরত']
    wrong = ['wrong', 'mistake', 'wrong number', 'wrong person', 'ভুল']
    if any(s in text for s in refund) and not any(w in text for w in wrong):
        return CaseType.refund_request

    transfer = ['sent', 'transfer', 'send', 'পাঠিয়েছি']
    if any(w in text for w in wrong) and (
        any(t in text for t in transfer)
        or any(tx.type.value == 'transfer' for tx in history)
    ):
        return CaseType.wrong_transfer

    return CaseType.other


def get_severity(case_type: CaseType, matched_txn: Optional[Transaction], history: List[Transaction], complaint: str) -> Severity:
    text = complaint.lower()
    if case_type == CaseType.phishing_or_social_engineering:
        return Severity.critical

    amount = matched_txn.amount if matched_txn else (max(t.amount for t in history) if history else 0.0)

    if amount >= 5000:
        return Severity.high

    if case_type in (CaseType.wrong_transfer, CaseType.duplicate_payment):
        return Severity.high if amount >= 2000 else Severity.medium

    if case_type == CaseType.payment_failed:
        deducted = ['deducted', 'balance gone', 'taken', 'কেটে', 'কাটা']
        return Severity.high if any(s in text for s in deducted) else (Severity.medium if amount >= 500 else Severity.low)

    if case_type == CaseType.merchant_settlement_delay:
        return Severity.high if amount >= 10000 else Severity.medium

    if case_type == CaseType.agent_cash_in_issue:
        return Severity.high if amount >= 2000 else Severity.medium

    if case_type == CaseType.refund_request:
        return Severity.low

    return Severity.low


def get_verdict(complaint: str, matched_txn_id: Optional[str], history: List[Transaction], case_type: CaseType) -> EvidenceVerdict:
    text = complaint.lower()
    if not history:
        return EvidenceVerdict.insufficient_data
    if not matched_txn_id:
        return EvidenceVerdict.insufficient_data

    matched = next((t for t in history if t.transaction_id == matched_txn_id), None)
    if not matched:
        return EvidenceVerdict.insufficient_data

    failed_claim = any(w in text for w in ['failed', 'not working', 'unsuccessful', 'ব্যর্থ'])
    if failed_claim and matched.status.value == 'completed':
        deducted = any(w in text for w in ['deducted', 'balance gone', 'taken'])
        if not deducted:
            return EvidenceVerdict.inconsistent

    not_recv = any(w in text for w in ['not received', 'he didn\'t get', 'she didn\'t get', 'আসেনি'])
    if not_recv and matched.status.value == 'completed':
        return EvidenceVerdict.inconsistent

    if case_type == CaseType.wrong_transfer:
        same_cp = sum(1 for t in history if t.counterparty == matched.counterparty and t.transaction_id != matched_txn_id)
        if same_cp >= 2:
            return EvidenceVerdict.inconsistent

    return EvidenceVerdict.consistent


def needs_human_review(case_type: CaseType, verdict: EvidenceVerdict, severity: Severity, matched_txn: Optional[Transaction]) -> bool:
    if case_type in (CaseType.wrong_transfer, CaseType.phishing_or_social_engineering, CaseType.duplicate_payment):
        return True
    if verdict == EvidenceVerdict.inconsistent:
        return True
    if severity in (Severity.high, Severity.critical):
        return True
    if case_type == CaseType.agent_cash_in_issue:
        return True
    if matched_txn and matched_txn.status.value == 'pending':
        return True
    return False


# ── Endpoint ──────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/classify", response_model=ClassifyResponse)
def classify(req: ClassifyRequest):
    history = req.transaction_history or []
    matched_txn = next((t for t in history if t.transaction_id == req.matched_txn_id), None) if req.matched_txn_id else None

    case_type = detect_case_type(req.complaint, history, req.user_type, req.is_duplicate)
    department = CASE_TO_DEPT.get(case_type, Department.customer_support)
    if case_type == CaseType.refund_request and req.user_type == 'merchant':
        department = Department.dispute_resolution

    severity = get_severity(case_type, matched_txn, history, req.complaint)
    verdict = get_verdict(req.complaint, req.matched_txn_id, history, case_type)
    human_review = needs_human_review(case_type, verdict, severity, matched_txn)

    reason_codes = [case_type.value]
    if matched_txn:
        reason_codes.append("transaction_match")
        reason_codes.append(f"{matched_txn.status.value}_transaction")
    else:
        reason_codes.append("no_transaction_match")
    reason_codes.append("evidence_" + ("supported" if verdict == EvidenceVerdict.consistent else
                                        "inconsistent" if verdict == EvidenceVerdict.inconsistent else "insufficient"))

    base_conf = 0.8 if req.matched_txn_id else 0.5
    if verdict == EvidenceVerdict.consistent:
        confidence = min(base_conf + 0.15, 0.99)
    elif verdict == EvidenceVerdict.inconsistent:
        confidence = min(base_conf + 0.05, 0.85)
    else:
        confidence = max(base_conf - 0.1, 0.5)

    return ClassifyResponse(
        case_type=case_type,
        department=department,
        severity=severity,
        evidence_verdict=verdict,
        human_review_required=human_review,
        confidence=round(confidence, 2),
        reason_codes=reason_codes,
    )
