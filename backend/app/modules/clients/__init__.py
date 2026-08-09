"""The `clients` module — the client spine.

🔒 DB §5. **Owner of** ``clients``, ``client_stage_history``, ``client_notes``,
``tags``, ``client_tags`` and ``client_assignments``. **Writers:** this module
only. Every other module reads client identity and stage through
``kernel.clients.ClientDirectory`` (Arch R3/R6).

⚠️ This package exposes its public surface here, in ``__init__.py``, because R2
forbids importing a module's internals: ``from app.modules.clients import
service`` is a boundary violation, and the checker reports it as one. Anything
another layer needs is re-exported below.

⚠️ **This module must not import ``app.platform``** (Arch R5). It has no session
factory, no logger and no settings of its own — those arrive as arguments from
the entry point that wires it. That is what keeps the domain logic testable
without the framework, and the framework replaceable without the domain.
"""

from __future__ import annotations

from app.modules.clients.access import ClientAccessView, load_for_access, load_grants
from app.modules.clients.actions import (
    CLIENT_ARCHIVE,
    CLIENT_CHANGE_STAGE,
    CLIENT_CREATE,
    CLIENT_MANAGE_ACCESS,
    CLIENT_MANAGE_TAGS,
    CLIENT_READ,
    CLIENT_READ_ACCESS,
    CLIENT_READ_NOTES,
    CLIENT_READ_TIMELINE,
    CLIENT_RESTORE,
    CLIENT_UPDATE,
    CLIENT_WRITE_NOTE,
    TAG_MANAGE,
    TAG_READ,
)
from app.modules.clients.assignments import (
    GrantRecord,
    grant_access,
    list_grants,
    reassign_owner,
    revoke_access,
)
from app.modules.clients.directory import ClientRepositoryDirectory
from app.modules.clients.models import (
    Client,
    ClientAssignment,
    ClientNote,
    ClientStageHistory,
    ClientTag,
    Tag,
    TimelineEvent,
)
from app.modules.clients.notes import (
    NoteAuthor,
    add_note,
    archive_note,
    edit_note,
    get_note,
    list_notes,
)
from app.modules.clients.queries import count_active_clients
from app.modules.clients.service import (
    UNSET,
    ClientCreate,
    ClientUpdate,
    Unset,
    create_client,
    get_client,
    update_client,
)
from app.modules.clients.tags import (
    archive_tag,
    attach_tag,
    create_tag,
    detach_tag,
    get_tag,
    list_client_tags,
    list_tags,
)
from app.modules.clients.timeline import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    TimelineCursor,
    TimelinePage,
    read_timeline,
    record,
    register_subscribers,
)
from app.modules.clients.transitions import (
    MAX_REASON_LENGTH,
    archive,
    change_stage,
    restore,
)

__all__ = [
    "CLIENT_ARCHIVE",
    "CLIENT_CHANGE_STAGE",
    "CLIENT_CREATE",
    "CLIENT_MANAGE_ACCESS",
    "CLIENT_MANAGE_TAGS",
    "CLIENT_READ",
    "CLIENT_READ_ACCESS",
    "CLIENT_READ_NOTES",
    "CLIENT_READ_TIMELINE",
    "CLIENT_RESTORE",
    "CLIENT_UPDATE",
    "CLIENT_WRITE_NOTE",
    "DEFAULT_LIMIT",
    "MAX_LIMIT",
    "MAX_REASON_LENGTH",
    "TAG_MANAGE",
    "TAG_READ",
    "UNSET",
    "Client",
    "ClientAccessView",
    "ClientAssignment",
    "ClientCreate",
    "ClientNote",
    "ClientRepositoryDirectory",
    "ClientStageHistory",
    "ClientTag",
    "ClientUpdate",
    "GrantRecord",
    "NoteAuthor",
    "Tag",
    "TimelineCursor",
    "TimelineEvent",
    "TimelinePage",
    "Unset",
    "add_note",
    "archive",
    "archive_note",
    "archive_tag",
    "attach_tag",
    "change_stage",
    "count_active_clients",
    "create_client",
    "create_tag",
    "detach_tag",
    "edit_note",
    "get_client",
    "get_note",
    "get_tag",
    "grant_access",
    "list_client_tags",
    "list_grants",
    "list_notes",
    "list_tags",
    "load_for_access",
    "load_grants",
    "read_timeline",
    "reassign_owner",
    "record",
    "register_subscribers",
    "restore",
    "revoke_access",
    "update_client",
]
