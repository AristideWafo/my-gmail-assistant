import ast
import unittest
from pathlib import Path

from src import bootstrap
from src.config import Settings
from src.ports import (
    AlertChannel,
    ChatInbox,
    DecisionStore,
    EmailAnalyzer,
    EmailClassifier,
    MailProvider,
    PendingActions,
    ThreadStore,
    Unsubscriber,
)
from tests import fakes

ROOT = Path(__file__).resolve().parent.parent
CORE_MODULES = [
    ROOT / "main.py",
    ROOT / "src" / "workflow.py",
    ROOT / "src" / "gateways" / "alerts.py",
    *sorted((ROOT / "src" / "interactions").glob("*.py")),
    *sorted((ROOT / "src" / "maintenance").glob("*.py")),
    *sorted((ROOT / "src" / "followup").glob("*.py")),
]
ADAPTER_MODULES = (
    "src.gmail",
    "src.llm",
    "src.storage",
    "src.triage.engine",
    "src.triage.sent_mail",
    "src.triage.heuristic",
    "src.triage.fallback",
    "src.gateways.telegram_bot",
    "src.gateways.discord",
    "src.gateways.unsubscribe_http",
)
# These package __init__ files re-export adapters, so importing from them pulls adapters in.
ADAPTER_REEXPORTING_PACKAGES = ("src.triage", "src.gateways")

FACTORY_REGISTRIES = {
    "MAIL_PROVIDERS": (bootstrap.MAIL_PROVIDERS, MailProvider),
    "CLASSIFIERS": (bootstrap.CLASSIFIERS, EmailClassifier),
    "ANALYZERS": (bootstrap.ANALYZERS, EmailAnalyzer),
    "ALERT_CHANNELS": (bootstrap.ALERT_CHANNELS, AlertChannel),
    "CHAT_INBOXES": (bootstrap.CHAT_INBOXES, ChatInbox),
    "UNSUBSCRIBERS": (bootstrap.UNSUBSCRIBERS, Unsubscriber),
}
# "none" is the one registered way to opt out of a chat inbox.
OPTIONAL = {("CHAT_INBOXES", "none"), ("UNSUBSCRIBERS", "none")}


def is_adapter(module: str) -> bool:
    return module in ADAPTER_REEXPORTING_PACKAGES or any(
        module == adapter or module.startswith(f"{adapter}.") for adapter in ADAPTER_MODULES
    )


def imported_modules(path: Path) -> set[str]:
    modules = set()
    for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
            modules.update(f"{node.module}.{alias.name}" for alias in node.names)
    return modules


class RegistryConformanceTests(unittest.TestCase):
    def setUp(self):
        self.settings = Settings(_env_file=None, db_path=":memory:")

    def test_every_store_implements_the_port(self):
        for name, factory in bootstrap.STORES.items():
            with self.subTest(store=name):
                store = factory(self.settings)
                self.addCleanup(store.close)
                self.assertIsInstance(store, DecisionStore)

    def test_every_registered_adapter_implements_its_port(self):
        ctx = bootstrap.BuildContext(self.settings, fakes.fake_components().store)
        for registry_name, (registry, port) in FACTORY_REGISTRIES.items():
            for name, factory in registry.items():
                with self.subTest(registry=registry_name, adapter=name):
                    product = factory(ctx)
                    if (registry_name, name) in OPTIONAL:
                        self.assertIsNone(product)
                    else:
                        self.assertIsInstance(product, port)

    def test_fakes_implement_the_ports(self):
        components = fakes.fake_components()
        self.assertIsInstance(components.mail, MailProvider)
        self.assertIsInstance(components.classifier, EmailClassifier)
        self.assertIsInstance(components.analyzer, EmailAnalyzer)
        self.assertIsInstance(components.channels[0], AlertChannel)
        self.assertIsInstance(components.chat, ChatInbox)
        self.assertIsInstance(components.store, DecisionStore)
        self.assertIsInstance(components.store.threads, ThreadStore)
        self.assertIsInstance(components.store.pending_actions, PendingActions)


class CoreIsolationTests(unittest.TestCase):
    def test_is_adapter_matches_modules_packages_and_submodules(self):
        self.assertTrue(is_adapter("src.gmail.client"))
        self.assertTrue(is_adapter("src.triage"))
        self.assertFalse(is_adapter("src.triage.rules"))
        self.assertFalse(is_adapter("src.gateways.alerts"))

    def test_core_modules_import_no_adapter(self):
        for path in CORE_MODULES:
            with self.subTest(module=str(path.relative_to(ROOT))):
                leaked = sorted(m for m in imported_modules(path) if is_adapter(m))
                self.assertEqual(leaked, [])


if __name__ == "__main__":
    unittest.main()
