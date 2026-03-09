"""
DentalPro PMS — Servidor Principal v2.0
========================================
Integra todos los módulos:
    • Auth (Google OAuth + JWT)
    • Agente de Recepción (Claude API)
    • Inventario Híbrido
    • Base de datos PostgreSQL (asyncpg)
    • WebSockets en tiempo real
    • Webhooks de WhatsApp (Meta API)
    • Cifrado AES-256-GCM (LFPDPPP / HIPAA)
    • Odontograma sincronizado con DB + Inventario
    • Portal del Paciente en tiempo real

Correr en desarrollo:
    uvicorn main:app --reload --port 8000

Correr en producción:
    gunicorn main:app -w 4 -k uvicorn.workers.UvicornWorker --bind 0.0.0.0:8000

Docs interactivas:
    http://localhost:8000/docs
"""

import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from fastapi import FastAPI, HTTPException, Request, Depends, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

# ── Módulos originales ─────────────────────────────────────────
from agents.reception_agent import router as agent_router
from inventory.hybrid_inventory import router as inventory_router
from auth.google_oauth import router as auth_router

# ── Módulos nuevos ─────────────────────────────────────────────
from db.database import (
    run_migrations,
    close_pool,
    OdontogramRepository,
    InventoryRepository,
    AppointmentRepository,
    PatientRepository,
    AuditRepository,
)
from websockets.ws_manager import ws_router, ws_manager, make_stock_low_event
from webhooks.whatsapp_webhook import router as webhook_router
from security.encryption import (
    encrypt_patient_sensitive,
    decrypt_patient_sensitive,
    hash_document,
    evict_dentist_key,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("dentalpro.main")


# ════════════════════════════════════════════════════════════════
# LIFESPAN — startup / shutdown
# ════════════════════════════════════════════════════════════════

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("🦷 DentalPro PMS arrancando…")
    await run_migrations()
    logger.info("✅ DB migrations completadas")
    yield
    await close_pool()
    logger.info("🔴 DentalPro PMS detenido")


# ════════════════════════════════════════════════════════════════
# APP
# ════════════════════════════════════════════════════════════════

app = FastAPI(
    title="🦷 DentalPro PMS — API v2",
    description="""
## DentalPro — Sistema de Gestión Dental Profesional

Backend completo con sincronización PostgreSQL, WebSockets en tiempo real,
webhooks WhatsApp y cifrado AES-256-GCM.

### Módulos

| Módulo | Prefix | Descripción |
|--------|--------|-------------|
| 🔐 Auth | `/auth` | Google OAuth 2.0, JWT, onboarding |
| 🤖 Agente IA | `/agent` | Triage de urgencia con Claude API |
| 📦 Inventario | `/inventory` | Stock híbrido con edición pre-confirmación |
| 🦷 Odontograma | `/odontogram` | FDI sync con PostgreSQL + inventario |
| 📅 Citas | `/appointments` | CRUD con urgency_level del agente |
| 👤 Portal | `/portal` | Vista del paciente en tiempo real |
| 💬 WhatsApp | `/webhooks` | Meta API webhook + bot engine |
| 🔔 WebSockets | `/ws` | Alertas en tiempo real al dashboard |
| 🔒 Seguridad | `/security` | Cifrado, auditoría, LFPDPPP |

### Variables de entorno requeridas
```
DATABASE_URL=postgresql://user:pass@localhost:5432/dentalpro
ANTHROPIC_API_KEY=sk-ant-...
GOOGLE_CLIENT_ID=...
GOOGLE_CLIENT_SECRET=...
JWT_SECRET_KEY=...
DENTALPRO_MASTER_KEY=<32 bytes hex>
WHATSAPP_VERIFY_TOKEN=...
WHATSAPP_APP_SECRET=...
WHATSAPP_ACCESS_TOKEN=...
```
""",
    version="2.0.0",
    lifespan=lifespan,
)

# ── Middlewares ────────────────────────────────────────────────
app.add_middleware(GZipMiddleware, minimum_size=1000)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://localhost:5173",
        os.environ.get("FRONTEND_URL", "https://app.dentalpro.mx"),
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Routers ────────────────────────────────────────────────────
app.include_router(auth_router)
app.include_router(agent_router)
app.include_router(inventory_router)
app.include_router(ws_router)
app.include_router(webhook_router)


# ════════════════════════════════════════════════════════════════
# ODONTOGRAM ENDPOINTS — sincronizados con PostgreSQL
# ════════════════════════════════════════════════════════════════

class ToothUpdateRequest(BaseModel):
    patient_id: str
    dentist_id: str
    tooth_fdi: int
    state: str
    previous_state: Optional[str] = None
    notes: Optional[str] = None
    appointment_id: Optional[str] = None
    source: str = "dentist"
    ai_confidence: Optional[float] = None
    # Inventario
    deduct_inventory: bool = False
    inventory_materials: Optional[list[dict]] = None  # [{item_id, delta, reason}]


TREATMENT_MATERIALS: dict[str, list[str]] = {
    "resin":      ["composite_a2", "acid_etch", "adhesive", "anesthesia"],
    "endo":       ["anesthesia", "k_files", "hypochlorite", "gutta_percha"],
    "extraction": ["anesthesia", "gauze"],
    "crown":      ["anesthesia", "temp_cement", "retraction_cord"],
    "implant":    ["anesthesia", "suture", "gauze"],
}


@app.post("/odontogram/tooth", tags=["Odontograma"])
async def update_tooth(req: ToothUpdateRequest):
    record = await OdontogramRepository.upsert_tooth(
        patient_id=req.patient_id,
        dentist_id=req.dentist_id,
        tooth_fdi=req.tooth_fdi,
        state=req.state,
        previous_state=req.previous_state,
        notes=req.notes,
        appointment_id=req.appointment_id,
        source=req.source,
        ai_confidence=req.ai_confidence,
    )

    inventory_result = []

    if req.deduct_inventory and req.inventory_materials:
        inventory_result = await InventoryRepository.deduct_materials(
            dentist_id=req.dentist_id,
            patient_id=req.patient_id,
            appointment_id=req.appointment_id,
            materials=req.inventory_materials,
        )

        for item in inventory_result:
            if item["quantity"] <= item["threshold_low"]:
                event = make_stock_low_event(
                    item_name=item["name"],
                    quantity=float(item["quantity"]),
                    threshold=float(item["threshold_low"]),
                    item_id=str(item["id"]),
                )
                await ws_manager.broadcast(req.dentist_id, event)

    await AuditRepository.log(
        dentist_id=req.dentist_id,
        patient_id=req.patient_id,
        action="write_odontogram",
        resource_type="odontogram_records",
        resource_id=str(record["id"]),
        metadata={"tooth_fdi": req.tooth_fdi, "state": req.state},
    )

    return {
        "odontogram_record": record,
        "inventory_deducted": inventory_result,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/odontogram/{patient_id}", tags=["Odontograma"])
async def get_odontogram(patient_id: str, dentist_id: str):
    await AuditRepository.log(
        dentist_id=dentist_id,
        patient_id=patient_id,
        action="read_odontogram",
        resource_type="odontogram_records",
    )
    chart = await OdontogramRepository.get_current_chart(patient_id)
    history = await OdontogramRepository.get_history(patient_id)
    return {"current_chart": chart, "history": history}


@app.get("/odontogram/{patient_id}/history", tags=["Odontograma"])
async def get_tooth_history(patient_id: str, tooth_fdi: Optional[int] = None):
    return await OdontogramRepository.get_history(patient_id, tooth_fdi)


# ════════════════════════════════════════════════════════════════
# APPOINTMENTS ENDPOINTS
# ════════════════════════════════════════════════════════════════

@app.get("/appointments/today", tags=["Citas"])
async def get_today_appointments(dentist_id: str):
    return await AppointmentRepository.get_today(dentist_id)


@app.patch("/appointments/{appt_id}/urgency", tags=["Citas"])
async def update_urgency(appt_id: str, level: str, reason: str = ""):
    updated = await AppointmentRepository.set_urgency(appt_id, level, reason)
    if not updated:
        raise HTTPException(status_code=404, detail="Cita no encontrada")
    return updated


# ════════════════════════════════════════════════════════════════
# PATIENT PORTAL
# ════════════════════════════════════════════════════════════════

@app.get("/portal/{portal_code}", tags=["Portal Paciente"])
async def get_portal_data(portal_code: str):
    data = await PatientRepository.get_portal_data(portal_code)
    if not data:
        raise HTTPException(status_code=404, detail="Portal no encontrado. Verifica el QR.")
    return data


# ════════════════════════════════════════════════════════════════
# SECURITY ENDPOINTS
# ════════════════════════════════════════════════════════════════

class PatientSensitiveRequest(BaseModel):
    dentist_id: str
    patient_id: str
    salt_hex: str
    medical_history: str = ""
    clinical_notes:  str = ""


@app.post("/security/encrypt-patient-fields", tags=["Seguridad"])
async def encrypt_fields(req: PatientSensitiveRequest):
    salt = bytes.fromhex(req.salt_hex)
    encrypted = encrypt_patient_sensitive(
        dentist_id=req.dentist_id,
        salt=salt,
        medical_history=req.medical_history,
        clinical_notes=req.clinical_notes,
    )
    await AuditRepository.log(
        dentist_id=req.dentist_id,
        patient_id=req.patient_id,
        action="encrypt_patient_fields",
        resource_type="patients",
        resource_id=req.patient_id,
    )
    return {
        "medical_history_enc": encrypted.medical_history_enc.hex(),
        "clinical_notes_enc":  encrypted.clinical_notes_enc.hex(),
        "algorithm": "AES-256-GCM",
        "kdf": "PBKDF2-HMAC-SHA256",
        "iterations": 600000,
    }


class DecryptRequest(BaseModel):
    dentist_id: str
    patient_id: str
    salt_hex: str
    medical_history_enc_hex: Optional[str] = None
    clinical_notes_enc_hex:  Optional[str] = None


@app.post("/security/decrypt-patient-fields", tags=["Seguridad"])
async def decrypt_fields(req: DecryptRequest, request: Request):
    salt = bytes.fromhex(req.salt_hex)
    med_enc  = bytes.fromhex(req.medical_history_enc_hex)  if req.medical_history_enc_hex  else None
    note_enc = bytes.fromhex(req.clinical_notes_enc_hex)   if req.clinical_notes_enc_hex   else None

    try:
        decrypted = decrypt_patient_sensitive(
            dentist_id=req.dentist_id,
            salt=salt,
            medical_history_enc=med_enc,
            clinical_notes_enc=note_enc,
        )
    except Exception as e:
        await AuditRepository.log(
            dentist_id=req.dentist_id,
            patient_id=req.patient_id,
            action="decrypt_patient_fields",
            resource_type="patients",
            ip_address=request.client.host if request.client else None,
            success=False,
            metadata={"error": str(e)},
        )
        raise HTTPException(status_code=403, detail="Descifrado fallido — sesión inválida o datos corruptos")

    await AuditRepository.log(
        dentist_id=req.dentist_id,
        patient_id=req.patient_id,
        action="decrypt_patient_fields",
        resource_type="patients",
        resource_id=req.patient_id,
        ip_address=request.client.host if request.client else None,
        metadata={"fields": ["medical_history", "clinical_notes"]},
    )
    return decrypted


@app.delete("/security/evict-key/{dentist_id}", tags=["Seguridad"])
async def evict_key(dentist_id: str):
    evict_dentist_key(dentist_id)
    return {"status": "key_evicted", "dentist_id": dentist_id}


@app.get("/security/audit-log", tags=["Seguridad"])
async def get_audit_log(
    dentist_id: str,
    patient_id: Optional[str] = None,
    limit: int = 50,
):
    from db.database import get_pool
    pool = await get_pool()
    async with pool.acquire() as conn:
        if patient_id:
            rows = await conn.fetch(
                "SELECT * FROM audit_log WHERE dentist_id=$1 AND patient_id=$2 ORDER BY created_at DESC LIMIT $3",
                dentist_id, patient_id, limit,
            )
        else:
            rows = await conn.fetch(
                "SELECT * FROM audit_log WHERE dentist_id=$1 ORDER BY created_at DESC LIMIT $2",
                dentist_id, limit,
            )
    return [dict(r) for r in rows]


# ════════════════════════════════════════════════════════════════
# HEALTH CHECK
# ════════════════════════════════════════════════════════════════

@app.get("/health", tags=["Sistema"])
async def health_check():
    from db.database import get_pool
    db_ok = False
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
        db_ok = True
    except Exception:
        pass

    return {
        "status": "healthy" if db_ok else "degraded",
        "version": "2.0.0",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "services": {
            "database":   "✅ PostgreSQL" if db_ok else "❌ Offline",
            "websockets": f"✅ {sum(len(v) for v in ws_manager._connections.values())} conexiones activas",
            "encryption": "✅ AES-256-GCM",
            "whatsapp":   "✅ Webhook registrado" if os.environ.get("WHATSAPP_ACCESS_TOKEN") else "⚠️  Token no configurado",
        },
        "compliance": ["LFPDPPP", "HIPAA", "NOM-004-SSA3-2012"],
    }


@app.get("/", tags=["Sistema"])
async def root():
    return {
        "name": "DentalPro PMS API",
        "version": "2.0.0",
        "docs": "/docs",
        "health": "/health",
        "websocket": "wss://api.dentalpro.mx/ws/dashboard/{dentist_id}",
    }
EOF}
cat > ~/Downloads/dentalpro-backend/main_v2.py << 'EOF'
"""
DentalPro PMS — Servidor Principal v2.0
========================================
Integra todos los módulos:
    • Auth (Google OAuth + JWT)
    • Agente de Recepción (Claude API)
    • Inventario Híbrido
    • Base de datos PostgreSQL (asyncpg)
    • WebSockets en tiempo real
    • Webhooks de WhatsApp (Meta API)
    • Cifrado AES-256-GCM (LFPDPPP / HIPAA)
    • Odontograma sincronizado con DB + Inventario
    • Portal del Paciente en tiempo real

Correr en desarrollo:
    uvicorn main:app --reload --port 8000

Correr en producción:
    gunicorn main:app -w 4 -k uvicorn.workers.UvicornWorker --bind 0.0.0.0:8000

Docs interactivas:
    http://localhost:8000/docs
"""

import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from fastapi import FastAPI, HTTPException, Request, Depends, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

# ── Módulos originales ─────────────────────────────────────────
from agents.reception_agent import router as agent_router
from inventory.hybrid_inventory import router as inventory_router
from auth.google_oauth import router as auth_router

# ── Módulos nuevos ─────────────────────────────────────────────
from db.database import (
    run_migrations,
    close_pool,
    OdontogramRepository,
    InventoryRepository,
    AppointmentRepository,
    PatientRepository,
    AuditRepository,
)
from websockets.ws_manager import ws_router, ws_manager, make_stock_low_event
from webhooks.whatsapp_webhook import router as webhook_router
from security.encryption import (
    encrypt_patient_sensitive,
    decrypt_patient_sensitive,
    hash_document,
    evict_dentist_key,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("dentalpro.main")


# ════════════════════════════════════════════════════════════════
# LIFESPAN — startup / shutdown
# ════════════════════════════════════════════════════════════════

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("🦷 DentalPro PMS arrancando…")
    await run_migrations()
    logger.info("✅ DB migrations completadas")
    yield
    await close_pool()
    logger.info("🔴 DentalPro PMS detenido")


# ════════════════════════════════════════════════════════════════
# APP
# ════════════════════════════════════════════════════════════════

app = FastAPI(
    title="🦷 DentalPro PMS — API v2",
    description="""
## DentalPro — Sistema de Gestión Dental Profesional

Backend completo con sincronización PostgreSQL, WebSockets en tiempo real,
webhooks WhatsApp y cifrado AES-256-GCM.

### Módulos

| Módulo | Prefix | Descripción |
|--------|--------|-------------|
| 🔐 Auth | `/auth` | Google OAuth 2.0, JWT, onboarding |
| 🤖 Agente IA | `/agent` | Triage de urgencia con Claude API |
| 📦 Inventario | `/inventory` | Stock híbrido con edición pre-confirmación |
| 🦷 Odontograma | `/odontogram` | FDI sync con PostgreSQL + inventario |
| 📅 Citas | `/appointments` | CRUD con urgency_level del agente |
| 👤 Portal | `/portal` | Vista del paciente en tiempo real |
| 💬 WhatsApp | `/webhooks` | Meta API webhook + bot engine |
| 🔔 WebSockets | `/ws` | Alertas en tiempo real al dashboard |
| 🔒 Seguridad | `/security` | Cifrado, auditoría, LFPDPPP |

### Variables de entorno requeridas
```
DATABASE_URL=postgresql://user:pass@localhost:5432/dentalpro
ANTHROPIC_API_KEY=sk-ant-...
GOOGLE_CLIENT_ID=...
GOOGLE_CLIENT_SECRET=...
JWT_SECRET_KEY=...
DENTALPRO_MASTER_KEY=<32 bytes hex>
WHATSAPP_VERIFY_TOKEN=...
WHATSAPP_APP_SECRET=...
WHATSAPP_ACCESS_TOKEN=...
```
""",
    version="2.0.0",
    lifespan=lifespan,
)

# ── Middlewares ────────────────────────────────────────────────
app.add_middleware(GZipMiddleware, minimum_size=1000)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://localhost:5173",
        os.environ.get("FRONTEND_URL", "https://app.dentalpro.mx"),
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Routers ────────────────────────────────────────────────────
app.include_router(auth_router)
app.include_router(agent_router)
app.include_router(inventory_router)
app.include_router(ws_router)
app.include_router(webhook_router)


# ════════════════════════════════════════════════════════════════
# ODONTOGRAM ENDPOINTS — sincronizados con PostgreSQL
# ════════════════════════════════════════════════════════════════

class ToothUpdateRequest(BaseModel):
    patient_id: str
    dentist_id: str
    tooth_fdi: int
    state: str
    previous_state: Optional[str] = None
    notes: Optional[str] = None
    appointment_id: Optional[str] = None
    source: str = "dentist"
    ai_confidence: Optional[float] = None
    # Inventario
    deduct_inventory: bool = False
    inventory_materials: Optional[list[dict]] = None  # [{item_id, delta, reason}]


TREATMENT_MATERIALS: dict[str, list[str]] = {
    "resin":      ["composite_a2", "acid_etch", "adhesive", "anesthesia"],
    "endo":       ["anesthesia", "k_files", "hypochlorite", "gutta_percha"],
    "extraction": ["anesthesia", "gauze"],
    "crown":      ["anesthesia", "temp_cement", "retraction_cord"],
    "implant":    ["anesthesia", "suture", "gauze"],
}


@app.post("/odontogram/tooth", tags=["Odontograma"])
async def update_tooth(req: ToothUpdateRequest):
    record = await OdontogramRepository.upsert_tooth(
        patient_id=req.patient_id,
        dentist_id=req.dentist_id,
        tooth_fdi=req.tooth_fdi,
        state=req.state,
        previous_state=req.previous_state,
        notes=req.notes,
        appointment_id=req.appointment_id,
        source=req.source,
        ai_confidence=req.ai_confidence,
    )

    inventory_result = []

    if req.deduct_inventory and req.inventory_materials:
        inventory_result = await InventoryRepository.deduct_materials(
            dentist_id=req.dentist_id,
            patient_id=req.patient_id,
            appointment_id=req.appointment_id,
            materials=req.inventory_materials,
        )

        for item in inventory_result:
            if item["quantity"] <= item["threshold_low"]:
                event = make_stock_low_event(
                    item_name=item["name"],
                    quantity=float(item["quantity"]),
                    threshold=float(item["threshold_low"]),
                    item_id=str(item["id"]),
                )
                await ws_manager.broadcast(req.dentist_id, event)

    await AuditRepository.log(
        dentist_id=req.dentist_id,
        patient_id=req.patient_id,
        action="write_odontogram",
        resource_type="odontogram_records",
        resource_id=str(record["id"]),
        metadata={"tooth_fdi": req.tooth_fdi, "state": req.state},
    )

    return {
        "odontogram_record": record,
        "inventory_deducted": inventory_result,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/odontogram/{patient_id}", tags=["Odontograma"])
async def get_odontogram(patient_id: str, dentist_id: str):
    await AuditRepository.log(
        dentist_id=dentist_id,
        patient_id=patient_id,
        action="read_odontogram",
        resource_type="odontogram_records",
    )
    chart = await OdontogramRepository.get_current_chart(patient_id)
    history = await OdontogramRepository.get_history(patient_id)
    return {"current_chart": chart, "history": history}


@app.get("/odontogram/{patient_id}/history", tags=["Odontograma"])
async def get_tooth_history(patient_id: str, tooth_fdi: Optional[int] = None):
    return await OdontogramRepository.get_history(patient_id, tooth_fdi)


# ════════════════════════════════════════════════════════════════
# APPOINTMENTS ENDPOINTS
# ════════════════════════════════════════════════════════════════

@app.get("/appointments/today", tags=["Citas"])
async def get_today_appointments(dentist_id: str):
    return await AppointmentRepository.get_today(dentist_id)


@app.patch("/appointments/{appt_id}/urgency", tags=["Citas"])
async def update_urgency(appt_id: str, level: str, reason: str = ""):
    updated = await AppointmentRepository.set_urgency(appt_id, level, reason)
    if not updated:
        raise HTTPException(status_code=404, detail="Cita no encontrada")
    return updated


# ════════════════════════════════════════════════════════════════
# PATIENT PORTAL
# ════════════════════════════════════════════════════════════════

@app.get("/portal/{portal_code}", tags=["Portal Paciente"])
async def get_portal_data(portal_code: str):
    data = await PatientRepository.get_portal_data(portal_code)
    if not data:
        raise HTTPException(status_code=404, detail="Portal no encontrado. Verifica el QR.")
    return data


# ════════════════════════════════════════════════════════════════
# SECURITY ENDPOINTS
# ════════════════════════════════════════════════════════════════

class PatientSensitiveRequest(BaseModel):
    dentist_id: str
    patient_id: str
    salt_hex: str
    medical_history: str = ""
    clinical_notes:  str = ""


@app.post("/security/encrypt-patient-fields", tags=["Seguridad"])
async def encrypt_fields(req: PatientSensitiveRequest):
    salt = bytes.fromhex(req.salt_hex)
    encrypted = encrypt_patient_sensitive(
        dentist_id=req.dentist_id,
        salt=salt,
        medical_history=req.medical_history,
        clinical_notes=req.clinical_notes,
    )
    await AuditRepository.log(
        dentist_id=req.dentist_id,
        patient_id=req.patient_id,
        action="encrypt_patient_fields",
        resource_type="patients",
        resource_id=req.patient_id,
    )
    return {
        "medical_history_enc": encrypted.medical_history_enc.hex(),
        "clinical_notes_enc":  encrypted.clinical_notes_enc.hex(),
        "algorithm": "AES-256-GCM",
        "kdf": "PBKDF2-HMAC-SHA256",
        "iterations": 600000,
    }


class DecryptRequest(BaseModel):
    dentist_id: str
    patient_id: str
    salt_hex: str
    medical_history_enc_hex: Optional[str] = None
    clinical_notes_enc_hex:  Optional[str] = None


@app.post("/security/decrypt-patient-fields", tags=["Seguridad"])
async def decrypt_fields(req: DecryptRequest, request: Request):
    salt = bytes.fromhex(req.salt_hex)
    med_enc  = bytes.fromhex(req.medical_history_enc_hex)  if req.medical_history_enc_hex  else None
    note_enc = bytes.fromhex(req.clinical_notes_enc_hex)   if req.clinical_notes_enc_hex   else None

    try:
        decrypted = decrypt_patient_sensitive(
            dentist_id=req.dentist_id,
            salt=salt,
            medical_history_enc=med_enc,
            clinical_notes_enc=note_enc,
        )
    except Exception as e:
        await AuditRepository.log(
            dentist_id=req.dentist_id,
            patient_id=req.patient_id,
            action="decrypt_patient_fields",
            resource_type="patients",
            ip_address=request.client.host if request.client else None,
            success=False,
            metadata={"error": str(e)},
        )
        raise HTTPException(status_code=403, detail="Descifrado fallido — sesión inválida o datos corruptos")

    await AuditRepository.log(
        dentist_id=req.dentist_id,
        patient_id=req.patient_id,
        action="decrypt_patient_fields",
        resource_type="patients",
        resource_id=req.patient_id,
        ip_address=request.client.host if request.client else None,
        metadata={"fields": ["medical_history", "clinical_notes"]},
    )
    return decrypted


@app.delete("/security/evict-key/{dentist_id}", tags=["Seguridad"])
async def evict_key(dentist_id: str):
    evict_dentist_key(dentist_id)
    return {"status": "key_evicted", "dentist_id": dentist_id}


@app.get("/security/audit-log", tags=["Seguridad"])
async def get_audit_log(
    dentist_id: str,
    patient_id: Optional[str] = None,
    limit: int = 50,
):
    from db.database import get_pool
    pool = await get_pool()
    async with pool.acquire() as conn:
        if patient_id:
            rows = await conn.fetch(
                "SELECT * FROM audit_log WHERE dentist_id=$1 AND patient_id=$2 ORDER BY created_at DESC LIMIT $3",
                dentist_id, patient_id, limit,
            )
        else:
            rows = await conn.fetch(
                "SELECT * FROM audit_log WHERE dentist_id=$1 ORDER BY created_at DESC LIMIT $2",
                dentist_id, limit,
            )
    return [dict(r) for r in rows]


# ════════════════════════════════════════════════════════════════
# HEALTH CHECK
# ════════════════════════════════════════════════════════════════

@app.get("/health", tags=["Sistema"])
async def health_check():
    from db.database import get_pool
    db_ok = False
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
        db_ok = True
    except Exception:
        pass

    return {
        "status": "healthy" if db_ok else "degraded",
        "version": "2.0.0",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "services": {
            "database":   "✅ PostgreSQL" if db_ok else "❌ Offline",
            "websockets": f"✅ {sum(len(v) for v in ws_manager._connections.values())} conexiones activas",
            "encryption": "✅ AES-256-GCM",
            "whatsapp":   "✅ Webhook registrado" if os.environ.get("WHATSAPP_ACCESS_TOKEN") else "⚠️  Token no configurado",
        },
        "compliance": ["LFPDPPP", "HIPAA", "NOM-004-SSA3-2012"],
    }


@app.get("/", tags=["Sistema"])
async def root():
    return {
        "name": "DentalPro PMS API",
        "version": "2.0.0",
        "docs": "/docs",
        "health": "/health",
        "websocket": "wss://api.dentalpro.mx/ws/dashboard/{dentist_id}",
    }
