#!/usr/bin/env python3
"""Failure-loudness tests for the docs pipeline (issue #1404).

Deliberately break a reference in a scratch copy of the repo and assert the
docs-site build / snippet checker / link checker fail with a message that names
the broken reference, and that no stale output survives.

Usage: python3 tools/docs-site/test_failure_loud.py
"""

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONSTANTS = "contracts/ledgerlens-score/src/constants.rs"


def run(*cmd, cwd=ROOT):
    return subprocess.run(cmd, capture_output=True, text=True, cwd=cwd)


class FailureLoud(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = self.tmp / "repo"
        (self.root / "docs").mkdir(parents=True)
        (self.root / "docs/a.md").write_text("# A\n\n[b](b.md)\n")
        (self.root / "docs/b.md").write_text("# B\n")
        (self.root / "README.md").write_text("# R\n")
        (self.root / CONSTANTS).parent.mkdir(parents=True)
        (self.root / CONSTANTS).write_text("pub const CONTRACT_VERSION: u32 = 4;\n")
        self.out = self.tmp / "out"

    def build(self):
        return run(sys.executable, str(ROOT / "tools/docs-site/build.py"),
                   "--root", str(self.root), "--out", str(self.out))

    def test_baseline_builds(self):
        r = self.build()
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_missing_source_file_names_reference(self):
        (self.root / CONSTANTS).unlink()
        r = self.build()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("constants.rs", r.stderr)

    def test_renamed_constant_names_reference(self):
        (self.root / CONSTANTS).write_text("pub const CONTRACT_REV: u32 = 4;\n")
        r = self.build()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("CONTRACT_VERSION", r.stderr)

    def test_missing_readme_names_reference(self):
        (self.root / "README.md").unlink()
        r = self.build()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("README.md", r.stderr)

    def test_failed_build_leaves_no_stale_output(self):
        self.assertEqual(self.build().returncode, 0)
        (self.root / CONSTANTS).unlink()
        self.assertNotEqual(self.build().returncode, 0)
        # Stale pages from the previous good build must not be left to publish.
        self.assertFalse((self.out / "book").exists())

    def test_deleted_linked_document_fails_link_check(self):
        (self.root / "docs/b.md").unlink()
        script = (
            "import importlib.util,sys;"
            "s=importlib.util.spec_from_file_location('cl','tools/docs-site/check_links.py');"
            "m=importlib.util.module_from_spec(s);s.loader.exec_module(m);"
            f"from pathlib import Path;m.ROOT=Path({str(self.root)!r});sys.exit(m.main())"
        )
        r = run(sys.executable, "-c", script)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("b.md", r.stderr)

    def test_renamed_function_fails_snippet_check(self):
        sys.path.insert(0, str(ROOT / "tools"))
        import check_doc_snippets as c

        fns = c.source_fns("pub fn renamed(env: Env, a: u32) -> u32 {}")
        errs = c.check_doc("t", "### `get_score(a: u32) -> u32`\n", fns, {})
        self.assertTrue(errs)
        self.assertIn("get_score", errs[0])


if __name__ == "__main__":
    unittest.main(verbosity=1)
