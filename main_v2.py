import os, json, base64 as b64lib, hashlib, secrets
from fastapi import FastAPI, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, List
import asyncpg

app = FastAPI(title="DentalPro PMS API", version="3.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

DATABASE_URL = os.getenv("DATABASE_URL", "")
_pool = None

async def get_pool():
    global _pool
    if not _pool:
        _pool = await asyncpg.create_pool(DATABASE_URL, ssl="require")
    return _pool

async def run_migrations():
    pool = await get_pool()
    async with pool.acquire() as conn:
        # ── Core tables ───────────────────────────────────────────
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS clinics (
                id SERIAL PRIMARY KEY,
                name VARCHAR(200) NOT NULL,
                owner_email VARCHAR(200) UNIQUE NOT NULL,
                owner_name VARCHAR(200),
                phone VARCHAR(50),
                address TEXT,
                logo_url TEXT,
                plan VARCHAR(20) DEFAULT 'trial',
                created_at TIMESTAMPTZ DEFAULT NOW()
            );
            CREATE TABLE IF NOT EXISTS staff (
                id SERIAL PRIMARY KEY,
                clinic_id INTEGER REFERENCES clinics(id),
                name VARCHAR(200) NOT NULL,
                email VARCHAR(200) NOT NULL,
                role VARCHAR(50) NOT NULL,
                invite_code VARCHAR(20) UNIQUE,
                permissions JSONB DEFAULT '{}'::jsonb,
                active BOOLEAN DEFAULT true,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                UNIQUE(clinic_id, email)
            );
            CREATE TABLE IF NOT EXISTS patients (
                id SERIAL PRIMARY KEY,
                clinic_id INTEGER REFERENCES clinics(id),
                name VARCHAR(200) NOT NULL,
                email VARCHAR(200),
                phone VARCHAR(50),
                portal_code VARCHAR(20) UNIQUE,
                created_at TIMESTAMPTZ DEFAULT NOW()
            );
            CREATE TABLE IF NOT EXISTS appointments (
                id SERIAL PRIMARY KEY,
                clinic_id INTEGER REFERENCES clinics(id),
                patient_id INTEGER REFERENCES patients(id),
                patient_name VARCHAR(200),
                scheduled_at TIMESTAMPTZ NOT NULL,
                reason TEXT,
                urgency_level VARCHAR(10) DEFAULT 'green',
                status VARCHAR(20) DEFAULT 'scheduled',
                notes TEXT,
                created_at TIMESTAMPTZ DEFAULT NOW()
            );
            CREATE TABLE IF NOT EXISTS inventory_items (
                id SERIAL PRIMARY KEY,
                clinic_id INTEGER REFERENCES clinics(id),
                name VARCHAR(200) NOT NULL,
                category VARCHAR(100),
                stock DECIMAL DEFAULT 0,
                unit VARCHAR(50) DEFAULT 'pza',
                min_stock DECIMAL DEFAULT 5,
                price DECIMAL DEFAULT 0,
                created_at TIMESTAMPTZ DEFAULT NOW()
            );
        """)

        # ── Staff invite enhancements (ALTER, idempotent) ─────────
        await conn.execute("""
            ALTER TABLE staff
                ADD COLUMN IF NOT EXISTS invite_expires_at TIMESTAMPTZ,
                ADD COLUMN IF NOT EXISTS invite_max_uses INTEGER DEFAULT 1,
                ADD COLUMN IF NOT EXISTS invite_uses INTEGER DEFAULT 0,
                ADD COLUMN IF NOT EXISTS referring_doctor_id INTEGER REFERENCES staff(id);
        """)

        # ── Patient communication tables ──────────────────────────
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS patient_messages (
                id SERIAL PRIMARY KEY,
                clinic_id INTEGER REFERENCES clinics(id),
                patient_id INTEGER REFERENCES patients(id),
                doctor_name VARCHAR(200),
                subject VARCHAR(300),
                body TEXT NOT NULL,
                read_at TIMESTAMPTZ,
                created_at TIMESTAMPTZ DEFAULT NOW()
            );
            CREATE TABLE IF NOT EXISTS patient_prescriptions (
                id SERIAL PRIMARY KEY,
                clinic_id INTEGER REFERENCES clinics(id),
                patient_id INTEGER REFERENCES patients(id),
                doctor_name VARCHAR(200),
                medication VARCHAR(300) NOT NULL,
                dose VARCHAR(200),
                frequency VARCHAR(200),
                duration VARCHAR(200),
                route VARCHAR(100),
                instructions TEXT,
                created_at TIMESTAMPTZ DEFAULT NOW()
            );
            CREATE TABLE IF NOT EXISTS patient_postop (
                id SERIAL PRIMARY KEY,
                clinic_id INTEGER REFERENCES clinics(id),
                patient_id INTEGER REFERENCES patients(id),
                doctor_name VARCHAR(200),
                procedure_name VARCHAR(300),
                instructions TEXT NOT NULL,
                restrictions TEXT,
                medications TEXT,
                follow_up_date TIMESTAMPTZ,
                emergency_contact VARCHAR(200),
                created_at TIMESTAMPTZ DEFAULT NOW()
            );
        """)
    print("Migrations OK")

from contextlib import asynccontextmanager
@asynccontextmanager
async def lifespan(app):
    await run_migrations()
    yield
app.router.lifespan_context = lifespan

# ── AUTH ──────────────────────────────────────────────────────────────
def decode_token(token):
    try:
        parts = token.split(".")
        if len(parts) == 3:
            padding = 4 - len(parts[1]) % 4
            payload = json.loads(b64lib.urlsafe_b64decode(parts[1] + "=" * padding))
            return payload
    except:
        pass
    return None

async def get_clinic_from_token(authorization: str):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Token requerido")
    token = authorization.split(" ")[1]
    payload = decode_token(token)
    if not payload:
        raise HTTPException(status_code=401, detail="Token invalido")
    email = payload.get("email")
    if not email:
        raise HTTPException(status_code=401, detail="Email no encontrado en token")
    pool = await get_pool()
    async with pool.acquire() as conn:
        # Buscar como dueño
        clinic = await conn.fetchrow("SELECT * FROM clinics WHERE owner_email=$1", email)
        if clinic:
            return dict(clinic), {"role": "owner", "email": email, "name": payload.get("name")}
        # Buscar como staff
        staff = await conn.fetchrow("""
            SELECT s.*, c.* FROM staff s
            JOIN clinics c ON s.clinic_id = c.id
            WHERE s.email=$1 AND s.active=true
        """, email)
        if staff:
            return {"id": staff["clinic_id"], "name": staff["name"]}, {"role": staff["role"], "email": email, "name": staff["name"], "permissions": dict(staff["permissions"] or {})}
    return None, {"role": "new", "email": email, "name": payload.get("name")}

# ── MODELS ────────────────────────────────────────────────────────────
class ClinicCreate(BaseModel):
    name: str
    owner_name: str
    phone: Optional[str] = None
    address: Optional[str] = None

class StaffCreate(BaseModel):
    name: str
    email: str
    role: str
    permissions: Optional[dict] = {}

class PatientCreate(BaseModel):
    name: str
    email: Optional[str] = None
    phone: Optional[str] = None

class AppointmentCreate(BaseModel):
    patient_id: int
    scheduled_at: str
    reason: Optional[str] = None
    urgency_level: Optional[str] = "green"
    notes: Optional[str] = None

class InventoryCreate(BaseModel):
    name: str
    category: Optional[str] = None
    stock: Optional[float] = 0
    unit: Optional[str] = "pza"
    min_stock: Optional[float] = 5
    price: Optional[float] = 0

class MessageCreate(BaseModel):
    subject: Optional[str] = None
    body: str

class PrescriptionCreate(BaseModel):
    medication: str
    dose: Optional[str] = None
    frequency: Optional[str] = None
    duration: Optional[str] = None
    route: Optional[str] = None
    instructions: Optional[str] = None

class PostOpCreate(BaseModel):
    procedure_name: Optional[str] = None
    instructions: str
    restrictions: Optional[str] = None
    medications: Optional[str] = None
    follow_up_date: Optional[str] = None
    emergency_contact: Optional[str] = None

# ── ENDPOINTS ────────────────────────────────────────────────────────
@app.get("/")
async def root():
    return {"name": "DentalPro PMS API", "version": "3.0.0", "status": "online"}

@app.get("/health")
async def health():
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
        return {"status": "healthy", "db": "connected"}
    except Exception as e:
        return {"status": "unhealthy", "error": str(e)}

@app.get("/auth/verify")
async def auth_verify(authorization: str = Header(None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Token requerido")
    token = authorization.split(" ")[1]
    payload = decode_token(token)
    if not payload:
        raise HTTPException(status_code=401, detail="Token invalido")
    email = payload.get("email")
    pool = await get_pool()
    async with pool.acquire() as conn:
        clinic = await conn.fetchrow("SELECT id, name FROM clinics WHERE owner_email=$1", email)
        if clinic:
            return {"valid": True, "role": "owner", "clinic_id": clinic["id"], "clinic_name": clinic["name"], "email": email, "name": payload.get("name")}
        staff = await conn.fetchrow("SELECT s.role, s.permissions, c.id as cid, c.name as cname FROM staff s JOIN clinics c ON s.clinic_id=c.id WHERE s.email=$1 AND s.active=true", email)
        if staff:
            return {"valid": True, "role": staff["role"], "clinic_id": staff["cid"], "clinic_name": staff["cname"], "email": email, "name": payload.get("name"), "permissions": dict(staff["permissions"] or {})}
    return {"valid": True, "role": "new", "email": email, "name": payload.get("name")}

# ── CLINICS ───────────────────────────────────────────────────────────
@app.post("/clinics")
async def create_clinic(data: ClinicCreate, authorization: str = Header(None)):
    if not authorization:
        raise HTTPException(status_code=401, detail="Token requerido")
    token = authorization.split(" ")[1] if " " in authorization else authorization
    payload = decode_token(token)
    if not payload:
        raise HTTPException(status_code=401, detail="Token invalido")
    email = payload.get("email")
    pool = await get_pool()
    async with pool.acquire() as conn:
        existing = await conn.fetchrow("SELECT id FROM clinics WHERE owner_email=$1", email)
        if existing:
            raise HTTPException(status_code=400, detail="Ya tienes una clínica registrada")
        clinic = await conn.fetchrow(
            "INSERT INTO clinics (name, owner_email, owner_name, phone, address) VALUES ($1,$2,$3,$4,$5) RETURNING *",
            data.name, email, data.owner_name, data.phone, data.address
        )
        # Crear staff entry para el dueño
        invite = secrets.token_urlsafe(6).upper()
        await conn.execute(
            "INSERT INTO staff (clinic_id, name, email, role, invite_code, permissions) VALUES ($1,$2,$3,'owner',$4,'{}'::jsonb)",
            clinic["id"], data.owner_name, email, invite
        )
        return dict(clinic)

@app.get("/clinics/me")
async def get_my_clinic(authorization: str = Header(None)):
    clinic, user = await get_clinic_from_token(authorization)
    if not clinic:
        return {"clinic": None, "role": user.get("role"), "email": user.get("email")}
    return {"clinic": clinic, "role": user.get("role"), "email": user.get("email")}

@app.put("/clinics/me")
async def update_clinic(data: dict, authorization: str = Header(None)):
    clinic, user = await get_clinic_from_token(authorization)
    if not clinic or user["role"] not in ["owner", "co_owner"]:
        raise HTTPException(status_code=403, detail="Sin permisos")
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("UPDATE clinics SET name=$1, phone=$2, address=$3 WHERE id=$4",
            data.get("name"), data.get("phone"), data.get("address"), clinic["id"])
    return {"ok": True}

# ── STAFF ─────────────────────────────────────────────────────────────
DEFAULT_PERMISSIONS = {
    "owner":       {"patients":True,"appointments":True,"inventory":True,"analytics":True,"staff":True,"config":True,"payments":True,"marketing":True},
    "co_owner":    {"patients":True,"appointments":True,"inventory":True,"analytics":True,"staff":True,"config":True,"payments":True,"marketing":True},
    "doctor":      {"patients":True,"appointments":True,"inventory":False,"analytics":False,"staff":False,"config":False,"payments":False,"marketing":False},
    "receptionist":{"patients":True,"appointments":True,"inventory":False,"analytics":False,"staff":False,"config":False,"payments":True,"marketing":False},
    "assistant":   {"patients":False,"appointments":True,"inventory":True,"analytics":False,"staff":False,"config":False,"payments":False,"marketing":False},
}

@app.get("/staff")
async def get_staff(authorization: str = Header(None)):
    clinic, user = await get_clinic_from_token(authorization)
    if not clinic:
        raise HTTPException(status_code=403, detail="Sin clínica")
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM staff WHERE clinic_id=$1 ORDER BY created_at", clinic["id"])
    return [dict(r) for r in rows]

@app.post("/staff")
async def add_staff(data: StaffCreate, authorization: str = Header(None)):
    clinic, user = await get_clinic_from_token(authorization)
    if not clinic or user["role"] not in ["owner", "co_owner"]:
        raise HTTPException(status_code=403, detail="Sin permisos")
    pool = await get_pool()
    invite = secrets.token_urlsafe(6).upper()
    perms = data.permissions or DEFAULT_PERMISSIONS.get(data.role, {})
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "INSERT INTO staff (clinic_id, name, email, role, invite_code, permissions) VALUES ($1,$2,$3,$4,$5,$6::jsonb) RETURNING *",
            clinic["id"], data.name, data.email, data.role, invite, json.dumps(perms)
        )
    return dict(row)

@app.put("/staff/{staff_id}/permissions")
async def update_permissions(staff_id: int, data: dict, authorization: str = Header(None)):
    clinic, user = await get_clinic_from_token(authorization)
    if not clinic or user["role"] not in ["owner", "co_owner"]:
        raise HTTPException(status_code=403, detail="Sin permisos")
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("UPDATE staff SET permissions=$1::jsonb WHERE id=$2 AND clinic_id=$3",
            json.dumps(data), staff_id, clinic["id"])
    return {"ok": True}

@app.delete("/staff/{staff_id}")
async def remove_staff(staff_id: int, authorization: str = Header(None)):
    clinic, user = await get_clinic_from_token(authorization)
    if not clinic or user["role"] not in ["owner", "co_owner"]:
        raise HTTPException(status_code=403, detail="Sin permisos")
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("UPDATE staff SET active=false WHERE id=$1 AND clinic_id=$2", staff_id, clinic["id"])
    return {"ok": True}

# ── PATIENTS ──────────────────────────────────────────────────────────
@app.get("/patients")
async def get_patients(authorization: str = Header(None)):
    clinic, user = await get_clinic_from_token(authorization)
    if not clinic:
        raise HTTPException(status_code=403, detail="Sin clínica")
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM patients WHERE clinic_id=$1 ORDER BY name", clinic["id"])
    return [dict(r) for r in rows]

@app.post("/patients")
async def create_patient(data: PatientCreate, authorization: str = Header(None)):
    clinic, user = await get_clinic_from_token(authorization)
    if not clinic:
        raise HTTPException(status_code=403, detail="Sin clínica")
    import random, string
    code = "PAC-" + "".join(random.choices(string.ascii_uppercase + string.digits, k=4)) + "-" + "".join(random.choices(string.digits, k=4))
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "INSERT INTO patients (clinic_id, name, email, phone, portal_code) VALUES ($1,$2,$3,$4,$5) RETURNING *",
            clinic["id"], data.name, data.email, data.phone, code
        )
    return dict(row)

@app.get("/patients/{patient_id}")
async def get_patient(patient_id: int, authorization: str = Header(None)):
    clinic, user = await get_clinic_from_token(authorization)
    if not clinic:
        raise HTTPException(status_code=403, detail="Sin clínica")
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM patients WHERE id=$1 AND clinic_id=$2", patient_id, clinic["id"])
    if not row:
        raise HTTPException(status_code=404, detail="Paciente no encontrado")
    return dict(row)

# ── APPOINTMENTS ──────────────────────────────────────────────────────
@app.get("/appointments")
async def get_appointments(authorization: str = Header(None)):
    clinic, user = await get_clinic_from_token(authorization)
    if not clinic:
        raise HTTPException(status_code=403, detail="Sin clínica")
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM appointments WHERE clinic_id=$1 ORDER BY scheduled_at", clinic["id"])
    return [dict(r) for r in rows]

@app.post("/appointments")
async def create_appointment(data: AppointmentCreate, authorization: str = Header(None)):
    clinic, user = await get_clinic_from_token(authorization)
    if not clinic:
        raise HTTPException(status_code=403, detail="Sin clínica")
    pool = await get_pool()
    async with pool.acquire() as conn:
        patient = await conn.fetchrow("SELECT name FROM patients WHERE id=$1 AND clinic_id=$2", data.patient_id, clinic["id"])
        if not patient:
            raise HTTPException(status_code=404, detail="Paciente no encontrado")
        row = await conn.fetchrow(
            "INSERT INTO appointments (clinic_id, patient_id, patient_name, scheduled_at, reason, urgency_level, notes) VALUES ($1,$2,$3,$4,$5,$6,$7) RETURNING *",
            clinic["id"], data.patient_id, patient["name"], data.scheduled_at, data.reason, data.urgency_level, data.notes
        )
    return dict(row)

@app.patch("/appointments/{appt_id}/status")
async def update_appointment_status(appt_id: int, data: dict, authorization: str = Header(None)):
    clinic, user = await get_clinic_from_token(authorization)
    if not clinic:
        raise HTTPException(status_code=403, detail="Sin clínica")
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("UPDATE appointments SET status=$1 WHERE id=$2 AND clinic_id=$3",
            data.get("status"), appt_id, clinic["id"])
    return {"ok": True}

# ── INVENTORY ─────────────────────────────────────────────────────────
@app.get("/inventory")
async def get_inventory(authorization: str = Header(None)):
    clinic, user = await get_clinic_from_token(authorization)
    if not clinic:
        raise HTTPException(status_code=403, detail="Sin clínica")
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM inventory_items WHERE clinic_id=$1 ORDER BY name", clinic["id"])
    return [dict(r) for r in rows]

@app.post("/inventory")
async def create_inventory(data: InventoryCreate, authorization: str = Header(None)):
    clinic, user = await get_clinic_from_token(authorization)
    if not clinic:
        raise HTTPException(status_code=403, detail="Sin clínica")
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "INSERT INTO inventory_items (clinic_id, name, category, stock, unit, min_stock, price) VALUES ($1,$2,$3,$4,$5,$6,$7) RETURNING *",
            clinic["id"], data.name, data.category, data.stock, data.unit, data.min_stock, data.price
        )
    return dict(row)

@app.patch("/inventory/{item_id}")
async def update_inventory(item_id: int, data: dict, authorization: str = Header(None)):
    clinic, user = await get_clinic_from_token(authorization)
    if not clinic:
        raise HTTPException(status_code=403, detail="Sin clínica")
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("UPDATE inventory_items SET stock=$1 WHERE id=$2 AND clinic_id=$3",
            data.get("stock"), item_id, clinic["id"])
    return {"ok": True}

# ── PORTAL PACIENTE ───────────────────────────────────────────────────
@app.get("/portal/{portal_code}")
async def get_portal_patient(portal_code: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        patient = await conn.fetchrow("SELECT * FROM patients WHERE portal_code=$1", portal_code)
        if not patient:
            raise HTTPException(status_code=404, detail="Código no válido")
        pid = patient["id"]
        apts   = await conn.fetch("SELECT * FROM appointments WHERE patient_id=$1 ORDER BY scheduled_at DESC LIMIT 20", pid)
        rxs    = await conn.fetch("SELECT * FROM patient_prescriptions WHERE patient_id=$1 ORDER BY created_at DESC", pid)
        postop = await conn.fetch("SELECT * FROM patient_postop WHERE patient_id=$1 ORDER BY created_at DESC", pid)
        msgs   = await conn.fetch("SELECT * FROM patient_messages WHERE patient_id=$1 ORDER BY created_at DESC", pid)
        # Mark unread messages as read
        await conn.execute("UPDATE patient_messages SET read_at=NOW() WHERE patient_id=$1 AND read_at IS NULL", pid)
    return {
        "patient":       dict(patient),
        "appointments":  [dict(a) for a in apts],
        "prescriptions": [dict(r) for r in rxs],
        "postop":        [dict(p) for p in postop],
        "messages":      [dict(m) for m in msgs],
    }

# ── PATIENT MESSAGES ──────────────────────────────────────────────────
@app.post("/patients/{patient_id}/messages")
async def send_message(patient_id: int, data: MessageCreate, authorization: str = Header(None)):
    clinic, user = await get_clinic_from_token(authorization)
    if not clinic:
        raise HTTPException(status_code=403, detail="Sin clínica")
    pool = await get_pool()
    async with pool.acquire() as conn:
        patient = await conn.fetchrow("SELECT id FROM patients WHERE id=$1 AND clinic_id=$2", patient_id, clinic["id"])
        if not patient:
            raise HTTPException(status_code=404, detail="Paciente no encontrado")
        row = await conn.fetchrow(
            "INSERT INTO patient_messages (clinic_id, patient_id, doctor_name, subject, body) VALUES ($1,$2,$3,$4,$5) RETURNING *",
            clinic["id"], patient_id, user.get("name"), data.subject, data.body
        )
    return dict(row)

@app.get("/patients/{patient_id}/messages")
async def get_messages(patient_id: int, authorization: str = Header(None)):
    clinic, user = await get_clinic_from_token(authorization)
    if not clinic:
        raise HTTPException(status_code=403, detail="Sin clínica")
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM patient_messages WHERE patient_id=$1 AND clinic_id=$2 ORDER BY created_at DESC",
            patient_id, clinic["id"]
        )
    return [dict(r) for r in rows]

# ── PATIENT PRESCRIPTIONS ─────────────────────────────────────────────
@app.post("/patients/{patient_id}/prescriptions")
async def add_prescription(patient_id: int, data: PrescriptionCreate, authorization: str = Header(None)):
    clinic, user = await get_clinic_from_token(authorization)
    if not clinic:
        raise HTTPException(status_code=403, detail="Sin clínica")
    pool = await get_pool()
    async with pool.acquire() as conn:
        patient = await conn.fetchrow("SELECT id FROM patients WHERE id=$1 AND clinic_id=$2", patient_id, clinic["id"])
        if not patient:
            raise HTTPException(status_code=404, detail="Paciente no encontrado")
        row = await conn.fetchrow(
            """INSERT INTO patient_prescriptions
               (clinic_id, patient_id, doctor_name, medication, dose, frequency, duration, route, instructions)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9) RETURNING *""",
            clinic["id"], patient_id, user.get("name"),
            data.medication, data.dose, data.frequency, data.duration, data.route, data.instructions
        )
    return dict(row)

@app.get("/patients/{patient_id}/prescriptions")
async def get_prescriptions(patient_id: int, authorization: str = Header(None)):
    clinic, user = await get_clinic_from_token(authorization)
    if not clinic:
        raise HTTPException(status_code=403, detail="Sin clínica")
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM patient_prescriptions WHERE patient_id=$1 AND clinic_id=$2 ORDER BY created_at DESC",
            patient_id, clinic["id"]
        )
    return [dict(r) for r in rows]

# ── PATIENT POST-OP INSTRUCTIONS ──────────────────────────────────────
@app.post("/patients/{patient_id}/postop")
async def add_postop(patient_id: int, data: PostOpCreate, authorization: str = Header(None)):
    clinic, user = await get_clinic_from_token(authorization)
    if not clinic:
        raise HTTPException(status_code=403, detail="Sin clínica")
    pool = await get_pool()
    async with pool.acquire() as conn:
        patient = await conn.fetchrow("SELECT id FROM patients WHERE id=$1 AND clinic_id=$2", patient_id, clinic["id"])
        if not patient:
            raise HTTPException(status_code=404, detail="Paciente no encontrado")
        follow_up = None
        if data.follow_up_date:
            from datetime import datetime
            try:
                follow_up = datetime.fromisoformat(data.follow_up_date)
            except ValueError:
                pass
        row = await conn.fetchrow(
            """INSERT INTO patient_postop
               (clinic_id, patient_id, doctor_name, procedure_name, instructions, restrictions, medications, follow_up_date, emergency_contact)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9) RETURNING *""",
            clinic["id"], patient_id, user.get("name"),
            data.procedure_name, data.instructions, data.restrictions,
            data.medications, follow_up, data.emergency_contact
        )
    return dict(row)

@app.get("/patients/{patient_id}/postop")
async def get_postop(patient_id: int, authorization: str = Header(None)):
    clinic, user = await get_clinic_from_token(authorization)
    if not clinic:
        raise HTTPException(status_code=403, detail="Sin clínica")
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM patient_postop WHERE patient_id=$1 AND clinic_id=$2 ORDER BY created_at DESC",
            patient_id, clinic["id"]
        )
    return [dict(r) for r in rows]

# ── INVITE CODE ───────────────────────────────────────────────────────
@app.get("/invite/{code}")
async def get_invite(code: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        staff = await conn.fetchrow("""
            SELECT s.*, c.name as clinic_name FROM staff s
            JOIN clinics c ON s.clinic_id=c.id
            WHERE s.invite_code=$1 AND s.active=true
        """, code.upper())
    if not staff:
        raise HTTPException(status_code=404, detail="Código de invitación no válido")

    # Check expiration
    if staff["invite_expires_at"] and staff["invite_expires_at"] < __import__("datetime").datetime.now(__import__("datetime").timezone.utc):
        raise HTTPException(status_code=410, detail="Código de invitación expirado")

    # Check max uses
    max_uses = staff["invite_max_uses"] or 1
    uses = staff["invite_uses"] or 0
    if uses >= max_uses:
        raise HTTPException(status_code=410, detail="Código de invitación ya fue utilizado")

    # Increment use counter
    async with pool.acquire() as conn:
        await conn.execute("UPDATE staff SET invite_uses = COALESCE(invite_uses,0) + 1 WHERE invite_code=$1", code.upper())

    return {
        "name":        staff["name"],
        "role":        staff["role"],
        "clinic_name": staff["clinic_name"],
        "invite_code": code.upper(),
    }
