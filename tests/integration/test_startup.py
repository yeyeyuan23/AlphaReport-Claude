import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.support.paths import PROJECT_ROOT


class StartupTests(unittest.TestCase):
    def test_ops_launchers_use_project_root_when_called_from_another_directory(self):
        root = PROJECT_ROOT
        commands = [("run_bot.sh", "reportagent.bot.feishu"),
                    ("sync_public_url.sh", "reportagent.cli.sync_public_url")]
        for script, module in commands:
            with self.subTest(script=script), tempfile.TemporaryDirectory(prefix="alpha launch ") as directory:
                project = Path(directory)
                target = project / "scripts" / "ops" / script
                target.parent.mkdir(parents=True)
                source = root / "scripts" / "ops" / script
                self.assertTrue(source.is_file(), f"Missing operational launcher: {source}")
                shutil.copy2(source, target)
                binary = project / ".venv" / "bin" / "python"
                binary.parent.mkdir(parents=True)
                output = project / "called.txt"
                binary.write_text('#!/bin/bash\nprintf "%s\\n" "$PWD" "$@" > "$LAUNCH_FIXTURE_OUTPUT"\n')
                binary.chmod(0o755)
                result = subprocess.run(["bash", str(target)],
                                        cwd="/tmp", env={**os.environ, "LAUNCH_FIXTURE_OUTPUT": str(output)},
                                        capture_output=True, text=True, timeout=5)
                self.assertEqual(result.returncode, 0, result.stderr)
                lines = output.read_text().splitlines()
                self.assertEqual(Path(lines[0]).resolve(), project.resolve())
                self.assertEqual(lines[1:], ["-m", module])



if __name__ == "__main__":
    unittest.main()
