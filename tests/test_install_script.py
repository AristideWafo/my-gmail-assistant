import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

FAKE_DOCKER = """#!/usr/bin/env bash
echo "$*" >> "$DOCKER_LOG"
if [[ "$1 $2" == "network inspect" ]]; then
    [[ " $EXISTING_NETWORKS " == *" $3 "* ]]
fi
"""

BUILD = "-f docker-compose.yml -f docker-compose.build.yml"
TRAEFIK = "-f docker-compose.traefik.yml"


@unittest.skipUnless(shutil.which("bash"), "needs bash")
class InstallScriptTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)
        shutil.copy(ROOT / "install.sh", self.dir)
        shutil.copy(ROOT / ".env.example", self.dir)
        bin_dir = self.dir / "bin"
        bin_dir.mkdir()
        docker = bin_dir / "docker"
        docker.write_text(FAKE_DOCKER)
        docker.chmod(docker.stat().st_mode | stat.S_IEXEC)
        self.log = self.dir / "docker.log"

    def run_install(self, mode, env_file=None, networks=""):
        if env_file is not None:
            (self.dir / ".env").write_text(env_file)
        result = subprocess.run(
            ["bash", str(self.dir / "install.sh"), mode],
            capture_output=True,
            text=True,
            check=False,
            env={
                "PATH": f"{self.dir / 'bin'}:/usr/bin:/bin",
                "DOCKER_LOG": str(self.log),
                "EXISTING_NETWORKS": networks,
            },
        )
        return result, result.stdout + result.stderr

    def compose_calls(self):
        if not self.log.exists():
            return []
        calls = self.log.read_text().splitlines()
        return [c for c in calls if c.startswith("compose -f")]

    def test_first_run_writes_env_and_starts_nothing(self):
        result, output = self.run_install("--docker")

        self.assertEqual(result.returncode, 1)
        self.assertTrue((self.dir / ".env").exists())
        self.assertIn("Nothing was started", output)
        self.assertEqual(self.compose_calls(), [])

    def test_docker_builds_from_the_checkout_without_traefik(self):
        result, output = self.run_install("--docker", env_file="VERSION=1.9.0\n")

        self.assertEqual(result.returncode, 0, output)
        self.assertEqual(self.compose_calls(), [f"compose {BUILD} up --build -d"])
        self.assertNotIn("Traefik", output)

    def test_release_pulls_the_published_image_then_starts(self):
        result, output = self.run_install("--release", env_file="VERSION=1.9.0\n")

        self.assertEqual(result.returncode, 0, output)
        self.assertEqual(
            self.compose_calls(),
            ["compose -f docker-compose.yml pull", "compose -f docker-compose.yml up -d"],
        )

    def test_a_grafana_hostname_adds_the_traefik_file(self):
        env_file = "GRAFANA_HOSTNAME=grafana.example.com\nGRAFANA_ADMIN_PASSWORD='s3cret pass'\n"

        result, output = self.run_install("--release", env_file=env_file, networks="proxy")

        self.assertEqual(result.returncode, 0, output)
        self.assertEqual(
            self.compose_calls(),
            [
                f"compose -f docker-compose.yml {TRAEFIK} pull",
                f"compose -f docker-compose.yml {TRAEFIK} up -d",
            ],
        )
        self.assertIn("https://grafana.example.com", output)
        self.assertIn("COMPOSE_FILE=", output)

    def test_build_and_traefik_combine(self):
        env_file = (
            "GRAFANA_HOSTNAME=grafana.example.com\nGRAFANA_ADMIN_PASSWORD=s3cret\n"
            "TRAEFIK_NETWORK=edge\nCOMPOSE_FILE=docker-compose.yml:docker-compose.traefik.yml\n"
        )

        result, output = self.run_install("--docker", env_file=env_file, networks="edge")

        self.assertEqual(result.returncode, 0, output)
        self.assertEqual(self.compose_calls(), [f"compose {BUILD} {TRAEFIK} up --build -d"])
        self.assertNotIn("Add COMPOSE_FILE", output)

    def test_a_commented_hostname_is_not_a_hostname(self):
        result, output = self.run_install(
            "--release", env_file="# GRAFANA_HOSTNAME=grafana.example.com\n"
        )

        self.assertEqual(result.returncode, 0, output)
        self.assertNotIn(TRAEFIK, "\n".join(self.compose_calls()))

    def test_grafana_is_not_published_with_a_default_password(self):
        for password in ("", "GRAFANA_ADMIN_PASSWORD=admin\n", "GRAFANA_ADMIN_PASSWORD=change_me\n"):
            with self.subTest(password=password):
                result, output = self.run_install(
                    "--release",
                    env_file=f"GRAFANA_HOSTNAME=grafana.example.com\n{password}",
                    networks="proxy",
                )

                self.assertEqual(result.returncode, 1)
                self.assertIn("GRAFANA_ADMIN_PASSWORD", output)
                self.assertEqual(self.compose_calls(), [])

    def test_a_missing_traefik_network_stops_before_starting(self):
        env_file = "GRAFANA_HOSTNAME=grafana.example.com\nGRAFANA_ADMIN_PASSWORD=s3cret\n"

        result, output = self.run_install("--release", env_file=env_file, networks="other")

        self.assertEqual(result.returncode, 1)
        self.assertIn("'proxy' not found", output)
        self.assertEqual(self.compose_calls(), [])

    def test_unknown_mode_is_rejected(self):
        result, output = self.run_install("--prod", env_file="")

        self.assertEqual(result.returncode, 1)
        self.assertIn("Unknown mode", output)


if __name__ == "__main__":
    unittest.main()
