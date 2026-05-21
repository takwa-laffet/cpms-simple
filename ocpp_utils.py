import asyncio
import logging
from typing import Callable, Any

LOGGER = logging.getLogger("cpms.ocpp_utils")


async def call_with_timeout(cp_instance: Any, request: Any, timeout: float = 10.0) -> Any:
    """Call cp_instance.call(request) with a timeout and unified error handling."""
    try:
        return await asyncio.wait_for(cp_instance.call(request), timeout=timeout)
    except asyncio.TimeoutError:
        LOGGER.warning("OCPP call timed out for %s", getattr(cp_instance, "id", "<cp>"))
        raise
    except Exception:
        LOGGER.exception("OCPP call failed for %s", getattr(cp_instance, "id", "<cp>"))
        raise


async def safe_call_retry(cp_id: str, cp_instance_getter: Callable[[str], Any], request_factory: Callable[[bool], Any], retries: int = 1, include_connector: bool = True, timeout: float = 10.0) -> dict:
    """Generalized retry helper for calls that may require retrying without connector_id.

    - `cp_instance_getter(cp_id)` should return the cp_instance or None.
    - `request_factory(include_connector)` should return an ocpp call object.
    Returns a dict with response object(s) and metadata.
    """
    cp_instance = await cp_instance_getter(cp_id)
    if cp_instance is None:
        return {"error": "not_connected"}

    try:
        req = request_factory(include_connector)
        resp = await call_with_timeout(cp_instance, req, timeout=timeout)
        return {"response": resp, "fallback_used": False}
    except Exception as e:
        # try fallback once if requested
        if include_connector and retries > 0:
            try:
                req2 = request_factory(False)
                resp2 = await call_with_timeout(cp_instance, req2, timeout=timeout)
                return {"response": resp2, "fallback_used": True}
            except Exception:
                LOGGER.exception("Fallback call also failed for %s", cp_id)
                return {"error": "call_failed", "exception": str(e)}
        return {"error": "call_failed", "exception": str(e)}
