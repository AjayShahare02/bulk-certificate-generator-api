import os
import uuid
import sqlite3
from typing import List
from pydantic import BaseModel, EmailStr
from fastapi import FastAPI, BackgroundTasks, HTTPException
from fastapi.responses import FileResponse
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

app = FastAPI(title="Bulk Certificate Generator")

# Ensure storage folder exists
OS_STORAGE_DIR = "generated_certs"
os.makedirs(OS_STORAGE_DIR, exist_ok=True)

# --- DATABASE SETUP ---
DB_NAME = "certificates.db"

def init_db():
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
                job_id TEXT PRIMARY KEY,
                status TEXT,
                total INTEGER,
                completed INTEGER,
                failed INTEGER
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS certificates (
                id TEXT PRIMARY KEY,
                job_id TEXT,
                name TEXT,
                email TEXT,
                status TEXT,
                file_path TEXT,
                error TEXT
            )
        """)
        conn.commit()

init_db()

# --- SCHEMAS ---
class Recipient(BaseModel):
    name: str
    email: EmailStr

class BulkCertificateRequest(BaseModel):
    course_name: str
    recipients: List[Recipient]

# --- PDF GENERATOR HELPER ---
def generate_pdf(recipient_name: str, course_name: str, output_path: str):
    c = canvas.Canvas(output_path, pagesize=letter)
    c.setFont("Helvetica-Bold", 24)
    c.drawCentredString(300, 500, "Certificate of Completion")
    c.setFont("Helvetica", 16)
    c.drawCentredString(300, 450, "This is proudly presented to")
    c.setFont("Helvetica-Bold", 20)
    c.drawCentredString(300, 400, recipient_name)
    c.setFont("Helvetica", 14)
    c.drawCentredString(300, 350, f"For successfully completing {course_name}")
    c.save()

# --- BACKGROUND WORKER ---
def process_bulk_generation(job_id: str, course_name: str, recipients: list):
    completed = 0
    failed = 0

    for r in recipients:
        cert_id = str(uuid.uuid4())
        file_path = os.path.join(OS_STORAGE_DIR, f"{cert_id}.pdf")
        
        try:
            generate_pdf(r["name"], course_name, file_path)
            status = "SUCCESS"
            error_msg = None
            completed += 1
        except Exception as e:
            status = "FAILED"
            error_msg = str(e)
            file_path = None
            failed += 1

        with sqlite3.connect(DB_NAME) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO certificates VALUES (?, ?, ?, ?, ?, ?, ?)",
                (cert_id, job_id, r["name"], r["email"], status, file_path, error_msg)
            )
            cursor.execute(
                "UPDATE jobs SET completed = ?, failed = ? WHERE job_id = ?",
                (completed, failed, job_id)
            )
            conn.commit()

    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE jobs SET status = 'COMPLETED' WHERE job_id = ?", (job_id,))
        conn.commit()

# --- API ENDPOINTS ---
@app.post("/api/certificates/generate", status_code=202)
def generate_certificates(payload: BulkCertificateRequest, background_tasks: BackgroundTasks):
    if not payload.recipients:
        raise HTTPException(status_code=400, detail="Recipient list cannot be empty.")

    job_id = str(uuid.uuid4())
    recipients_data = [r.model_dump() if hasattr(r, 'model_dump') else r.dict() for r in payload.recipients]

    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO jobs VALUES (?, 'PROCESSING', ?, 0, 0)",
            (job_id, len(recipients_data))
        )
        conn.commit()

    background_tasks.add_task(process_bulk_generation, job_id, payload.course_name, recipients_data)

    return {"job_id": job_id, "status": "PROCESSING", "total": len(recipients_data)}

@app.get("/api/certificates/status/{job_id}")
def get_job_status(job_id: str):
    with sqlite3.connect(DB_NAME) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        job = cursor.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        
        if not job:
            raise HTTPException(status_code=404, detail="Job not found.")
            
        certs = cursor.execute("SELECT id, name, email, status, error FROM certificates WHERE job_id = ?", (job_id,)).fetchall()

    return {
        "job_id": job["job_id"],
        "status": job["status"],
        "total": job["total"],
        "completed": job["completed"],
        "failed": job["failed"],
        "results": [dict(c) for c in certs]
    }

@app.get("/api/certificates/download/{cert_id}")
def download_certificate(cert_id: str):
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        res = cursor.execute("SELECT file_path, status FROM certificates WHERE id = ?", (cert_id,)).fetchone()

    if not res or res[1] != "SUCCESS" or not os.path.exists(res[0]):
        raise HTTPException(status_code=404, detail="Certificate file not found or generation failed.")

    return FileResponse(res[0], media_type="application/pdf", filename=f"certificate_{cert_id}.pdf")