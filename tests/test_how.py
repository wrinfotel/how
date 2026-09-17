import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import how  # noqa: E402


def run(argv, env=None, cwd=None):
    """Run how.main with argv, env, cwd; return (code, stdout, stderr).

    SystemExit from die()/argparse is captured into code — that IS the CLI
    contract, tests just assert on it.
    """
    out, err = io.StringIO(), io.StringIO()
    old_env = dict(os.environ)
    old_cwd = os.getcwd()
    os.environ.update(env or {})
    if cwd:
        os.chdir(cwd)
    try:
        with redirect_stdout(out), redirect_stderr(err):
            try:
                code = how.main(argv)
            except SystemExit as exc:
                code = int(exc.code or 0)
    finally:
        os.environ.clear()
        os.environ.update(old_env)
        os.chdir(old_cwd)
    return code, out.getvalue(), err.getvalue()


class RecordFlow(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "how.json"
        self.addCleanup(self.tmp.cleanup)

    def rec(self, cmd, cwd=None):
        cwd = str(cwd or self.db.parent)
        code, out, err = run(["--db", str(self.db), "record"], env={"HOW_CMD": cmd}, cwd=cwd)
        self.assertEqual(code, 0, err)
        return out, err

    def test_record_and_list(self):
        for _ in range(3):
            self.rec("./gradlew bootRun --profiles dev")
        self.rec("docker compose up -d db")
        code, out, err = run(["--db", str(self.db)], cwd=str(self.db.parent))
        self.assertEqual(code, 0)
        self.assertIn("gradlew", out)
        self.assertIn("3x", out)
        self.assertIn("docker compose", out)

    def test_noise_not_kept(self):
        for noise in ("ls -la", "cd /tmp", "git status", "git diff", "history"):
            self.rec(noise)
        code, out, err = run(["--db", str(self.db)], cwd=str(self.db.parent))
        self.assertEqual(code, 0)
        self.assertIn("nothing recorded", err)

    def test_counter_and_last_seen(self):
        self.rec("make build")
        self.rec("make build")
        code, out, _ = run(["--db", str(self.db)], cwd=str(self.db.parent))
        self.assertIn("(2x", out)

    def test_expiry_flagged_for_package_managers(self):
        self.rec("sudo apt install htop")
        store = how.load_store(self.db)
        entry = list(store[str(self.db.parent)].values())[0]
        self.assertEqual(entry["x"], 1)
        self.rec("make build")
        store = how.load_store(self.db)
        self.assertEqual(store[str(self.db.parent)]["make build"]["x"], 0)

    def test_dedup_is_exact_command(self):
        self.rec("./gradlew bootRun")
        self.rec("./gradlew test")
        store = how.load_store(self.db)
        self.assertEqual(len(store[str(self.db.parent)]), 2)

    def test_record_positional(self):
        code, _, err = run(["--db", str(self.db), "record", "--", "./bin/deploy.sh"],
                           cwd=str(self.db.parent))
        self.assertEqual(code, 0, err)
        code, out, _ = run(["--db", str(self.db), "deploy"], cwd=str(self.db.parent))
        self.assertIn("deploy.sh", out)

    def test_record_to_unwritable_store_fails_loudly(self):
        block = Path(self.tmp.name) / "block"
        block.touch()
        code, _, err = run(["--db", str(block / "x.json"), "record"],
                           env={"HOW_CMD": "echo hi"})
        self.assertEqual(code, 1)
        self.assertIn("cannot read store", err)

    def test_record_empty_fails_usage(self):
        code, _, err = run(["--db", str(self.db), "record"], env={"HOW_CMD": ""})
        self.assertEqual(code, 2)

    def test_unicode_command(self):
        self.rec('echo "привет мир"')
        code, out, _ = run(["--db", str(self.db)], cwd=str(self.db.parent))
        self.assertIn("привет", out)


class Listing(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = self.root / "how.json"
        (self.root / "proj").mkdir()
        (self.root / "proj" / "sub").mkdir(parents=True)
        run(["--db", str(self.db), "record"], env={"HOW_CMD": "./gradlew bootRun"},
            cwd=str(self.root / "proj"))
        run(["--db", str(self.db), "record"], env={"HOW_CMD": "./gradlew bootRun"},
            cwd=str(self.root / "proj"))
        run(["--db", str(self.db), "record"],
            env={"HOW_CMD": "python3 -m unittest discover tests"},
            cwd=str(self.root / "proj" / "sub"))
        self.addCleanup(self.tmp.cleanup)

    def test_subdir_sees_project_root(self):
        code, out, err = run(["--db", str(self.db)], cwd=str(self.root / "proj" / "sub"))
        self.assertEqual(code, 0)
        self.assertIn("gradlew", out)
        self.assertIn("unittest", out)

    def test_needle_filters(self):
        code, out, _ = run(["--db", str(self.db), "gradlew"], cwd=str(self.root / "proj"))
        self.assertIn("gradlew", out)
        self.assertNotIn("unittest", out)

    def test_needle_named_like_subcommand(self):
        # subcommand words are reserved: `how record` (no $HOW_CMD, no
        # args) is a usage error, NOT a stats query for needle 'record'.
        # Searching for such words works via a longer needle (below).
        code, _, err = run(["--db", str(self.db), "record"], cwd=str(self.root / "proj"))
        self.assertEqual(code, 2)
        self.assertIn("nothing to record", err)
        run(["--db", str(self.db), "record"], env={"HOW_CMD": "recctl --force"},
            cwd=str(self.root / "proj"))
        code, out, _ = run(["--db", str(self.db), "rec"], cwd=str(self.root / "proj"))
        self.assertIn("recctl", out)

    def test_limit(self):
        code, out, _ = run(["--db", str(self.db), "-n", "1"], cwd=str(self.root / "proj"))
        self.assertEqual(out.count("\n"), 2)  # title + one command

    def test_all_flag(self):
        code, out, _ = run(["--db", str(self.db), "--all"], cwd=str(self.root))
        self.assertIn("gradlew", out)

    def test_json_output(self):
        code, out, _ = run(["--db", str(self.db), "--json"], cwd=str(self.root / "proj"))
        data = json.loads(out)
        self.assertEqual(data["count"], 1)
        self.assertIn("commands", data)
        self.assertIn("gradlew", data["commands"][0]["cmd"])

    def test_weight_orders_by_frequency(self):
        # 5x pytest vs 1x older gradlew: frequency must win with equal recency
        for _ in range(5):
            run(["--db", str(self.db), "record"], env={"HOW_CMD": "pytest -x"},
                cwd=str(self.root / "proj"))
        code, out, _ = run(["--db", str(self.db)], cwd=str(self.root / "proj"))
        self.assertLess(out.index("pytest"), out.index("gradlew"))

    def test_recency_beats_frequency_when_old(self):
        # pytest 5x but 200 days old; newer one-offs must outrank it
        for _ in range(5):
            run(["--db", str(self.db), "record"], env={"HOW_CMD": "pytest -x"},
                cwd=str(self.root / "proj"))
        store = how.load_store(self.db)
        for e in store[str(self.root / "proj")].values():
            if e["cmd"] == "pytest -x":
                e["ts"] -= 200 * 86400
        how.save_store(self.db, store)
        code, out, _ = run(["--db", str(self.db)], cwd=str(self.root / "proj"))
        self.assertGreater(out.index("pytest"), out.index("gradlew"))

    def test_dirs(self):
        code, out, _ = run(["--db", str(self.db), "dirs"], cwd=str(self.root))
        self.assertIn("proj", out)

    def test_empty_db_message(self):
        code, out, err = run(["--db", str(self.root / "nope.json")], cwd=str(self.root))
        self.assertEqual(code, 0)
        self.assertIn("nothing recorded", err)

    def test_broken_json_fails(self):
        (self.root / "bad.json").write_text("not json")
        code, _, err = run(["--db", str(self.root / "bad.json")], cwd=str(self.root))
        self.assertEqual(code, 1)
        self.assertIn("cannot read store", err)


class Forget(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = self.root / "how.json"
        (self.root / "p").mkdir()
        for cmd in ("htop", "sudo apt install htop", "make build"):
            run(["--db", str(self.db), "record"], env={"HOW_CMD": cmd},
                cwd=str(self.root / "p"))
        self.addCleanup(self.tmp.cleanup)

    def test_forget_removes_matching(self):
        code, out, _ = run(["--db", str(self.db), "forget", "htop"],
                           cwd=str(self.root / "p"))
        self.assertEqual(code, 0)
        self.assertIn("forgot 2 entries", out)
        code, out, _ = run(["--db", str(self.db)], cwd=str(self.root / "p"))
        self.assertIn("make build", out)
        self.assertNotIn("htop", out)

    def test_forget_requires_pattern(self):
        code, _, err = run(["--db", str(self.db), "forget"], cwd=str(self.root / "p"))
        self.assertEqual(code, 2)


class Cli(unittest.TestCase):
    def test_version(self):
        import subprocess
        r = subprocess.run(
            [sys.executable, str(Path(__file__).resolve().parent.parent / "how.py"), "-V"],
            capture_output=True, text=True)
        self.assertEqual(r.returncode, 0)
        self.assertIn(how.__version__, r.stdout)

    def test_bad_limit(self):
        code, _, err = run(["-n", "0"])
        self.assertEqual(code, 2)
        self.assertIn("limit must be positive", err)

    def test_init_bash(self):
        code, out, _ = run(["init", "bash"])
        self.assertEqual(code, 0)
        self.assertIn("PROMPT_COMMAND", out)
        self.assertIn("HOW_CMD", out)

    def test_init_zsh(self):
        code, out, _ = run(["init", "zsh"])
        self.assertEqual(code, 0)
        self.assertIn("precmd", out)

    def test_color_always(self):
        code, out, _ = run(["--color", "always", "--db", "/tmp/does-not-matter.json"])
        self.assertEqual(code, 0)


class HookEnv(unittest.TestCase):
    """The real hook body is: HOW_CMD="$COMMAND" how record.

    Tests simulate exactly that: env HOW_CMD + cwd, optional HOW_DB.
    """

    def test_hook_env_records_to_custom_db(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db = Path(tmp.name) / "how.json"
        code, _, err = run(["record"],
                           env={"HOW_CMD": "cargo build --release", "HOW_DB": str(db)},
                           cwd=tmp.name)
        self.assertEqual(code, 0, err)
        store = how.load_store(db)
        self.assertIn("cargo build --release", store[tmp.name])


if __name__ == "__main__":
    unittest.main()
