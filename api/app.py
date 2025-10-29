# api/main.py
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field, EmailStr
from typing import List, Dict, Any
from math import isclose
import io
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas
import smtplib
from email.message import EmailMessage

app = FastAPI(title="APR Calculator API")

# ---------- Models ----------
class CalcRequest(BaseModel):
    P: float = Field(..., gt=0)
    R: float = Field(..., gt=0)   # user enters 18.5 for 18.5%
    N: int = Field(..., gt=0)

class ScheduleRow(BaseModel):
    month: int
    payment: float
    principal: float
    interest: float
    balance: float

class CalcResponse(BaseModel):
    apr: float
    emi: float
    processing_fee: float
    disbursed_amount: float
    total_interest_paid: float
    schedule: List[ScheduleRow]

class EmailRequest(BaseModel):
    to_email: EmailStr
    subject: str = "APR Report"
    body: str = "Please find attached APR report."
    data: CalcRequest

# ---------- Finance functions ----------
def emi_from_P_RN(P, annual_R_percent, N):
    R = annual_R_percent / 100.0
    r = R / 12.0
    if isclose(r, 0.0):
        return P / N
    return P * r * (1 + r) ** N / ((1 + r) ** N - 1)

def pv_of_annuity(EMI, r, N):
    if isclose(r, 0.0):
        return EMI * N
    return EMI * (1 - (1 + r) ** (-N)) / r

def solve_monthly_rate_bisection(EMI, DA, N, tol=1e-12, max_iter=200):
    low = 0.0
    high = 1.0
    f_low = pv_of_annuity(EMI, low, N) - DA
    f_high = pv_of_annuity(EMI, high, N) - DA
    expand_iter = 0
    while f_low * f_high > 0 and expand_iter < 60:
        high *= 2
        f_high = pv_of_annuity(EMI, high, N) - DA
        expand_iter += 1
    if f_low * f_high > 0:
        raise ValueError("Unable to bracket root for monthly rate. Check inputs.")
    for _ in range(max_iter):
        mid = (low + high) / 2.0
        f_mid = pv_of_annuity(EMI, mid, N) - DA
        if abs(f_mid) <= tol:
            return mid
        if f_low * f_mid <= 0:
            high = mid
            f_high = f_mid
        else:
            low = mid
            f_low = f_mid
    return (low + high) / 2.0

def amortization_schedule(P, R_percent, N, processing_fee_amount):
    emi = emi_from_P_RN(P, R_percent, N)
    balance = P
    schedule = []
    total_interest = 0.0
    r = (R_percent / 100.0) / 12.0
    for m in range(1, N+1):
        interest = balance * r
        principal = emi - interest
        balance = max(0.0, balance - principal)
        total_interest += interest
        schedule.append({
            "month": m,
            "payment": emi,
            "principal": principal,
            "interest": interest,
            "balance": balance
        })
    return emi, schedule, total_interest

# ---------- API endpoints ----------
@app.post("/calculate", response_model=CalcResponse)
def calculate(req: CalcRequest):
    P, R, N = req.P, req.R, req.N

    # processing fee including GST as per your last instruction
    processing_fee = P * 0.03 * 1.18

    # disbursed amount
    disbursed_amount = P - processing_fee

    # EMI based on nominal rate and original principal
    EMI = emi_from_P_RN(P, R, N)

    # Solve for monthly internal rate that discounts the EMIs to disbursed_amount
    monthly_r = solve_monthly_rate_bisection(EMI, disbursed_amount, N)
    apr_annual = monthly_r * 12.0

    # amortization schedule and totals (on full principal)
    emi_calc, schedule, total_interest = amortization_schedule(P, R, N, processing_fee)

    return {
        "apr": apr_annual,
        "emi": EMI,
        "processing_fee": processing_fee,
        "disbursed_amount": disbursed_amount,
        "total_interest_paid": total_interest,
        "schedule": schedule
    }

@app.post("/pdf")
def generate_pdf(req: CalcRequest):
    """Return a PDF bytes response with the calculation summary"""
    P, R, N = req.P, req.R, req.N
    processing_fee = P * 0.03 * 1.18
    disbursed_amount = P - processing_fee
    EMI = emi_from_P_RN(P, R, N)
    monthly_r = solve_monthly_rate_bisection(EMI, disbursed_amount, N)
    apr_annual = monthly_r * 12.0
    emi_calc, schedule, total_interest = amortization_schedule(P, R, N, processing_fee)

    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=letter)
    c.setFont('Helvetica-Bold', 14)
    c.drawString(30, 750, "APR Calculation Report")
    c.setFont('Helvetica', 10)
    c.drawString(30, 730, f"Principal (P): {P:,.2f}")
    c.drawString(30, 716, f"Rate (R): {R}%")
    c.drawString(30, 702, f"Tenure (N months): {N}")
    c.drawString(30, 688, f"Processing Fee (3% * P * 1.18): {processing_fee:,.2f}")
    c.drawString(30, 674, f"Disbursed Amount: {disbursed_amount:,.2f}")
    c.drawString(30, 660, f"EMI: {EMI:,.2f}")
    c.drawString(30, 646, f"APR (annualized): {apr_annual*100:.2f}%")
    c.showPage()
    c.save()
    buffer.seek(0)
    return StreamingResponse(buffer, media_type='application/pdf', headers={"Content-Disposition": "attachment; filename=apr-report.pdf"})

@app.post("/email")
def email_report(req: EmailRequest):
    """Send an email with the PDF attached (uses SMTP)"""
    # Environment variables expected:
    # SMTP_HOST, SMTP_PORT (int), SMTP_USER, SMTP_PASS, FROM_EMAIL
    import os
    SMTP_HOST = os.getenv("SMTP_HOST")
    SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
    SMTP_USER = os.getenv("SMTP_USER")
    SMTP_PASS = os.getenv("SMTP_PASS")
    FROM_EMAIL = os.getenv("FROM_EMAIL")

    if not (SMTP_HOST and SMTP_USER and SMTP_PASS and FROM_EMAIL):
        raise HTTPException(status_code=400, detail="SMTP configuration missing in environment")

    # Generate PDF bytes
    calc_req = CalcRequest(**req.data.dict())
    # reuse generate_pdf logic (simple approach)
    P, R, N = calc_req.P, calc_req.R, calc_req.N
    processing_fee = P * 0.03 * 1.18
    disbursed_amount = P - processing_fee
    EMI = emi_from_P_RN(P, R, N)
    monthly_r = solve_monthly_rate_bisection(EMI, disbursed_amount, N)
    apr_annual = monthly_r * 12.0
    emi_calc, schedule, total_interest = amortization_schedule(P, R, N, processing_fee)

    # build simple PDF in-memory
    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=letter)
    c.setFont('Helvetica-Bold', 14)
    c.drawString(30, 750, "APR Calculation Report")
    c.setFont('Helvetica', 10)
    c.drawString(30, 730, f"Principal (P): {P:,.2f}")
    c.drawString(30, 716, f"Rate (R): {R}%")
    c.drawString(30, 702, f"Tenure (N months): {N}")
    c.drawString(30, 688, f"Processing Fee (3% * P * 1.18): {processing_fee:,.2f}")
    c.drawString(30, 674, f"Disbursed Amount: {disbursed_amount:,.2f}")
    c.drawString(30, 660, f"EMI: {EMI:,.2f}")
    c.drawString(30, 646, f"APR (annualized): {apr_annual*100:.2f}%")
    c.showPage()
    c.save()
    buffer.seek(0)
    pdf_bytes = buffer.read()

    # send email
    msg = EmailMessage()
    msg['Subject'] = req.subject
    msg['From'] = FROM_EMAIL
    msg['To'] = req.to_email
    msg.set_content(req.body)
    msg.add_attachment(pdf_bytes, maintype='application', subtype='pdf', filename='apr-report.pdf')

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
            s.starttls()
            s.login(SMTP_USER, SMTP_PASS)
            s.send_message(msg)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"SMTP send failed: {e}")

    return {"status": "sent"}
