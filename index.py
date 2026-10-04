"""Vercel FastAPI entry, only for the clean prepared deployment directory."""
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent / "src"))
from ashare_research.auction_web import app_from_env

# No local production paths or credentials are embedded into the deployment.
try:
    app=app_from_env()
except Exception:
    raise RuntimeError("CONFIG_INVALID: Configure cloud storage, auth and distributed controls") from None
