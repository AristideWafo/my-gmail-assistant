import argparse
import sys
from datetime import timedelta

from src.bootstrap import MAIL_PROVIDERS, STORES, BuildContext
from src.config import Settings
from src.domain import EmailMessage
from src.evaluation.attention import format_attention_report
from src.evaluation.corpus import LabCase, cases_from_rated, load_corpus
from src.evaluation.dataset import build_dataset
from src.evaluation.lab import format_lab_report, planned_calls, run_variant
from src.evaluation.runner import NOT_CONCLUSIVE, evaluate, format_report, is_conclusive
from src.evaluation.variants import VARIANTS
from src.ports import EmailClassifier
from src.triage.engine import JevClassifier
from src.triage.few_shot import build_examples
from src.triage.heuristic import HeuristicClassifier
from src.triage.rules import load_ruleset

CANDIDATE_WINDOW = timedelta(days=90)
DEFAULT_MAX_CALLS = 500
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
        # No split: question variants learn nothing from past verdicts.
        dataset = build_dataset(rated, mail, split_at="")
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


def lab(args: argparse.Namespace, settings: Settings) -> int:
    wanted = [name for name in args.variants.split(",") if name] or list(VARIANTS)
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
    else:
        cases, notes = load_corpus()[: args.limit or None], []
    variants = [VARIANTS[name] for name in wanted]
    calls = planned_calls(variants, cases, args.repeats)
    print(
        f"{calls} JEV call(s): {len(variants)} variant(s) x {len(cases)} mail(s) "
        f"x {args.repeats} run(s)"
    )
    if calls > args.max_calls:
        print(
            f"Refused: more than --max-calls {args.max_calls}. "
            "Raise it, or use --limit / --variants."
        )
        return 2
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
        choices=("corpus", "rated"),
        default="corpus",
        help="corpus: invented test mails; rated: the real mails you gave a verdict on",
    )
    lab_parser.add_argument(
        "--variants", default="", help=f"comma-separated subset of {sorted(VARIANTS)}"
    )
    lab_parser.add_argument(
        "--repeats", type=int, default=1, help="runs per mail, to see instability"
    )
    lab_parser.add_argument(
        "--limit", type=int, default=0, help="only the first N test mails, or the N latest verdicts"
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

    args = parser.parse_args(argv)
    return args.handler(args, Settings())


if __name__ == "__main__":
    sys.exit(main())
