"""Ask TextIt to start one flow on one contact. One attempt, short timeout, no retry.

"Accepted" means TextIt answered 201 to the start. TextIt adds the contact to the
flow asynchronously, so a 201 does not prove the flow ran; only the test email
arriving proves that.
"""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from typing import Tuple

API = "https://textit.com/api/v2/flow_starts.json"
TIMEOUT_SECONDS = 8


def start_flow(token: str, flow_uuid: str, contact_uuid: str, params: dict,
               timeout: float = TIMEOUT_SECONDS, opener=urllib.request.urlopen) -> Tuple[bool, str]:
    """Returns (accepted, reason). The reason never contains the token."""
    body = json.dumps({
        "flow": flow_uuid,
        "contacts": [contact_uuid],
        "restart_participants": True,
        "params": params,
    }).encode()
    req = urllib.request.Request(API, data=body, method="POST", headers={
        "Authorization": f"Token {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    })
    try:
        with opener(req, timeout=timeout) as resp:
            code = getattr(resp, "status", None) or resp.getcode()
            if code == 201:
                return True, "201"
            return False, f"HTTP {code}"
    except urllib.error.HTTPError as exc:
        reason = f"HTTP {exc.code}"
        if exc.code == 429:
            retry = exc.headers.get("Retry-After") if exc.headers else None
            if retry:
                reason += f" (TextIt asks to wait {retry} s)"
        else:
            try:
                detail = exc.read(200).decode("utf-8", "replace").strip()
            except Exception:  # noqa: BLE001
                detail = ""
            if detail:
                reason += f": {detail}"
        return False, reason.replace(token, "[redacted]") if token else reason
    except (socket.timeout, TimeoutError):
        return False, f"timeout after {timeout:g} s"
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, (socket.timeout, TimeoutError)):
            return False, f"timeout after {timeout:g} s"
        return False, f"network error: {type(exc.reason).__name__}"
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}"
