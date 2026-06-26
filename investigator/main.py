import re
import logging
from typing import Optional, List, Tuple
from fastapi import FastAPI
from pydantic import BaseModel
from models import Transaction, TransactionType, TransactionStatus

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Investigator Service")


# ── Request/Response for this service ────────────────────────────────────────

class InvestigateRequest(BaseModel):
    complaint: str
    transaction_history: List[Transaction] = []

class InvestigateResponse(BaseModel):
    relevant_transaction_id: Optional[str]
    match_confidence: float
    is_duplicate: bool
    duplicate_txn_id: Optional[str]


# ── Core matching logic ───────────────────────────────────────────────────────

def extract_amounts(text: str) -> List[float]:
    amounts = []
    en_pattern = r'\b(\d[\d,]*(?:\.\d+)?)\s*(?:taka|bdt|tk|৳)\b'
    for m in re.finditer(en_pattern, text, re.IGNORECASE):
        try:
            amounts.append(float(m.group(1).replace(',', '')))
        except ValueError:
            pass

    # Bangla digits — with or without টাকা
    bn_map = str.maketrans('০১২৩৪৫৬৭৮৯', '0123456789')
    for chunk in re.findall(r'[০-৯]+', text):
        try:
            val = float(chunk.translate(bn_map))
            if 10 <= val <= 500000:
                amounts.append(val)
        except ValueError:
            pass

    # Bare number fallback
    if not amounts:
        for b in re.findall(r'\b(\d{3,6})\b', text):
            try:
                val = float(b)
                if 10 <= val <= 500000:
                    amounts.append(val)
            except ValueError:
                pass
    return amounts


def extract_phones(text: str) -> List[str]:
    return re.findall(r'\+?880\d{10}|\b01[3-9]\d{8}\b', text)


def score_transaction(txn: Transaction, complaint: str, amounts: List[float], phones: List[str]) -> float:
    score = 0.0
    complaint_lower = complaint.lower()

    if amounts:
        for amt in amounts:
            if abs(txn.amount - amt) < 0.01:
                score += 5.0
                break
            elif abs(txn.amount - amt) / max(amt, 1) < 0.10:
                score += 2.0

    for phone in phones:
        clean_phone = phone.replace('+880', '01').replace(' ', '')
        clean_cp = txn.counterparty.replace('+880', '01').replace(' ', '')
        if clean_phone in clean_cp or clean_cp in clean_phone:
            score += 4.0

    type_hints = {
        'transfer': ['sent', 'transfer', 'wrong number', 'wrong person', 'পাঠিয়েছি'],
        'payment': ['paid', 'payment', 'bill', 'merchant', 'recharge', 'পেমেন্ট'],
        'cash_in': ['cash in', 'deposit', 'agent', 'ক্যাশ ইন', 'জমা'],
        'cash_out': ['cash out', 'withdraw', 'ক্যাশ আউট'],
        'settlement': ['settlement', 'settle', 'সেটেলমেন্ট'],
        'refund': ['refund', 'return', 'রিফান্ড'],
    }
    for txn_type, keywords in type_hints.items():
        if txn.type.value == txn_type:
            if any(kw in complaint_lower for kw in keywords):
                score += 3.0

    failure_words = ['failed', 'not received', 'unsuccessful', 'ব্যর্থ', 'আসেনি']
    if txn.status.value == 'failed' and any(w in complaint_lower for w in failure_words):
        score += 2.0

    pending_words = ['pending', 'not credited', 'not received', 'not reflected', 'আসেনি']
    if txn.status.value == 'pending' and any(w in complaint_lower for w in pending_words):
        score += 2.0

    return score


def find_duplicate(history: List[Transaction]) -> Optional[str]:
    if len(history) < 2:
        return None
    for i in range(len(history)):
        for j in range(i + 1, len(history)):
            a, b = history[i], history[j]
            if (
                a.type == b.type
                and a.counterparty == b.counterparty
                and abs(a.amount - b.amount) < 0.01
                and a.status.value == 'completed'
                and b.status.value == 'completed'
            ):
                return b.transaction_id if a.timestamp <= b.timestamp else a.transaction_id
    return None


# ── Endpoint ──────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/investigate", response_model=InvestigateResponse)
def investigate(req: InvestigateRequest):
    history = req.transaction_history or []

    dup_id = find_duplicate(history)
    if dup_id:
        return InvestigateResponse(
            relevant_transaction_id=dup_id,
            match_confidence=0.93,
            is_duplicate=True,
            duplicate_txn_id=dup_id,
        )

    if not history:
        return InvestigateResponse(
            relevant_transaction_id=None,
            match_confidence=0.0,
            is_duplicate=False,
            duplicate_txn_id=None,
        )

    amounts = extract_amounts(req.complaint)
    phones = extract_phones(req.complaint)

    scored = [
        (txn.transaction_id, score_transaction(txn, req.complaint, amounts, phones), txn)
        for txn in history
    ]
    scored.sort(key=lambda x: x[1], reverse=True)
    best_id, best_score, _ = scored[0]

    if best_score < 3.0:
        return InvestigateResponse(relevant_transaction_id=None, match_confidence=0.0, is_duplicate=False, duplicate_txn_id=None)

    if len(scored) > 1 and abs(scored[0][1] - scored[1][1]) < 1.0 and scored[1][1] >= 3.0:
        return InvestigateResponse(relevant_transaction_id=None, match_confidence=0.0, is_duplicate=False, duplicate_txn_id=None)

    return InvestigateResponse(
        relevant_transaction_id=best_id,
        match_confidence=min(best_score / 10.0, 0.99),
        is_duplicate=False,
        duplicate_txn_id=None,
    )
