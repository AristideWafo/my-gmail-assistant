import argparse
import sys
from datetime import UTC, datetime, timedelta

from src.bootstrap import MAIL_PROVIDERS, STORES, BuildContext
from src.config import Settings
from src.domain import EmailMessage
from src.evaluation.attention import format_attention_report
from src.evaluation.corpus import LabCase, cases_from_rated, cases_from_recent, load_corpus
from src.evaluation.dataset import build_dataset
from src.evaluation.lab import against_baseline, format_lab_report, planned_calls, run_variant
from src.evaluation.routing_audit import DETERMINISTIC_SOURCES, RULES, audit, format_audit
from src.evaluation.runner import NOT_CONCLUSIVE, evaluate, format_report, is_conclusive
from src.evaluation.sent import (
    DEFAULT_SAMPLES_PATH,
    find_samples,
    format_sent_report,
    judge_samples,
    label_samples,
    load_samples,
    pick_for_labelling,
    save_samples,
)
from src.evaluation.variants import QUESTION_VARIANTS, TRUNCATION_VARIANTS, VARIANTS
from src.ports import EmailClassifier
from src.triage.engine import JevClassifier
from src.triage.few_shot import build_examples
from src.triage.heuristic import HeuristicClassifier
from src.triage.rules import load_ruleset
from src.triage.sent_mail import JevSentMailJudge

CANDIDATE_WINDOW = timedelta(days=90)
DEFAULT_MAX_CALLS = 500
DEFAULT_RECENT_MAILS = 60
RECENT_NOTE = (
    "No verdict on these mails: \"correct\" means routed as a first pass of `current` routed "
    "them. The `current` row is therefore the disagreement between two identical calls, the "
    "noise every other row has to be read against."
)
SAMPLE_EMAIL = EmailMessage(
    id="check",
    thread_id="check",
    sender="newsletter@example.com",
    subject="Weekly digest",
    snippet="This week's articles",
    body="This week's articles",
    sender_domain="example.com",
)
SAMPLE_EXAMPLES = [
    {
        "sender_domain": "example.com",
        "subject": "Weekly digest",
        "wrong_urgency": "high",
        "wrong_category": "personnel",
        "correct_urgency": "low",
        "correct_category": "newsletter",
    }
]


def _variants(
    settings: Settings, examples: list[dict[str, str]], wanted: set[str]
) -> dict[str, EmailClassifier]:
    variants: dict[str, EmailClassifier] = {"heuristic": HeuristicClassifier()}
    jev = JevClassifier(settings.jev_api_url, settings.jev_api_key)
    if jev.is_configured:
        variants["jev"] = jev
        if examples:
            variants["jev+few-shot"] = JevClassifier(
                settings.jev_api_url, settings.jev_api_key, examples_provider=lambda: examples
            )
    return {name: variant for name, variant in variants.items() if not wanted or name in wanted}


def run(args: argparse.Namespace, settings: Settings) -> int:
    store = STORES[settings.store_backend](settings)
    try:
        mail = MAIL_PROVIDERS[settings.mail_provider](BuildContext(settings, store))
        rated = store.rated_decisions()
        if args.limit:
            rated = rated[-args.limit :]
        dataset = build_dataset(rated, mail, args.split)
    finally:
        store.close()
    examples = build_examples(reversed(dataset.training))
    rules = load_ruleset(settings.triage_rules_path)
    variants = _variants(settings, examples, set(args.variants.split(",")) - {""})
    reports = [
        evaluate(name, classifier, dataset.cases, settings.low_confidence_threshold, rules)
        for name, classifier in variants.items()
    ]
    print(format_report(reports, dataset.cases, dataset.missing, dataset.split_at))
    return 0


def _rated_lab_cases(
    args: argparse.Namespace, settings: Settings
) -> tuple[list[LabCase], list[str]]:
    store = STORES[settings.store_backend](settings)
    try:
        mail = MAIL_PROVIDERS[settings.mail_provider](BuildContext(settings, store))
        rated = store.rated_decisions()
        if args.limit:
            rated = rated[-args.limit :]
        # No split: question variants learn nothing from past verdicts. Full bodies, so that a
        # variant keeping the end of a long mail has an end to keep.
        dataset = build_dataset(rated, mail, split_at="", full_body=True)
    finally:
        store.close()
    cases, by_rule = cases_from_rated(dataset.cases, load_ruleset(settings.triage_rules_path))
    notes = [
        (
            f"Rated mails: {len(dataset.cases)}, of which {by_rule} decided by a rule and left "
            f"out; {dataset.missing} no longer available"
        )
    ]
    if not is_conclusive(dataset.cases):
        notes.append(NOT_CONCLUSIVE)
    return cases, notes


def _recent_lab_cases(args: argparse.Namespace, settings: Settings) -> list[LabCase]:
    store = STORES[settings.store_backend](settings)
    try:
        mail = MAIL_PROVIDERS[settings.mail_provider](BuildContext(settings, store))
        return cases_from_recent(
            store.decisions_since(timedelta(days=args.days)),
            lambda message_id: mail.fetch_message(message_id, full_body=True),
            load_ruleset(settings.triage_rules_path),
            args.min_words,
            args.limit or DEFAULT_RECENT_MAILS,
        )
    finally:
        store.close()


def lab(args: argparse.Namespace, settings: Settings) -> int:
    default = TRUNCATION_VARIANTS if args.source == "recent" else QUESTION_VARIANTS
    wanted = [name for name in args.variants.split(",") if name] or list(default)
    unknown = sorted(set(wanted) - set(VARIANTS))
    if unknown:
        print(f"Unknown variant(s) {unknown}; available: {sorted(VARIANTS)}")
        return 2
    classifier = JevClassifier(settings.jev_api_url, settings.jev_api_key)
    if not classifier.is_configured:
        print("JEV is not configured (JEV_API_URL / JEV_API_KEY).")
        return 2
    if args.source == "rated":
        cases, notes = _rated_lab_cases(args, settings)
    elif args.source == "recent":
        cases, notes = _recent_lab_cases(args, settings), [RECENT_NOTE]
    else:
        cases, notes = load_corpus()[: args.limit or None], []
    variants = [VARIANTS[name] for name in wanted]
    baseline_calls = len(cases) if args.source == "recent" else 0
    calls = planned_calls(variants, cases, args.repeats) + baseline_calls
    print(
        f"{calls} JEV call(s): {len(variants)} variant(s) x {len(cases)} mail(s) "
        f"x {args.repeats} run(s)" + (f" + {baseline_calls} for the baseline" if baseline_calls else "")
    )
    if calls > args.max_calls:
        print(
            f"Refused: more than --max-calls {args.max_calls}. "
            "Raise it, or use --limit / --variants."
        )
        return 2
    if args.source == "recent":
        cases, failed = against_baseline(
            VARIANTS["current"], cases, classifier, settings.low_confidence_threshold
        )
        if failed:
            notes.append(f"{failed} mail(s) left out: the baseline call failed")
    reports = [
        run_variant(variant, cases, classifier, settings.low_confidence_threshold, args.repeats)
        for variant in variants
    ]
    print("\n".join([format_lab_report(reports, len(cases)), "", *notes]).rstrip())
    return 0


def check_examples(_: argparse.Namespace, settings: Settings) -> int:
    classifier = JevClassifier(
        settings.jev_api_url, settings.jev_api_key, examples_provider=lambda: SAMPLE_EXAMPLES
    )
    if not classifier.is_configured:
        print("JEV is not configured (JEV_API_URL / JEV_API_KEY).")
        return 2
    try:
        result = classifier.classify(SAMPLE_EMAIL)
    except Exception as exc:  # noqa: BLE001 - the point is to report whatever the API does
        print(f"REJECTED: a request carrying state.examples failed with {type(exc).__name__}: {exc}")
        return 1
    print(
        "ACCEPTED: JEV answered a request carrying state.examples "
        f"(urgency={result.urgency}, category={result.category}, confidence={result.confidence:.2f}). "
        "This shows the payload is accepted, not that the examples change the answers: "
        "compare the jev and jev+few-shot rows of `run` for that."
    )
    return 0


def candidates(args: argparse.Namespace, settings: Settings) -> int:
    store = STORES[settings.store_backend](settings)
    try:
        found = store.rule_candidates(args.min_count, CANDIDATE_WINDOW)
    finally:
        store.close()
    if not found:
        print(f"No sender with at least {args.min_count} identical, uncorrected JEV decisions.")
        return 0
    for candidate in found:
        print(f"{candidate.count:4d}  {candidate.sender}  ->  {candidate.urgency} / {candidate.category}")
    return 0


def attention(args: argparse.Namespace, settings: Settings) -> int:
    store = STORES[settings.store_backend](settings)
    try:
        records = store.decisions_since(timedelta(days=args.days))
    finally:
        store.close()
    print(format_attention_report(records, settings.attention_threshold))
    return 0


def routing_rules(args: argparse.Namespace, settings: Settings) -> int:
    store = STORES[settings.store_backend](settings)
    try:
        records = store.decisions_since(timedelta(days=args.days))
        rated = store.rated_decisions()
    finally:
        store.close()
    audits = audit(RULES, records, rated, settings.low_confidence_threshold)
    decided = sum(1 for record in records if record.source not in DETERMINISTIC_SOURCES)
    print(format_audit(audits, decided, args.days))
    return 0


def sent_collect(args: argparse.Namespace, settings: Settings) -> int:
    judge = JevSentMailJudge(JevClassifier(settings.jev_api_url, settings.jev_api_key))
    if not judge.is_configured:
        print("JEV is not configured (JEV_API_URL / JEV_API_KEY).")
        return 2
    store = STORES[settings.store_backend](settings)
    try:
        mail = MAIL_PROVIDERS[settings.mail_provider](BuildContext(settings, store))
        samples = find_samples(
            mail,
            args.days,
            args.max_threads,
            settings.follow_up_after_days,
            settings.tzinfo,
            datetime.now(UTC),
        )
        print(f"{len(samples)} JEV call(s): one per mail you sent in the last {args.days} days")
        if len(samples) > args.max_calls:
            print(f"Refused: more than --max-calls {args.max_calls}. Raise it, or lower --days.")
            return 2
        failed = judge_samples(samples, mail, judge)
    finally:
        store.close()
    pick_for_labelling(samples)
    save_samples(args.path, samples)
    print(
        f"Saved to {args.path} ({failed} could not be judged). "
        "Next: `python -m src.evaluation sent-label`."
    )
    return 0


def sent_label(args: argparse.Namespace, settings: Settings) -> int:
    samples = load_samples(args.path)
    done = label_samples(samples, input, print, lambda: save_samples(args.path, samples))
    print(f"{done} mail(s) labelled. Next: `python -m src.evaluation sent-report`.")
    return 0


def sent_report(args: argparse.Namespace, settings: Settings) -> int:
    print(format_sent_report(load_samples(args.path)))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m src.evaluation", description="Offline evaluation of the triage on rated mails"
    )
    commands = parser.add_subparsers(dest="command", required=True)

    run_parser = commands.add_parser("run", help="replay rated mails through each classifier variant")
    run_parser.add_argument("--limit", type=int, default=0, help="only the N most recent verdicts")
    run_parser.add_argument(
        "--split", default=None, help="ISO timestamp: learn from corrections before it, score after"
    )
    run_parser.add_argument("--variants", default="", help="comma-separated subset to run")
    run_parser.set_defaults(handler=run)

    check_parser = commands.add_parser(
        "check-examples", help="one live JEV call carrying state.examples"
    )
    check_parser.set_defaults(handler=check_examples)

    candidates_parser = commands.add_parser(
        "candidates", help="senders JEV always classifies the same way: candidates for a rule"
    )
    candidates_parser.add_argument("--min-count", type=int, default=5)
    candidates_parser.set_defaults(handler=candidates)

    lab_parser = commands.add_parser(
        "lab", help="compare question variants on live JEV calls, on test mails or rated mails"
    )
    lab_parser.add_argument(
        "--source",
        choices=("corpus", "rated", "recent"),
        default="corpus",
        help="corpus: invented test mails; rated: the real mails you gave a verdict on; "
        "recent: the latest long mails, compared with what `current` does with them",
    )
    lab_parser.add_argument(
        "--variants", default="", help=f"comma-separated subset of {sorted(VARIANTS)}"
    )
    lab_parser.add_argument(
        "--repeats", type=int, default=1, help="runs per mail, to see instability"
    )
    lab_parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="only the first N test mails, the N latest verdicts, or the N latest long mails "
        f"(recent: {DEFAULT_RECENT_MAILS} by default)",
    )
    lab_parser.add_argument(
        "--days", type=int, default=30, help="recent: how far back to look for mails"
    )
    lab_parser.add_argument(
        "--min-words",
        type=int,
        default=300,
        help="recent: only mails whose body is longer than this, where a cut can matter",
    )
    lab_parser.add_argument(
        "--max-calls",
        type=int,
        default=DEFAULT_MAX_CALLS,
        help="refuse to run beyond this many JEV calls",
    )
    lab_parser.set_defaults(handler=lab)

    attention_parser = commands.add_parser(
        "attention",
        help="what the attention questions would put forward, from the stored answers (no call)",
    )
    attention_parser.add_argument("--days", type=int, default=14)
    attention_parser.set_defaults(handler=attention)

    routing_parser = commands.add_parser(
        "routing-rules",
        help="what the archive rule in production and its candidates do to the stored "
        "decisions, against your verdicts (no call)",
    )
    routing_parser.add_argument("--days", type=int, default=90)
    routing_parser.set_defaults(handler=routing_rules)

    collect_parser = commands.add_parser(
        "sent-collect",
        help="ask JEV whether each mail you sent waited for an answer, and pick some to label",
    )
    collect_parser.add_argument("--days", type=int, default=90)
    collect_parser.add_argument("--max-threads", type=int, default=200)
    collect_parser.add_argument("--max-calls", type=int, default=DEFAULT_MAX_CALLS)
    collect_parser.add_argument("--path", default=DEFAULT_SAMPLES_PATH)
    collect_parser.set_defaults(handler=sent_collect)

    label_parser = commands.add_parser(
        "sent-label", help="say yes or no for the sent mails picked by sent-collect (no call)"
    )
    label_parser.add_argument("--path", default=DEFAULT_SAMPLES_PATH)
    label_parser.set_defaults(handler=sent_label)

    report_parser = commands.add_parser(
        "sent-report", help="precision and recall of each threshold on your labels (no call)"
    )
    report_parser.add_argument("--path", default=DEFAULT_SAMPLES_PATH)
    report_parser.set_defaults(handler=sent_report)

    args = parser.parse_args(argv)
    return args.handler(args, Settings())


if __name__ == "__main__":
    sys.exit(main())
