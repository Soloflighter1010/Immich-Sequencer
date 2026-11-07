"""OIDC authentication and request protection for the web application."""
import os
from urllib.parse import urlparse

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse
from authlib.integrations.starlette_client import OAuth

AUTH_DISABLED = os.getenv("AUTH_DISABLED", "false").strip().lower() in {"1", "true", "yes"}
OIDC_ISSUER_URL = os.getenv("OIDC_ISSUER_URL", "").rstrip("/")
OIDC_CLIENT_ID = os.getenv("OIDC_CLIENT_ID", "")
OIDC_CLIENT_SECRET = os.getenv("OIDC_CLIENT_SECRET", "")
OIDC_REDIRECT_URI = os.getenv("OIDC_REDIRECT_URI", "")
SESSION_SECRET = os.getenv("SESSION_SECRET", "")
OIDC_ALLOWED_EMAILS = {v.strip().lower() for v in os.getenv("OIDC_ALLOWED_EMAILS", "").split(",") if v.strip()}
OIDC_CONFIGURED = all((OIDC_ISSUER_URL, OIDC_CLIENT_ID, OIDC_CLIENT_SECRET, SESSION_SECRET))

oauth = OAuth()
if OIDC_CONFIGURED:
    issuer = urlparse(OIDC_ISSUER_URL)
    if issuer.scheme not in {"http", "https"} or not issuer.netloc:
        raise RuntimeError("OIDC_ISSUER_URL must be an absolute HTTP(S) URL")
    oauth.register(
        name="immich_oidc",
        client_id=OIDC_CLIENT_ID,
        client_secret=OIDC_CLIENT_SECRET,
        server_metadata_url=f"{OIDC_ISSUER_URL}/.well-known/openid-configuration",
        client_kwargs={"scope": "openid profile email"},
    )

class AuthenticationMiddleware(BaseHTTPMiddleware):
    """Fail closed unless OIDC is configured or auth is explicitly disabled."""
    PUBLIC_PATHS = {"/api/auth/login", "/api/auth/callback", "/api/auth/logout", "/api/auth/status"}

    async def dispatch(self, request, call_next):
        if AUTH_DISABLED or request.url.path in self.PUBLIC_PATHS:
            return await call_next(request)
        if not OIDC_CONFIGURED:
            message = ("Authentication is not configured. Set OIDC_ISSUER_URL, OIDC_CLIENT_ID, OIDC_CLIENT_SECRET, and SESSION_SECRET. For isolated local development only, set AUTH_DISABLED=true.")
            if request.url.path.startswith("/api/"):
                return JSONResponse({"error": message}, status_code=503)
            return HTMLResponse(message, status_code=503)
        if not request.session.get("user"):
            if request.url.path.startswith("/api/"):
                return JSONResponse({"error": "Authentication required."}, status_code=401)
            return RedirectResponse("/api/auth/login", status_code=303)
        return await call_next(request)
