from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
RUN_LIVE_AGENT_PATCH = (
    os.environ.get("AGENT_CORE_RUN_LIVE_TESTS") == "1"
    and bool(os.environ.get("OPENAI_API_KEY"))
)
LIVE_PROVIDER = os.environ.get("AGENT_CORE_LIVE_PROVIDER", "litellm")
LIVE_MODEL = os.environ.get("AGENT_CORE_LIVE_MODEL", "openai/gpt-4o-mini")


@unittest.skipUnless(
    RUN_LIVE_AGENT_PATCH,
    "requires AGENT_CORE_RUN_LIVE_TESTS=1 and OPENAI_API_KEY",
)
class TestAgentCoreLiveCLIPatch(unittest.TestCase):
    def test_agent_patch_rewrites_one_exact_comment_line_with_real_llm(self):
        # Verifies that the real CLI entrypoint can call a live model, generate a patch, and apply it to a workspace file.
        # This catches a broken end-to-end path where CLI bootstrap, model invocation, patch generation, or runtime patch application silently stop working.
        # The exact file content is correct because the prompt asks for one constrained line replacement and no other edits.
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            target = workspace / "math_utils.py"
            target.write_text(
                "def add(a, b):\n"
                "    # TODO: add comment\n"
                "    return a + b\n",
                encoding="utf-8",
            )
            runtime_db_path = workspace / "runtime.sqlite3"
            pycache_path = workspace / ".tmp_pycache"

            command = [
                sys.executable,
                "-m",
                "apps.cli.app",
                "--provider",
                LIVE_PROVIDER,
                "--model",
                LIVE_MODEL,
                "--runtime-db-path",
                str(runtime_db_path),
                "agent-patch",
                "--workspace",
                str(workspace),
                "--prompt",
                (
                    "Modify only math_utils.py. Replace the exact line "
                    "`# TODO: add comment` with `# Added by agent-core live test`. "
                    "Do not change any other line. Then complete."
                ),
                "--target-path",
                "math_utils.py",
            ]
            env = os.environ.copy()
            env["PYTHONPYCACHEPREFIX"] = str(pycache_path)

            completed = subprocess.run(
                command,
                cwd=REPO_ROOT,
                env=env,
                capture_output=True,
                text=True,
                timeout=300,
            )

            if completed.returncode != 0:
                self.fail(
                    "agent-patch live CLI command failed.\n"
                    f"exit_code={completed.returncode}\n"
                    f"stdout:\n{completed.stdout}\n"
                    f"stderr:\n{completed.stderr}"
                )

            try:
                outcome = json.loads(completed.stdout)
            except json.JSONDecodeError as exc:
                raise AssertionError(
                    "agent-patch live CLI command did not print valid JSON.\n"
                    f"stdout:\n{completed.stdout}\n"
                    f"stderr:\n{completed.stderr}"
                ) from exc

            self.assertEqual(outcome["status"], "completed")
            self.assertEqual(
                target.read_text(encoding="utf-8"),
                "def add(a, b):\n"
                "    # Added by agent-core live test\n"
                "    return a + b\n",
            )


if __name__ == "__main__":
    unittest.main()
