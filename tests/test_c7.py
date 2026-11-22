"""Offline tests for skills/context7-add/scripts/c7.py.

The Context7 API is stubbed: nothing is sent, and no API key is needed.
Run from the repo root:  python3 -m unittest discover -s tests -v
"""
import contextlib
import importlib.util
import io
import json
import os
import signal
import tempfile
import unittest
import urllib.error
import urllib.request

C7_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "skills", "context7-add", "scripts", "c7.py")

A = "https://github.com/acme/a"
B = "https://github.com/acme/b"
C = "https://docs.acme.dev/"
QUEUE_FULL = (400, {"error": "user-has-active-task",
                    "message": "You already have 5 libraries being processed or queued."})


def load_c7():
    spec = importlib.util.spec_from_file_location("c7", C7_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def ok(lib):
    return 200, {"libraryName": lib, "message": "Repository submitted successfully"}


class C7TestCase(unittest.TestCase):
    """Loads a fresh c7 module per test with call(), log() and sleep stubbed."""

    def setUp(self):
        self.c7 = load_c7()
        self.sent, self.logged, self.sleeps = [], [], []
        self.c7.log = lambda action, **kw: self.logged.append((action, kw))
        self.c7.time.sleep = lambda s: self.sleeps.append(s)
        self.c7.api_key = lambda: "test-key"
        self.sigterm = signal.getsignal(signal.SIGTERM)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(signal.signal, signal.SIGTERM, self.sigterm)

    def run_c7(self, argv, responses=()):
        """Run main() with queued responses (or exceptions); return (exit_code, json_or_None, stderr)."""
        queue = list(responses)

        def fake_call(method, path, body=None, params=None, form=None):
            self.sent.append((method, path, body))
            item = queue.pop(0)
            if isinstance(item, BaseException):
                raise item
            return item

        self.c7.call = fake_call
        out, err, code = io.StringIO(), io.StringIO(), 0
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            import sys
            old_argv, sys.argv = sys.argv, ["c7.py"] + list(argv)
            try:
                self.c7.main()
            except SystemExit as e:
                code = e.code or 0
            finally:
                sys.argv = old_argv
        text = out.getvalue()
        return code, (json.loads(text) if text.strip() else None), err.getvalue()

    def make_file(self, name, size=None, data=None):
        path = os.path.join(self.tmp.name, name)
        with open(path, "wb") as f:
            if data is not None:
                f.write(data)
            else:
                f.truncate(size)
        return path


class TestClassify(C7TestCase):
    def test_repo_url(self):
        c = self.c7.classify("https://github.com/Acme/Widgets.git")
        self.assertEqual((c["kind"], c["libraryId"]), ("github", "/acme/widgets"))

    def test_owner_repo_shorthand(self):
        self.assertEqual(self.c7.classify("acme/widgets")["libraryId"], "/acme/widgets")

    def test_library_id_and_context7_url(self):
        self.assertEqual(self.c7.classify("/acme/widgets")["kind"], "library_id")
        self.assertEqual(self.c7.classify("https://context7.com/acme/widgets")["libraryId"], "/acme/widgets")

    def test_github_owner(self):
        self.assertEqual(self.c7.classify("https://github.com/acme")["kind"], "repo_owner")

    def test_llms_openapi_website(self):
        self.assertEqual(self.c7.classify("https://docs.acme.dev/llms.txt")["kind"], "llmstxt")
        self.assertEqual(self.c7.classify("https://acme.dev/openapi.yaml")["kind"], "openapi")
        self.assertEqual(self.c7.classify("https://docs.acme.dev/guide/")["kind"], "website")


class TestWebsiteIds(C7TestCase):
    def test_documented_shapes(self):
        self.assertEqual(self.c7.website_id_candidates("https://docs.github.com/en")[0], "/websites/github_en")
        self.assertEqual(self.c7.website_id_candidates("https://example.com/docs/guide/")[0],
                         "/websites/example_guide")
        self.assertEqual(self.c7.website_id_candidates("https://docs.astral.sh/")[0], "/websites/astral_sh")


class TestAddSingle(C7TestCase):
    def test_success_keeps_old_shape(self):
        code, o, err = self.run_c7(["add", A, "--apply"], [ok("/acme/a")])
        self.assertEqual(code, 0)
        self.assertEqual(o["libraryId"], "/acme/a")
        self.assertNotIn("results", o)
        self.assertIn("[1/1] " + A + ": 200 /acme/a", err)
        self.assertEqual(self.logged[0][1]["libraryId"], "/acme/a")

    def test_already_exists_is_success(self):
        code, o, _ = self.run_c7(["add", A, "--apply"],
                                 [(400, {"error": "library-already-exists", "message": "The library /acme/a already exists."})])
        self.assertEqual((code, o["libraryId"]), (0, "/acme/a"))

    def test_already_exists_with_null_message(self):
        code, o, _ = self.run_c7(["add", A, "--apply"], [(400, {"error": "library-already-exists", "message": None})])
        self.assertEqual((code, o["libraryId"]), (0, "/acme/a"))

    def test_200_with_non_json_body_is_success(self):
        code, _, _ = self.run_c7(["add", A, "--apply"], [(200, "OK")])
        self.assertEqual(code, 0)

    def test_queue_full_exits_9(self):
        code, o, _ = self.run_c7(["add", A, "--apply"], [QUEUE_FULL])
        self.assertEqual(code, 9)
        self.assertIn("--queue", o["meaning"])

    def test_dry_run_sends_nothing(self):
        code, o, _ = self.run_c7(["add", A])
        self.assertEqual(code, 0)
        self.assertTrue(o["dryRun"])
        self.assertEqual(o["input"], A)
        self.assertEqual(self.sent, [])


class TestAddMany(C7TestCase):
    def test_queue_full_stops_and_lists_the_rest(self):
        code, o, _ = self.run_c7(["add", A, B, C, "--apply"], [ok("/acme/a"), QUEUE_FULL])
        self.assertEqual(code, 9)
        self.assertEqual(len(self.sent), 2)
        self.assertEqual(o["notSubmitted"], [B, C])
        self.assertEqual(o["results"][0]["libraryId"], "/acme/a")

    def test_queue_waits_and_retries(self):
        code, o, err = self.run_c7(["add", A, B, "--queue", "--apply"],
                                   [ok("/acme/a"), QUEUE_FULL, QUEUE_FULL, ok("/acme/b")])
        self.assertEqual(code, 0)
        self.assertEqual(self.sleeps, [30, 30])
        self.assertIn("retrying " + B, err)
        self.assertNotIn("notSubmitted", o)

    def test_queue_timeout_gives_up(self):
        code, o, _ = self.run_c7(["add", A, "--queue", "--queue-timeout", "0", "--apply"], [QUEUE_FULL])
        self.assertEqual(code, 9)
        self.assertIn("larger --queue-timeout", o["meaning"])

    def test_other_refusals_go_to_failed_and_keep_going(self):
        code, o, _ = self.run_c7(["add", A, C, "--apply"],
                                 [(400, {"error": "library-already-exists", "message": "The library /acme/a already exists."}),
                                  (422, {"error": "not_documentation_website", "message": "Website score 1 below threshold 9"})])
        self.assertEqual(code, 7)
        self.assertEqual(o["failed"], [C])
        self.assertIn("documentation site", o["results"][1]["meaning"])

    def test_real_refusal_wins_over_queue_full(self):
        code, o, _ = self.run_c7(["add", A, B, C, "--apply"],
                                 [(403, {"error": "access_denied", "message": "not allowed"}), QUEUE_FULL])
        self.assertEqual(code, 5)
        self.assertEqual((o["failed"], o["notSubmitted"]), ([A], [B, C]))

    def test_rate_limit_stops(self):
        code, o, _ = self.run_c7(["add", A, B, C, "--apply"],
                                 [ok("/acme/a"), (429, {"error": "rate_limited", "message": "slow down"})])
        self.assertEqual(code, 6)
        self.assertEqual(o["notSubmitted"], [B, C])

    def test_bad_key_stops_and_its_url_is_resubmittable(self):
        code, o, _ = self.run_c7(["add", A, B, C, "--apply"], [(401, {"error": "unauthorized", "message": "bad key"})])
        self.assertEqual(code, 3)
        self.assertEqual(len(self.sent), 1)
        self.assertNotIn("failed", o)
        self.assertEqual(o["notSubmitted"], [A, B, C])

    def test_dry_run_lists_every_input(self):
        code, o, _ = self.run_c7(["add", A, C])
        self.assertEqual([r["input"] for r in o["results"]], [A, C])
        self.assertEqual(self.sent, [])

    def test_git_token_goes_to_repositories_only(self):
        os.environ["C7_TEST_TOKEN"] = "token-value"
        self.addCleanup(os.environ.pop, "C7_TEST_TOKEN")
        self.run_c7(["add", A, C, "--git-token-env", "C7_TEST_TOKEN", "--apply"],
                    [ok("/acme/a"), ok("/websites/acme_dev")])
        self.assertEqual(self.sent[0][2]["gitToken"], "token-value")
        self.assertNotIn("gitToken", self.sent[1][2])


class TestAddValidation(C7TestCase):
    def assert_refused(self, argv):
        code, _, _ = self.run_c7(argv)
        self.assertEqual(code, 2)
        self.assertEqual(self.sent, [])

    def test_base_url_needs_one_url(self):
        self.assert_refused(["add", A, C, "--base-url", C, "--apply"])

    def test_type_needs_one_url(self):
        self.assert_refused(["add", A, C, "--type", "website", "--apply"])

    def test_queue_timeout_needs_queue(self):
        self.assert_refused(["add", A, "--queue-timeout", "10", "--apply"])

    def test_negative_queue_timeout(self):
        self.assert_refused(["add", A, "--queue", "--queue-timeout", "-5", "--apply"])

    def test_one_bad_input_stops_the_batch(self):
        self.assert_refused(["add", A, "https://acme.dev/llms-api.txt", "--apply"])


class TestAddInterrupted(C7TestCase):
    def test_partial_summary_exit_130(self):
        code, o, err = self.run_c7(["add", A, B, C, "--apply"], [ok("/acme/a"), KeyboardInterrupt()])
        self.assertEqual(code, 130)
        self.assertTrue(o["interrupted"])
        self.assertEqual(o["results"][0]["libraryId"], "/acme/a")
        self.assertEqual(o["notSubmitted"], [B, C])

    def test_single_url(self):
        code, o, _ = self.run_c7(["add", A, "--apply"], [KeyboardInterrupt()])
        self.assertEqual((code, o["notSubmitted"]), (130, [A]))

    def test_130_wins_over_earlier_refusal(self):
        code, o, _ = self.run_c7(["add", A, B, "--apply"],
                                 [(403, {"error": "access_denied", "message": "x"}), KeyboardInterrupt()])
        self.assertEqual(code, 130)
        self.assertEqual((o["failed"], o["notSubmitted"]), ([A], [B]))

    def test_duplicates_kept_by_position(self):
        _, o, _ = self.run_c7(["add", A, A, "--apply"], [ok("/acme/a"), KeyboardInterrupt()])
        self.assertEqual(o["notSubmitted"], [A])

    def test_sigterm_handler_restored(self):
        self.run_c7(["add", A, "--apply"], [ok("/acme/a")])
        self.assertIs(signal.getsignal(signal.SIGTERM), self.sigterm)


class TestUpload(C7TestCase):
    def test_small_file_dry_run(self):
        path = self.make_file("small.pdf", data=b"%PDF-1.4\n" + b"0" * 1000)
        code, o, _ = self.run_c7(["upload", path, "--title", "T"])
        self.assertEqual(code, 0)
        self.assertNotIn("sizeWarning", o)
        self.assertEqual(o["fields"], {"projectTitle": "T"})
        self.assertIn("file name", o["note"])
        self.assertEqual(self.sent, [])

    def test_upload_ok(self):
        path = self.make_file("small.pdf", data=b"%PDF-1.4\n")
        code, o, _ = self.run_c7(["upload", path, "--apply"], [(200, {"libraryName": "/pdf/x-small.pdf-y"})])
        self.assertEqual((code, o["libraryId"]), (0, "/pdf/x-small.pdf-y"))
        self.assertEqual(self.logged[0][0], "upload")

    def test_openapi_uses_library_title(self):
        path = self.make_file("spec.yaml", data=b"openapi: 3.0.0\n")
        _, o, _ = self.run_c7(["upload", path, "--title", "API"])
        self.assertIn("openapi-upload", o["would"])
        self.assertEqual(o["fields"], {"libraryTitle": "API"})

    def test_big_file_warns_and_refuses(self):
        path = self.make_file("big.pdf", size=5_000_000)
        _, o, _ = self.run_c7(["upload", path])
        self.assertIn("qpdf", o["sizeWarning"])
        code, _, err = self.run_c7(["upload", path, "--apply"])
        self.assertEqual(code, 2)
        self.assertEqual(self.sent, [])
        self.assertIn("--skip-size-check", err)

    def test_size_shown_in_bytes_just_over_the_limit(self):
        path = self.make_file("just.pdf", size=4_400_001)
        _, o, _ = self.run_c7(["upload", path])
        self.assertIn("4,400,001 bytes", o["sizeWarning"])

    def test_docx_hint_has_no_qpdf(self):
        path = self.make_file("big.docx", size=4_450_000)
        _, o, _ = self.run_c7(["upload", path])
        self.assertNotIn("qpdf", o["sizeWarning"])

    def test_413_explained(self):
        path = self.make_file("big.pdf", size=5_000_000)
        code, o, _ = self.run_c7(["upload", path, "--apply", "--skip-size-check"],
                                 [(413, "Request Entity Too Large\n\nFUNCTION_PAYLOAD_TOO_LARGE\n")])
        self.assertEqual(code, 2)
        self.assertIn("Split", o["meaning"])

    def test_network_error_small_file_has_no_size_advice(self):
        path = self.make_file("small.pdf", data=b"%PDF-1.4\n")
        code, o, _ = self.run_c7(["upload", path, "--apply"], [(0, {"error": "network", "message": "reset"})])
        self.assertEqual(code, 1)
        self.assertNotIn("4.5 MB", o["meaning"])

    def test_network_error_large_file_mentions_size(self):
        path = self.make_file("mid.pdf", size=3_500_000)
        _, o, _ = self.run_c7(["upload", path, "--apply"], [(0, {"error": "network", "message": "reset"})])
        self.assertIn("4.5 MB", o["meaning"])

    def test_unsupported_type(self):
        path = self.make_file("notes.txt", data=b"x")
        code, _, _ = self.run_c7(["upload", path])
        self.assertEqual(code, 2)


class TestCall(unittest.TestCase):
    def setUp(self):
        self.c7 = load_c7()
        self.c7.api_key = lambda: "test-key"
        self.addCleanup(setattr, urllib.request, "urlopen", urllib.request.urlopen)

    def call_with(self, exc):
        def raiser(*a, **k):
            raise exc
        urllib.request.urlopen = raiser
        return self.c7.call("POST", "/v2/add/pdf-upload", form=(b"x", "multipart/form-data; boundary=b"))

    def test_network_failures_return_json(self):
        import http.client
        import socket
        for exc in (ConnectionResetError(54, "reset"), socket.timeout("timed out"),
                    http.client.RemoteDisconnected("closed"), http.client.IncompleteRead(b"x"),
                    BrokenPipeError(32, "Broken pipe"), urllib.error.URLError("no route")):
            with self.subTest(exc=type(exc).__name__):
                status, data = self.call_with(exc)
                self.assertEqual((status, data["error"]), (0, "network"))

    def test_error_body_cut_off_keeps_status(self):
        class CutBody:
            def read(self, *a):
                raise ConnectionResetError(54, "reset")

            def close(self):
                pass

        status, data = self.call_with(urllib.error.HTTPError("https://context7.com/api/x", 413, "Too Large", {}, CutBody()))
        self.assertEqual((status, data), (413, ""))


class TestLog(unittest.TestCase):
    def test_write_failure_only_warns(self):
        c7 = load_c7()
        c7.LOG_PATH = os.path.join(tempfile.gettempdir(), "no-such-dir-c7-test", "registry.jsonl")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            c7.log("add", libraryId="/acme/a")
        self.assertIn("could not write", err.getvalue())

    def test_empty_log_prints_nothing_on_stdout(self):
        c7 = load_c7()
        c7.LOG_PATH = os.path.join(tempfile.gettempdir(), "no-such-dir-c7-test", "registry.jsonl")
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            c7.cmd_log(None)
        self.assertEqual(out.getvalue(), "")


class TestHelp(C7TestCase):
    def test_every_command_has_help(self):
        for name in ("whoami", "check", "allow", "add", "upload", "refresh", "wait", "log"):
            with self.subTest(command=name):
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    import sys
                    old, sys.argv = sys.argv, ["c7.py", name, "--help"]
                    try:
                        self.c7.main()
                    except SystemExit as e:
                        self.assertEqual(e.code, 0)
                    finally:
                        sys.argv = old
                self.assertIn("Exit codes", out.getvalue())


if __name__ == "__main__":
    unittest.main()
