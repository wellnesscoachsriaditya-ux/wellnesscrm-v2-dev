"""The `leads` module — lead capture and conversion.

🔒 DB §6. **Owner of** ``enquiry_forms`` and ``enquiry_submissions``.
**Writers:** this module only.

🔒 **There is no lead table, and that is M1.3's decision, not this module's.**
A lead is a ``clients`` row at stage ``lead``; this module owns the *enquiry* —
the raw submission and the form that produced it. Everything it needs from the
client spine arrives through two kernel ports (``ClientDirectory`` to match,
``ClientIntake`` to create), because R3 forbids importing ``clients`` at all.

⚠️ This package exposes its public surface here, in ``__init__.py``, because R2
forbids importing a module's internals: ``from app.modules.leads import intake``
is a boundary violation and the checker reports it as one. Anything another layer
needs is re-exported below.

⚠️ **This module must not import ``app.platform``** (R5). It has no session
factory, no settings and no logger of its own — those arrive as arguments from
the router that wires it. Two consequences worth naming, because both look like
omissions:

* The **consent ledger entry** is appended by the router, not here. The rules
  live in ``kernel.consent`` but the persistence lives in ``platform.consent``.
  :func:`submit` returns the ``mobile_hash`` and timestamp the caller needs, and
  the append happens in the same transaction (ADR-04).
* The **rate limit** is applied by the router, for the same reason.
"""

from __future__ import annotations

from app.modules.leads.actions import (
    ENQUIRY_FORM_READ,
    ENQUIRY_FORM_UPDATE,
    ENQUIRY_LIST,
    ENQUIRY_RESPOND,
)
from app.modules.leads.discovery import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    EnquiryCursor,
    EnquiryListItem,
    EnquiryPage,
    list_enquiries,
    load_submission_client,
)
from app.modules.leads.forms import (
    DEFAULT_FORM_TITLE,
    OwnForm,
    PublicForm,
    ensure_form,
    load_own_form,
    load_public_form,
    update_form,
)
from app.modules.leads.intake import (
    SpamRejectedError,
    SubmissionInput,
    SubmissionResult,
    submit,
)
from app.modules.leads.models import EnquiryForm, EnquirySubmission
from app.modules.leads.respond import mark_responded

__all__ = [
    "DEFAULT_FORM_TITLE",
    "DEFAULT_PAGE_SIZE",
    "ENQUIRY_FORM_READ",
    "ENQUIRY_FORM_UPDATE",
    "ENQUIRY_LIST",
    "ENQUIRY_RESPOND",
    "MAX_PAGE_SIZE",
    "EnquiryCursor",
    "EnquiryForm",
    "EnquiryListItem",
    "EnquiryPage",
    "EnquirySubmission",
    "OwnForm",
    "PublicForm",
    "SpamRejectedError",
    "SubmissionInput",
    "SubmissionResult",
    "ensure_form",
    "list_enquiries",
    "load_own_form",
    "load_public_form",
    "load_submission_client",
    "mark_responded",
    "submit",
    "update_form",
]
