from collections import Counter
from collections.abc import Iterable

from src.domain import DecisionRecord
from src.evaluation.corpus import SUBJECT_CHARS
from src.gmail.text_cleaning import extract_domain
from src.workflow import attention_reasons

NOTHING_ASKED = (
    "No decision carries an answer to the attention questions over this period: set "
    "ATTENTION_MODE=shadow, then come back after a few days."
)


def format_attention_report(records: Iterable[DecisionRecord], threshold: float) -> str:
    """What the attention questions would put forward, read from the answers already stored."""
    asked = [record for record in records if record.signals]
    if not asked:
        return NOTHING_ASKED
    days = len({record.created_at[:10] for record in asked})
    flagged = [
        (record, reasons)
        for record in asked
        if (reasons := attention_reasons(record, threshold))
    ]
    # An alerted mail was already shown: only the others would be news.
    forward = [(record, reasons) for record, reasons in flagged if record.route != "llm"]
    archived = sum(1 for record, _ in forward if record.route == "reject")
    by_reason = Counter(reason for _, reasons in forward for reason in reasons)
    reasons_line = ", ".join(f"{reason} {count}" for reason, count in by_reason.most_common())
    forward_line = (
        f"Would be put forward at {threshold}: {len(forward)} "
        f"({len(forward) / days:.1f} per day), of which {archived} are archived today"
    )
    lines = [
        f"Mails asked the attention questions: {len(asked)} over {days} day(s)",
        forward_line,
        f"Also yes on {len(flagged) - len(forward)} mail(s) already alerted",
        f"By reason: {reasons_line or 'none'}",
    ]
    if forward:
        lines.append("")
    for record, reasons in forward:
        subject = " ".join(record.subject.split())[:SUBJECT_CHARS]
        lines.append(
            f"{record.created_at[:10]} {extract_domain(record.sender)} «{subject}» "
            f"[{', '.join(reasons)}; {record.route}]"
        )
    return "\n".join(lines)
