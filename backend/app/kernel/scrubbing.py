"""PII and clinical-data scrubbing for logs and error reports.

🔒 NFR-033 — clinical data, client identifiers and credentials MUST NOT appear
in application logs, error reports, traces or analytics.

This is the most easily violated rule in the entire design, and the most
damaging: a clinical value in a log file is a data breach that database
encryption does not prevent, and log aggregation is routinely the least
protected component in a stack.

The defence is structural rather than advisory:

* Fields are scrubbed by **key name**, so a developer who logs
  ``{"weight_kg": 82}`` gets ``[redacted]`` rather than a breach.
* Free text is scrubbed by **pattern**, catching values interpolated into
  messages where no key exists.
* An allowlist governs ``metadata`` on audit entries (DB §15.3), because
  denylists always miss something.

⚠️ This module is a safety net, not permission to be careless. The primary rule
stands: log identifiers, never values.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any, Final

REDACTED: Final = "[redacted]"
_MAX_DEPTH: Final = 6
_MAX_STRING: Final = 2048

# ─── Key-based scrubbing ─────────────────────────────────────────────────
# Matched case-insensitively against a *normalised* key (non-alphanumerics
# stripped), so `client_name`, `clientName` and `CLIENT-NAME` all match.

_CREDENTIAL_KEYS: Final[frozenset[str]] = frozenset(
    {
        "password", "passwd", "secret", "token", "accesstoken", "refreshtoken",
        "apikey", "authorization", "auth", "credential", "privatekey",
        "servicekey", "anonkey", "jwt", "bearer", "signature", "otp",
        "tokenhash", "magiclink", "sessiontoken", "csrf", "cookie",
    }
)

_IDENTITY_KEYS: Final[frozenset[str]] = frozenset(
    {
        "fullname", "firstname", "lastname", "name", "clientname",
        "email", "emailaddress", "mobile", "phone", "phonenumber", "whatsapp",
        "dateofbirth", "dob", "address", "city", "pincode", "postcode",
        "recipientaddress", "guardianname", "gstin", "ipaddress", "useragent",
    }
)

# 🔒 The clinical set. These are the values that make this product regulated.
_CLINICAL_KEYS: Final[frozenset[str]] = frozenset(
    {
        "weightkg", "weight", "heightcm", "height", "bmi", "bodyfatpct",
        "waistcm", "hipcm", "waisthipratio", "measurement", "measurements",
        "diagnosis", "condition", "conditions", "medication", "medications",
        "supplement", "supplements", "allergy", "allergies", "allergen",
        "labvalue", "labvalues", "biochemical", "hba1c", "tsh", "haemoglobin",
        "assessment", "answers", "response", "responses", "note", "notes",
        "consultationnote", "clinicalnote", "dietplan", "planitems",
        "mealitems", "foodlog", "adherence", "symptom", "symptoms",
        "menstrual", "pregnancy", "goal", "goals",
    }
)

_SENSITIVE_KEYS: Final[frozenset[str]] = (
    _CREDENTIAL_KEYS | _IDENTITY_KEYS | _CLINICAL_KEYS
)

# Keys that are safe and useful, even though a substring rule might catch them.
# 🔒 Identifiers only — an id references a record; it does not disclose content.
_ALWAYS_ALLOWED: Final[frozenset[str]] = frozenset(
    {
        "id", "tenantid", "clientid", "userid", "subjectid", "actorid",
        "requestid", "sessionid", "planid", "planversionid", "foodid",
        "appointmentid", "messageid", "jobid", "fileid", "generationid",
        "resourceid", "operatorid", "invoiceid", "templateid", "noticeid",
        "purposeid", "definitionid", "actortype", "realm", "role", "status",
        "state", "stage", "action", "outcome", "eventtype", "errortype",
        "endpoint", "method", "path", "count", "limit", "used", "duration",
        "durationms", "latencyms", "attempt", "attemptcount", "version",
        "schemaversion", "promptversion", "transport", "provider", "severity",
        "rulecode", "resource", "plancode", "upgradeto", "retryafterseconds",
    }
)


def _normalise_key(key: str) -> str:
    return re.sub(r"[^a-z0-9]", "", key.lower())


def is_sensitive_key(key: str) -> bool:
    """Whether a field name denotes data that must not be logged.

    Exact match after normalisation, then substring match. Substring matching
    catches ``client_weight_kg`` and ``practitioner_email``; the allowlist
    prevents it from swallowing ``client_id``.
    """
    norm = _normalise_key(key)
    if norm in _ALWAYS_ALLOWED:
        return False
    if norm in _SENSITIVE_KEYS:
        return True
    return any(sensitive in norm for sensitive in _SENSITIVE_KEYS)


# ─── Pattern-based scrubbing ─────────────────────────────────────────────
# For values interpolated into free text, where no key exists to match on.

_PATTERNS: Final[tuple[tuple[re.Pattern[str], str], ...]] = (
    # Indian mobile numbers, with or without +91.
    (re.compile(r"\b(?:\+?91[\s-]?)?[6-9]\d{9}\b"), "[mobile]"),
    # Email addresses.
    (re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"), "[email]"),
    # Bearer tokens and long opaque strings that look like credentials.
    (re.compile(r"\bBearer\s+[\w\-._~+/]+=*", re.IGNORECASE), "Bearer [redacted]"),
    (re.compile(r"\beyJ[\w\-._~+/]+=*"), "[jwt]"),
    # Indian PAN and Aadhaar-shaped numbers.
    (re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b"), "[pan]"),
    (re.compile(r"\b\d{4}\s?\d{4}\s?\d{4}\b"), "[id-number]"),
    # Postgres connection strings, which carry credentials.
    (re.compile(r"postgres(?:ql)?(?:\+\w+)?://[^\s\"']+"), "[database-url]"),
)


def scrub_text(text: str) -> str:
    """Remove recognisable personal data from free text.

    ⚠️ Pattern matching is inherently incomplete — it cannot recognise a name or
    a diagnosis written in prose. It is a second line of defence behind
    key-based scrubbing, not a substitute for not logging values.
    """
    if not text:
        return text
    if len(text) > _MAX_STRING:
        text = text[:_MAX_STRING] + "…[truncated]"
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def scrub_value(value: Any, *, _depth: int = 0) -> Any:
    """Recursively scrub a structure destined for a log or error report.

    Depth-limited: a deeply nested or cyclic structure must not hang the logger.
    Failing to log is bad; hanging the request that produced it is worse.
    """
    if _depth > _MAX_DEPTH:
        return "[nested]"

    if value is None or isinstance(value, bool | int | float):
        return value

    if isinstance(value, str):
        return scrub_text(value)

    if isinstance(value, Mapping):
        return {
            str(k): (REDACTED if is_sensitive_key(str(k)) else scrub_value(v, _depth=_depth + 1))
            for k, v in value.items()
        }

    if isinstance(value, Sequence) and not isinstance(value, str | bytes):
        return [scrub_value(item, _depth=_depth + 1) for item in value[:50]]

    if isinstance(value, bytes):
        return f"[bytes:{len(value)}]"

    # Unknown type — its repr may embed anything, so scrub the string form.
    return scrub_text(repr(value))


def scrub_mapping(data: Mapping[str, Any]) -> dict[str, Any]:
    """Scrub a flat or nested mapping. The common entry point."""
    result = scrub_value(data)
    return result if isinstance(result, dict) else {"value": result}


# ─── Audit metadata allowlist ────────────────────────────────────────────


def filter_audit_metadata(
    metadata: Mapping[str, Any],
    allowed_keys: frozenset[str],
) -> dict[str, Any]:
    """Restrict audit metadata to an explicit allowlist.

    🔒 DB §15.3 — the single most likely way a clinical value reaches the audit
    log is an over-eager ``metadata`` dictionary.

    An **allowlist**, not a denylist: a denylist fails open on anything its
    author did not anticipate, which is precisely the failure mode that matters
    when the cost is a compliance breach.
    """
    return {
        key: scrub_value(value)
        for key, value in metadata.items()
        if key in allowed_keys
    }


# ─── Sentry integration ──────────────────────────────────────────────────


def sentry_before_send(event: dict[str, Any], _hint: dict[str, Any]) -> dict[str, Any] | None:
    """Scrub an event before it leaves the process.

    🔒 NFR-080 with NFR-033: errors are captured with enough context to
    diagnose, and no clinical data whatsoever.

    Combined with ``send_default_pii=False``, this is the second of two
    defences. Both are needed: the SDK flag stops automatic collection, this
    stops what our own code attached.
    """
    if request := event.get("request"):
        request.pop("cookies", None)
        request.pop("data", None)  # 🔒 Request bodies may carry clinical data.
        if headers := request.get("headers"):
            request["headers"] = {
                k: (REDACTED if is_sensitive_key(k) else scrub_text(str(v)))
                for k, v in headers.items()
            }
        if query := request.get("query_string"):
            request["query_string"] = scrub_text(str(query))

    if extra := event.get("extra"):
        event["extra"] = scrub_mapping(extra)

    if tags := event.get("tags"):
        event["tags"] = {
            k: (REDACTED if is_sensitive_key(k) else scrub_text(str(v)))
            for k, v in tags.items()
        }

    # 🔒 Keep only the actor identifier — never name, email or IP.
    if user := event.get("user"):
        event["user"] = {"id": user.get("id")} if user.get("id") else None

    for entry in event.get("breadcrumbs", {}).get("values", []):
        if isinstance(entry, dict):
            if "message" in entry:
                entry["message"] = scrub_text(str(entry["message"]))
            if "data" in entry:
                entry["data"] = scrub_value(entry["data"])

    # Exception messages routinely echo the input that caused them.
    for exc in event.get("exception", {}).get("values", []):
        if isinstance(exc, dict) and "value" in exc:
            exc["value"] = scrub_text(str(exc["value"]))

    if "message" in event:
        event["message"] = scrub_text(str(event["message"]))

    return event
