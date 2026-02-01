"""
Biluppgifter API Server
Kör: uvicorn server:app --port 3456
Docs: http://localhost:3456/docs
"""

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from biluppgifter import BiluppgifterClient

app = FastAPI(title="Biluppgifter API", version="2.0.0")

# Enable CORS for local and production
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Allow all origins for ngrok
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
client = BiluppgifterClient()


def _handle(fn, *args):
    try:
        return fn(*args)
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except ConnectionError as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.get("/api/vehicle/{regnr}")
def get_vehicle(regnr: str):
    """Hämta all fordonsdata (teknisk data, status, skatt, besiktning)."""
    return _handle(client.lookup, regnr)


@app.get("/api/owner/{regnr}")
def get_owner(regnr: str):
    """Hämta ägarprofil via regnr (namn, adress, personnummer, fordon)."""
    return _handle(client.lookup_owner_by_regnr, regnr)


@app.get("/api/profile/{profile_id}")
def get_profile(profile_id: str):
    """Hämta ägarprofil direkt med profil-ID."""
    return _handle(client.lookup_owner_profile, profile_id)


@app.get("/api/address/{regnr}")
def get_address_vehicles(regnr: str):
    """Hämta alla fordon registrerade på ägarens adress."""
    return _handle(client.lookup_address_vehicles, regnr)


@app.get("/health")
def health():
    return {"status": "ok"}
