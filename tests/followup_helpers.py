from datetime import UTC, datetime

from src.domain import ThreadMessage, ThreadSnapshot

ME = "me@example.com"
MINE = frozenset({ME, "alias@example.com"})
ACTIVATED = datetime(2026, 10, 1, tzinfo=UTC)


def message(
    id_,
    sender=ME,
    to=("jean@example.com",),
    cc=(),
    day=5,
    from_me=None,
    automated=False,
    bounce=False,
    subject="Devis",
):
    return ThreadMessage(
        id=id_,
        sender=sender,
        to=tuple(to),
        cc=tuple(cc),
        sent_at=datetime(2026, 10, day, 9, 0, tzinfo=UTC),
        subject=subject,
        message_id_header=f"<{id_}@x>",
        from_me=sender in MINE if from_me is None else from_me,
        automated=automated,
        bounce=bounce,
    )


def snapshot(*messages, thread_id="t1", history_id="1"):
    return ThreadSnapshot(thread_id, history_id, tuple(messages))
