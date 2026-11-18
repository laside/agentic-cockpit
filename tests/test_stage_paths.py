import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))
import pipeline  # noqa: E402


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


class StagePathsTest(unittest.TestCase):
    def test_add_modify_delete_and_already_removed(self):
        with tempfile.TemporaryDirectory() as d:
            wt = Path(d)
            git(wt, "init", "-q")
            git(wt, "config", "user.email", "t@example.com")
            git(wt, "config", "user.name", "t")
            for name in ("keep.txt", "rm_plain.txt", "rm_git.txt"):
                (wt / name).write_text("x\n")
            git(wt, "add", ".")
            git(wt, "commit", "-qm", "init")
            (wt / "keep.txt").write_text("y\n")
            (wt / "new.txt").write_text("n\n")
            (wt / "rm_plain.txt").unlink()
            git(wt, "rm", "-q", "rm_git.txt")
            pipeline.stage_paths(wt, ["keep.txt", "new.txt", "rm_plain.txt", "rm_git.txt"])
            staged = git(wt, "diff", "--cached", "--name-status").split("\n")
            self.assertIn("M\tkeep.txt", staged)
            self.assertIn("A\tnew.txt", staged)
            self.assertIn("D\trm_plain.txt", staged)
            self.assertIn("D\trm_git.txt", staged)


if __name__ == "__main__":
    unittest.main()
