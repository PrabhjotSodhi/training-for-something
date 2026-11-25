import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


@unittest.skipUnless(os.name == "nt", "Windows installer")
class WindowsInstallTest(unittest.TestCase):
    def _run_install_step(self, scenario: str) -> tuple[subprocess.CompletedProcess[str], str]:
        installer = (Path(__file__).resolve().parents[1] / "install.ps1").read_text()
        step = installer.partition("Write-Host '3/4 Installing TFS'")[2]
        step = step.partition("uv tool update-shell")[0]
        fakes = r"""
$ErrorActionPreference = 'Stop'
$waitsLeft = if ($scenario -eq 'clear') { 0 } else { 2 }
$toolDir = Join-Path $pwd 'tool folder'
function uv {
    if ("$args" -eq 'tool dir') { return $toolDir }
    if ("$args" -ne 'tool install --force wheel') { throw "Unexpected uv call: $args" }
    Write-Output 'Installing wheel'
    Set-Content old.cmd '@exit /b 1'
    if ($waitsLeft) { throw 'Tried to install too early' }
}
function Get-CimInstance {
    $python = Join-Path $toolDir 'training-for-something\Scripts\python.exe'
    if (!$waitsLeft) { $python = Join-Path $pwd 'other\Scripts\python.exe' }
    @{ProcessId=1; ParentProcessId=0; Name='codex.exe'}
    @{ProcessId=2; ParentProcessId=1; Name='tfs.exe'}
    @{ProcessId=3; ParentProcessId=2; Name='python.exe'; CommandLine="`"$python`" tfs.exe `"mcp`""}
    @{ProcessId=4; ParentProcessId=9; Name='tfs.exe'}
    @{ProcessId=5; ParentProcessId=4; Name='python.exe'; CommandLine="`"$python`" tfs.exe mcp"}
}
function Start-Sleep {
    if (!$waitsLeft) { throw 'Waited for another install' }
    if ($scenario -eq 'cancel') { throw [System.Management.Automation.PipelineStoppedException]::new() }
    $script:waitsLeft--
}
$wheel = 'wheel'
"""
        with tempfile.TemporaryDirectory() as folder:
            old = Path(folder) / "old.cmd"
            old.write_text("@echo old install starts\n")
            result = subprocess.run(
                [
                    shutil.which("powershell.exe"),
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    f"$scenario = '{scenario}'\n" + fakes + step,
                ],
                cwd=folder,
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            old_starts = subprocess.run([str(old)], capture_output=True, text=True, check=False)
        return result, old_starts.stdout.strip()

    def test_waits_for_helper(self):
        # GIVEN a helper that closes during the wait
        # WHEN
        result, _ = self._run_install_step("wait")
        # THEN
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout.splitlines(),
            [
                "An AI tool is using TFS. Close it, and the install continues.",
                "Codex is using TFS. Close it, and the install continues.",
                "Installing wheel",
            ],
        )

    def test_installs_without_waiting(self):
        # GIVEN only a helper from a different install
        # WHEN
        result, _ = self._run_install_step("clear")
        # THEN
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "Installing wheel")

    def test_cancel_preserves_old_install(self):
        # GIVEN an interruption during the wait
        # WHEN
        result, old = self._run_install_step("cancel")
        # THEN
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Close it, and the install continues.", result.stdout)
        self.assertNotIn("Installing wheel", result.stdout)
        self.assertEqual(old, "old install starts")
