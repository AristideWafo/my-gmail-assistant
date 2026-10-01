import argparse
import sys
from datetime import timedelta

from src.bootstrap import MAIL_PROVIDERS, STORES, BuildContext
from src.config import Settings
from src.domain import EmailMessage
from src.evaluation.dataset import build_dataset
from src.evaluation.runner import evaluate, format_report
from src.ports import EmailClassifier
from src.triage.engine import JevClassifier
from src.triage.few_shot import build_examples
from src.triage.heuristic import HeuristicClassifier

CANDIDATE_WINDOW = timedelta(days=90)
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
    variants = _variants(settings, examples, set(args.variants.split(",")) - {""})
    reports = [
        evaluate(name, classifier, dataset.cases, settings.low_confidence_threshold)
        for name, classifier in variants.items()
    ]
    print(format_report(reports, dataset.cases, dataset.missing, dataset.split_at))
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

    args = parser.parse_args(argv)
    return args.handler(args, Settings())


if __name__ == "__main__":
    sys.exit(main())
