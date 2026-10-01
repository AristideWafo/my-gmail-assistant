import asyncio
import os
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

from main import create_app
from src.version import PYPROJECT, UNKNOWN, app_version


class AppVersionTests(unittest.TestCase):
    def test_reads_the_version_of_the_shipped_pyproject(self):
        with open(PYPROJECT, "rb") as handle:
            expected = tomllib.load(handle)["project"]["version"]

        self.assertEqual(app_version(), expected)
        self.assertRegex(app_version(), r"^\d+\.\d+\.\d+")

    def test_missing_or_unreadable_file_is_unknown_rather_than_a_crash(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        broken = Path(tmp.name) / "pyproject.toml"

        self.assertEqual(app_version(broken), UNKNOWN)
        for content in ("not = [toml", '[project]\nname = "x"\n'):
            with self.subTest(content=content):
                broken.write_text(content)

                self.assertEqual(app_version(broken), UNKNOWN)


class LifespanTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(
            os.environ,
            {"DB_PATH": ":memory:", "STARTUP_CHECKS": "off", "TELEGRAM_BOT_TOKEN": ""},
        )
        env.start()
        self.addCleanup(env.stop)
        # The real loop would call Gmail; the lifecycle only needs a task that waits.
        loop = patch("main.polling_loop", side_effect=lambda ctx: asyncio.sleep(3600))
        loop.start()
        self.addCleanup(loop.stop)

    def test_app_carries_the_released_version(self):
        self.assertEqual(create_app().version, app_version())

    def test_startup_starts_polling_and_shutdown_stops_it_and_closes_the_store(self):
        app = create_app()

        async def scenario():
            async with app.router.lifespan_context(app):
                self.assertFalse(app.state.polling_task.done())
                self.assertFalse(app.state.watchdog_task.done())
                self.assertFalse(app.state.ctx.stopping.is_set())
            return app.state.polling_task, app.state.watchdog_task

        with patch.object(app.state.ctx, "close") as close:
            polling, watchdog = asyncio.run(scenario())

        self.assertTrue(polling.cancelled())
        self.assertTrue(watchdog.cancelled())
        self.assertTrue(app.state.ctx.stopping.is_set())
        close.assert_called_once_with()

    def test_history_is_synced_before_polling_only_when_asked(self):
        for wanted in (False, True):
            with self.subTest(sync_history=wanted), patch("main.sync_history_once") as sync:
                app = create_app(sync_history=wanted)

                async def scenario(app=app):
                    async with app.router.lifespan_context(app):
                        pass

                asyncio.run(scenario())

                self.assertEqual(sync.called, wanted)

    def test_a_startup_that_fails_still_releases_what_was_opened(self):
        app = create_app()

        async def scenario():
            async with app.router.lifespan_context(app):
                self.fail("the application must not start")

        with (
            patch("main.run_startup_checks", side_effect=RuntimeError("fatal check")),
            patch.object(app.state.ctx, "close") as close,
            self.assertRaises(RuntimeError),
        ):
            asyncio.run(scenario())

        close.assert_called_once_with()
        self.assertTrue(app.state.ctx.stopping.is_set())


if __name__ == "__main__":
    unittest.main()
