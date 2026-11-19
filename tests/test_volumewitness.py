from copy import deepcopy
import json
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
from unittest.mock import patch

from volumewitness import InputError, compare


ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples"
TARGET = "/var/lib/postgresql/data"
MOUNTS = json.loads((EXAMPLES / "mounts.json").read_text())
MODEL = json.loads((EXAMPLES / "next.json").read_text())


class ComparisonTests(unittest.TestCase):
    def setUp(self):
        self.model = deepcopy(MODEL)
        self.mounts = deepcopy(MOUNTS)

    def finding(self):
        return compare(self.mounts, self.model, "db", TARGET)

    def test_detects_project_rename(self):
        result = self.finding()
        self.assertEqual(result.status, "changed")
        self.assertEqual(result.current_volume, "demo_pgdata")
        self.assertEqual(result.proposed_volume, "demo_v2_pgdata")

    def test_resolved_external_name_matches_across_project_names(self):
        self.model["volumes"]["pgdata"] = {"name": "demo_pgdata", "external": True}
        self.assertEqual(self.finding().status, "same")

    def test_source_alias_is_not_the_engine_volume_name(self):
        self.model["services"]["db"]["volumes"][0]["source"] = "data"
        self.model["volumes"] = {"data": {"name": "demo_pgdata"}}
        self.assertEqual(self.finding().status, "same")

    def test_multiple_mounts_only_compares_exact_destination(self):
        self.mounts.insert(0, {"Type": "bind", "Destination": TARGET + "/nested"})
        self.assertEqual(self.finding().current_volume, "demo_pgdata")

    def test_current_mount_missing(self):
        self.mounts.clear()
        self.assertEqual(self.finding().status, "unknown")

    def test_proposed_mount_missing(self):
        self.model["services"]["db"].pop("volumes")
        self.assertEqual(self.finding().status, "unknown")

    def test_missing_service_is_unknown(self):
        self.model["services"].clear()
        self.assertEqual(self.finding().status, "unknown")

    def test_named_volume_changes_to_bind_mount(self):
        self.model["services"]["db"]["volumes"] = [{"type": "bind", "target": TARGET}]
        self.assertEqual(self.finding().status, "changed")

    def test_bind_mount_changes_to_named_volume(self):
        self.mounts = [{"Type": "bind", "Destination": TARGET}]
        self.assertEqual(self.finding().status, "changed")

    def test_bind_mounts_are_indeterminate(self):
        self.mounts = [{"Type": "bind", "Destination": TARGET}]
        self.model["services"]["db"]["volumes"] = [{"type": "bind", "target": TARGET}]
        self.assertEqual(self.finding().status, "unknown")

    def test_anonymous_volume_is_indeterminate(self):
        self.model["services"]["db"]["volumes"][0].pop("source")
        self.assertEqual(self.finding().status, "unknown")

    def test_missing_resolved_name_is_rejected(self):
        self.model["volumes"]["pgdata"] = {}
        with self.assertRaisesRegex(InputError, "resolved name"):
            self.finding()

    def test_duplicate_mount_target_is_rejected(self):
        self.mounts.append(self.mounts[0])
        with self.assertRaisesRegex(InputError, "more than once"):
            self.finding()


class CLITests(unittest.TestCase):
    def run_cli(self, *args):
        return subprocess.run(
            [sys.executable, str(ROOT / "volumewitness.py"), "--service", "db", "--target", TARGET, *args],
            capture_output=True, text=True, check=False,
        )

    def test_offline_cli_reports_change_as_json(self):
        result = self.run_cli("--mounts-json", EXAMPLES / "mounts.json", "--model-json",
                              EXAMPLES / "next.json", "--format", "json")
        self.assertEqual(result.returncode, 1, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "changed")
        self.assertEqual(payload["proposed_volume"], "demo_v2_pgdata")

    def test_cannot_mix_offline_and_live_parameters(self):
        result = self.run_cli("--mounts-json", EXAMPLES / "mounts.json", "--context", "local")
        self.assertEqual(result.returncode, 2)
        self.assertIn("requires --model-json", result.stderr)

    def test_missing_file_is_json_error(self):
        result = self.run_cli("--mounts-json", EXAMPLES / "missing.json", "--model-json",
                              EXAMPLES / "next.json", "--format", "json")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stdout)["status"], "error")

    def test_live_cli_uses_only_read_operations_in_the_same_context(self):
        from volumewitness import load_inputs, parse_args

        args = parse_args([
            "--service", "db", "--target", TARGET, "--container", "demo-db-1",
            "--context", "local", "-f", "base.yaml", "-f", "override.yaml",
            "--env-file", "deploy.env", "--profile", "storage", "-p", "demo",
        ])
        with patch("volumewitness.subprocess.run") as run:
            run.side_effect = [
                subprocess.CompletedProcess([], 0, json.dumps(data).encode(), b"")
                for data in (MOUNTS, MODEL)
            ]
            self.assertEqual(load_inputs(args), (MOUNTS, MODEL))
        self.assertEqual([call.args[0] for call in run.call_args_list], [
            ["docker", "--context=local", "container", "inspect", "--format",
             "{{json .Mounts}}", "--", "demo-db-1"],
            ["docker", "--context=local", "compose", "--file=base.yaml", "--file=override.yaml",
             "--env-file=deploy.env", "--profile=storage", "--project-name=demo", "config", "--format", "json"],
        ])

    def test_docker_error_does_not_echo_resolved_env_or_stderr(self):
        from volumewitness import docker_json

        with patch("volumewitness.subprocess.run", side_effect=subprocess.CalledProcessError(
            1, ["docker"], output=b"hidden-example", stderr=b"hidden-example"
        )):
            with self.assertRaisesRegex(InputError, "^Docker compose config failed;"):
                docker_json(["compose", "config"], None, "compose config")


class DockerComposeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if shutil.which("docker") is None:
            raise unittest.SkipTest("Docker CLI is not installed")
        result = subprocess.run(["docker", "compose", "version"], capture_output=True, timeout=20)
        if result.returncode:
            raise unittest.SkipTest("Docker Compose plugin is not available")

    def render(self, project, *more):
        result = subprocess.run([
            "docker", "compose", "-p", project, "-f", str(EXAMPLES / "compose.yaml"), *more,
            "config", "--format", "json",
        ], capture_output=True, check=True, timeout=20)
        return json.loads(result.stdout)

    def test_actual_compose_resolves_stable_and_renamed_project_volumes(self):
        for project, status in (("demo", "same"), ("demo_v2", "changed")):
            with self.subTest(project=project):
                rendered = self.render(project)
                self.assertEqual(compare(MOUNTS, rendered, "db", TARGET).status, status)

    def test_actual_compose_resolves_volume_name_from_override(self):
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as directory:
            override = Path(directory) / "override.yaml"
            override.write_text("volumes:\n  pgdata:\n    name: demo_pgdata\n", encoding="utf-8")
            rendered = self.render("another_project", "-f", str(override))
            self.assertEqual(compare(MOUNTS, rendered, "db", TARGET).status, "same")


if __name__ == "__main__":
    unittest.main()
