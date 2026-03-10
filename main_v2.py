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
