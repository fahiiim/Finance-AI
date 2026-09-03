"""
Cross-user data containment barrier.

THE PROBLEM THIS SOLVES
    Every backend call is scoped by `context.user_id` (see
    services/backend_client.py), but scoping a REQUEST is a statement of
    intent — it is not proof about the RESPONSE. If the backend has a bug, a
    cache mixes tenants, or a JOIN drops a WHERE clause, this service would
    happily format another user's finances into an answer and hand it to the
    wrong person. Asking correctly is not the same as being answered
    correctly.

    So we verify rather than trust. Records that declare an owner are checked
    against the requesting user at the single chokepoint every tool passes
    through (services/financial_service.py). A mismatch aborts the request.

FAIL-CLOSED, NOT FAIL-QUIET
    A breach raises CrossUserDataError and the user gets a generic error.
    We never filter out the offending records and answer with the rest: if
    the data source is returning foreign records, we do not know that what
    remains is trustworthy either.

ON `owner_user_id` BEING OPTIONAL
    The AI service cannot verify what the backend does not tell it. Records
    carry `owner_user_id` only if the backend populates it. When it is absent
    this check cannot run, which is why `strict_data_ownership` exists: turn
    it on once the backend emits ownership on every record, and unlabeled
    data becomes an error instead of a blind spot. Until then this is a
    genuine safety net for a labelled subset, not a complete guarantee —
    see docs/security.md.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import BaseModel

from app.core.config import settings
from app.core.exceptions import CrossUserDataError
from app.core.logging import get_logger, log_with_fields
from app.core.security import UserContext

logger = get_logger(__name__)

OWNER_FIELD = "owner_user_id"

#: Sources already reported as returning unlabeled records, so the warning is
#: emitted once per process rather than once per request.
_UNLABELED_SOURCES_SEEN: set[str] = set()


def _record_owner(value: Any) -> str | None:
    """Return a record's declared owner, or None if it does not declare one."""
    if isinstance(value, BaseModel):
        return getattr(value, OWNER_FIELD, None)
    if isinstance(value, Mapping):
        return value.get(OWNER_FIELD)
    return None


def _is_record(value: Any) -> bool:
    return isinstance(value, BaseModel) or isinstance(value, Mapping)


def _declares_ownership(value: Any) -> bool:
    """
    Whether this object is the KIND that should carry an owner.

    The distinction matters because _iter_records walks into nested value
    objects too: a Money has no owner_user_id field and never will, so
    counting it as "unlabeled" would make strict mode reject every response.
    An OwnedRecord subclass, by contrast, declares the field -- so the field
    being None means the backend did not populate it, which is exactly the
    condition strict mode exists to catch.
    """
    return isinstance(value, BaseModel) and OWNER_FIELD in type(value).model_fields


def _iter_records(value: Any):
    """
    Walk a backend result yielding every record-like object.

    Handles the shapes the backend client actually returns: a single model, a
    list of models, and models nesting other models (a summary containing
    Money, holdings containing accounts, and so on).
    """
    if isinstance(value, BaseModel):
        yield value
        for field_name in type(value).model_fields:
            yield from _iter_records(getattr(value, field_name, None))
    elif isinstance(value, Mapping):
        yield value
        for item in value.values():
            yield from _iter_records(item)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for item in value:
            yield from _iter_records(item)


def verify_ownership(result: Any, context: UserContext, *, source: str) -> Any:
    """
    Assert every owned record in `result` belongs to `context.user_id`.

    Returns `result` unchanged so this can wrap a call site transparently.
    Raises CrossUserDataError on the first foreign record found.
    """
    checked = 0
    unlabeled = 0

    for record in _iter_records(result):
        if not _is_record(record):
            continue
        owner = _record_owner(record)
        if owner is None:
            # Only count things that were SUPPOSED to declare an owner.
            # Nested value objects (Money, and anything else without the
            # field) are not evidence of a backend that forgot to label.
            if _declares_ownership(record):
                unlabeled += 1
            continue
        checked += 1
        if str(owner) != context.user_id:
            # Deliberately does NOT log the foreign user_id or any record
            # content — this lands in logs that are less protected than the
            # data itself. The alert is that it happened, not whose data it was.
            log_with_fields(
                logger,
                50,
                "cross_user_data_blocked",
                source=source,
                session_id=context.session_id,
            )
            raise CrossUserDataError("Data integrity check failed for this request.")

    # ANY unlabeled record is a failure in strict mode -- not just a response
    # where every record was unlabeled. The `checked == 0` test this replaces
    # meant a single labelled row vouched for every unlabelled one beside it,
    # which is precisely the shape of a partial backend rollout: some
    # endpoints emit owner_user_id, some do not, and the mixed response passed
    # silently. The strictest reading is the only useful one here, because the
    # whole point of the setting is to turn a blind spot into an error.
    if unlabeled and not settings.strict_data_ownership and source not in _UNLABELED_SOURCES_SEEN:
        # Reported ONCE per source per process, not per request: this is a
        # standing property of the backend, not an event, and logging it on
        # every call would bury it in its own volume.
        #
        # Without this there is no way to answer "does our backend label
        # ownership?" short of turning strict mode on in production and seeing
        # what breaks. That question has to be answerable BEFORE flipping the
        # flag, or the flag can only ever be flipped as a gamble.
        _UNLABELED_SOURCES_SEEN.add(source)
        log_with_fields(
            logger,
            30,
            "records_without_owner_user_id",
            source=source,
            detail=(
                "The backend returned records that do not declare owner_user_id, so the "
                "cross-user containment check could not verify them. Populate the field upstream, "
                "then set STRICT_DATA_OWNERSHIP=true to make this an error."
            ),
        )

    if settings.strict_data_ownership and unlabeled:
        log_with_fields(
            logger,
            40,
            "unverifiable_data_ownership",
            source=source,
            session_id=context.session_id,
            record_count=unlabeled,
        )
        raise CrossUserDataError("Data ownership could not be verified for this request.")

    return result
