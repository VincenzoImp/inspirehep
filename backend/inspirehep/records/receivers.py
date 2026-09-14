#
# Copyright (C) 2019 CERN.
#
# inspirehep is free software; you can redistribute it and/or modify it under
# the terms of the MIT License; see LICENSE file for more details.

import structlog
from flask_sqlalchemy import models_committed
from inspirehep.records.api.base import InspireRecord
from inspirehep.records.tasks import (
    redirect_references_to_merged_record,
)
from invenio_records.models import RecordMetadata
from sqlalchemy import event
from sqlalchemy.orm import Session

LOGGER = structlog.getLogger()


@event.listens_for(Session, "after_flush")
def collect_merged_records(session, flush_context):
    changes = {
        record.id: record not in session.deleted and "new_record" in (record.json or {})
        for record in session.new | session.dirty | session.deleted
        if isinstance(record, RecordMetadata)
    }
    if changes:
        transaction = session.get_nested_transaction() or session.get_transaction()
        pending = session.info.setdefault("merged_record_redirects", {})
        pending.setdefault(transaction, {}).update(changes)


@event.listens_for(Session, "after_commit")
def redirect_merged_records_after_commit(session):
    # Unlike models_committed, this also runs when a SAVEPOINT left no dirty models.
    transaction = session.get_nested_transaction() or session.get_transaction()
    pending = session.info.get("merged_record_redirects", {})
    changes = pending.pop(transaction, {})
    if transaction.parent is not None:
        if changes:
            pending.setdefault(transaction.parent, {}).update(changes)
        return

    session.info.pop("merged_record_redirects", None)
    for uuid, redirect in changes.items():
        if redirect:
            redirect_references_to_merged_record.delay(str(uuid))


@event.listens_for(Session, "after_transaction_end")
def discard_merged_records(session, transaction):
    # Committed changes have already been passed to the parent or dispatched.
    pending = session.info.get("merged_record_redirects", {})
    pending.pop(transaction, None)
    if transaction.parent is None:
        session.info.pop("merged_record_redirects", None)


@models_committed.connect
def index_after_commit(sender, changes):
    """Index a record in ES after it was committed to the DB.

    This cannot happen in an ``after_record_commit`` receiver from Invenio-Records
    because, despite the name, at that point we are not yet sure whether the record
    has been really committed to the DB.
    """
    for model_instance, change in changes:
        if isinstance(model_instance, RecordMetadata) and change in (
            "insert",
            "update",
            "delete",
        ):
            LOGGER.debug(
                "Record commited, indexing.",
                change=change,
                uuid=str(model_instance.id),
            )
            force_delete = change == "delete"
            InspireRecord(model_instance.json, model=model_instance).index(
                force_delete=force_delete
            )
