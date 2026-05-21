from fastapi import Header, HTTPException, status


def admin_auth_dependency(x_admin_token: str | None = Header(None)):
    """Simple header-based admin auth. Replace with real auth in production."""
    if not x_admin_token or x_admin_token != "changeme-admin-token":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")
    return True
