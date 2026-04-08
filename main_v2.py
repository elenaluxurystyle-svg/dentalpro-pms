import os
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="DentalPro PMS API", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
async def root():
    return {"name": "DentalPro PMS API", "version": "2.0.0", "docs": "/docs", "status": "online"}

@app.get("/health")
async def health():
    return {"status": "healthy", "version": "2.0.0"}

# ── AUTH GOOGLE ──────────────────────────────────────────
from fastapi import Header
import base64 as b64

async def verify_google_token(authorization: str = Header(None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Token requerido")
    token = authorization.split(" ")[1]
    try:
        parts = token.split(".")
        if len(parts) == 3:
            padding = 4 - len(parts[1]) % 4
            import json as _json
            payload = _json.loads(b64.urlsafe_b64decode(parts[1] + "=" * padding))
            return payload
        raise HTTPException(status_code=401, detail="Token invalido")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=401, detail=str(e))

@app.get("/auth/verify")
async def auth_verify(authorization: str = Header(None)):
    user = await verify_google_token(authorization)
    return {"valid": True, "email": user.get("email"), "name": user.get("name")}
