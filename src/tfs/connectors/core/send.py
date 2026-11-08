"""Call a send helper and keep uncertain outcomes and secrets out of retries."""

from tfs import password_store
from tfs.connectors.core import runner


def deliver(job: dict, manifest: dict, now: str) -> dict:
    """Run one approved delivery outside a transaction and return a safe outcome."""
    try:
        secrets = {
            ask["key"]: password_store.get(f"{job['connector']}:{ask['key']}")
            for ask in manifest["asks"]
            if ask["secret"]
        }
    except password_store.PasswordStoreError:
        return {
            "status": "needs_you",
            "message": "Unlock the password store and reconnect this app.",
            "auth_failed": True,
        }
    if None in secrets.values():
        return {
            "status": "needs_you",
            "auth_failed": True,
            "message": "Reconnect this app before sending.",
        }
    payload = {
        "item": job["item"],
        "op_id": job["id"],
        "remote_id": job["remote_id"],
        "remote_version": job["remote_version"],
        "in_doubt": job["in_doubt"],
        "secrets": secrets,
        "now": now,
    }
    try:
        result = runner.run(job["connector"], "send", payload, timeout_s=60)
    except (runner.RunnerTimeoutError, runner.RunnerFailedError, runner.ConnectorUserError):
        return {
            "status": "unknown",
            "message": "Delivery is uncertain. Check the app before trying again.",
        }
    return _outcome(result, list(secrets.values()))


def _safe_reference(value: object, secrets: list[str]) -> bool:
    return (
        isinstance(value, str)
        and bool(value.strip())
        and not any(s and s in value for s in secrets)
    )


def _outcome(result: dict, secrets: list[str]) -> dict:
    if result.get("unsupported") is True:
        return {"status": "needs_you", "message": "This app does not support plan delivery."}
    secrets = list(
        {value for secret in secrets if secret for value in (secret, repr(secret)[1:-1])}
    )
    status = result.get("status")
    if result.get("auth_failed") is True:
        return {
            "status": "needs_you",
            "auth_failed": True,
            "message": "Reconnect this app before sending.",
        }
    if status not in ("sent", "not_sent", "unknown", "conflict", "needs_you"):
        return {"status": "unknown", "message": "The app returned an invalid delivery result."}
    safe = {"status": status}
    if status == "sent":
        for field in ("remote_id", "remote_version"):
            value = result.get(field)
            if not _safe_reference(value, secrets):
                return {
                    "status": "unknown",
                    "message": "The app did not confirm a usable remote reference.",
                }
            safe[field] = value
    if (
        status == "needs_you"
        and result.get("manual_action") == "delete"
        and _safe_reference(result.get("remote_id"), secrets)
    ):
        safe.update(manual_action="delete", remote_id=result["remote_id"])
    if isinstance(result.get("message"), str):
        message = result["message"]
        for secret in sorted(filter(None, secrets), key=len, reverse=True):
            message = message.replace(secret, "[secret]").replace(repr(secret)[1:-1], "[secret]")
        safe["message"] = message[:500]
    return safe
