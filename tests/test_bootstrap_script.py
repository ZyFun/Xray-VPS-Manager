from pathlib import Path
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP = ROOT / "bootstrap.sh"


class BootstrapScriptTests(unittest.TestCase):
    def test_bootstrap_shell_syntax(self) -> None:
        result = subprocess.run(
            ["bash", "-n", str(BOOTSTRAP)],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        self.assertEqual(result.returncode, 0, result.stderr)

    def test_bootstrap_installs_from_release_archives(self) -> None:
        content = BOOTSTRAP.read_text(encoding="utf-8")

        self.assertIn("https://api.github.com/repos/${REPO}/releases/latest", content)
        self.assertIn("https://github.com/${REPO}/archive/refs/tags/${tag}.tar.gz", content)
        self.assertIn("bash \"$INSTALL_DIR/install.sh\"", content)

    def test_bootstrap_help_documents_latest_tag_install_only(self) -> None:
        result = subprocess.run(
            [str(BOOTSTRAP), "--help"],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("api.github.com/repos/ZyFun/Xray-VPS-Manager/releases/latest", result.stdout)
        self.assertIn("${TAG}/bootstrap.sh", result.stdout)
        self.assertNotIn("main/bootstrap.sh", result.stdout)

    def test_bootstrap_supports_version_and_no_install_modes(self) -> None:
        content = BOOTSTRAP.read_text(encoding="utf-8")

        self.assertIn("--version", content)
        self.assertIn("--no-install", content)
        self.assertIn("XRAY_MANAGER_VERSION", content)
        self.assertIn("XRAY_MANAGER_RUN_INSTALL", content)

    def test_bootstrap_refuses_existing_runtime_without_force(self) -> None:
        content = BOOTSTRAP.read_text(encoding="utf-8")

        self.assertIn("/usr/local/etc/xray/config.json", content)
        self.assertIn("/usr/local/etc/xray/manager.db", content)
        self.assertIn("xray-manager-update --check", content)
        self.assertIn("--force-install", content)


if __name__ == "__main__":
    unittest.main()
