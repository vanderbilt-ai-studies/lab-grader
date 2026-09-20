"""Synthetic-only tests. Never opens the real course's private directory."""
import copy
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile

import lab_pipeline as lp

TEST_TMP = str(Path(tempfile.gettempdir()).resolve())


def config(scope="synthetic-course"):
    return {"schema": 1, "host": "example.invalid", "course_scope": scope, "le_version": "1.93",
            "test_course": True, "attempt_policy": "latest",
            "sections": [{"org_unit_id": "100", "folder_id": "200", "course_name": "Synthetic Course",
                          "assignment_name": "Synthetic Lab"}],
            "criteria": [{"id": "explanation", "label": "Explanation", "possible": 10}]}


class FakeAPI:
    def __init__(self):
        self.folder = {"Id": 200, "Name": "Synthetic Lab", "GroupTypeId": None, "DropboxType": 2,
                       "Assessment": {"ScoreDenominator": 10}, "GradeItemId": 300}
        self.people = [{"Identifier": "401", "FirstName": "Zyra", "LastName": "Valenwood",
                        "DisplayName": "Zyra Valenwood", "Username": "zvalen", "OrgDefinedId": "9000401",
                        "Email": "zyra@example.invalid"},
                       {"Identifier": "402", "FirstName": "Quillon", "LastName": "Emberfall",
                        "DisplayName": "Quillon Emberfall", "Username": "qember",
                        "Email": "quillon@example.invalid"}]
        self.data = b"Zyra Valenwood\nI compared the outputs. zyra@example.invalid\nWith Quillon Emberfall."
        self.entities = [{"Entity": {"EntityId": 401, "EntityType": "User", "DisplayName": "Zyra Valenwood"},
                          "Submissions": [{"Id": 501, "SubmissionDate": "2026-09-10T00:00:00Z",
                                           "Comment": {"Text": "My work", "Html": None},
                                           "Files": [{"FileId": 601, "FileName": "Zyra-Valenwood.txt",
                                                      "Size": len(self.data)}]}]}]
        self.feedback = None
        self.posts = 0
        self.timeout_after_write = False
        self.drop_feedback_text = False
        self.grade_override = None

    def collection(self, path):
        return copy.deepcopy(self.people if "/classlist/" in path else self.entities)

    def get(self, path, missing=False, binary=False):
        if binary:
            return self.data
        if "/courses/" in path:
            return {"Name": "Synthetic Course"}
        if "/feedback/" in path:
            return copy.deepcopy(self.feedback)
        if "/grades/" in path:
            if self.grade_override is not None:
                return {"PointsNumerator": self.grade_override}
            return {"PointsNumerator": self.feedback["Score"] if self.feedback and self.feedback["IsGraded"] else None}
        if "/submissions/user/" in path:
            return copy.deepcopy(self.entities[0])
        return copy.deepcopy(self.folder)

    def post(self, path, payload):
        self.posts += 1
        self.feedback = {**copy.deepcopy(payload), "Files": [], "Links": []}
        if self.drop_feedback_text:
            self.feedback["Feedback"] = {"Text": "", "Html": ""}
        if self.timeout_after_write:
            raise TimeoutError("synthetic sensitive error: Zyra Valenwood")


class PipelineTests(unittest.TestCase):
    def test_inflected_feedback_words_do_not_remove_full_name_check(self):
        redactor=lp.Redactor({'synthetic':{'FirstName':'Zyra','LastName':'Marks'}},'synthetic','S-'+'a'*32)
        redactor.check_result('The log marks correctness for each row.')
        with self.assertRaisesRegex(lp.Stop,'RESULT_NEEDS_REDACTION_REVIEW'):
            redactor.check_result('Zyra Marks completed the report.')

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="lab-grader-test-", dir=TEST_TMP)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        lp.save(self.root / "labs/lab-one/grading-config.json", config())
        self.pipe = lp.Pipeline(self.root, "lab-one")
        self.api = FakeAPI()

    def ready(self):
        self.pipe.download(self.api)
        self.pipe.prepare()
        self.record = self.pipe.records()[0]
        self.key = self.record["key"]
        packet = lp.read_json(self.pipe.released / self.key / "packet.json")
        self.result = {"student_key": self.key, "package_digest": packet["package_digest"], "status": "complete",
                       "criteria": [{"id": "explanation", "earned": 8, "evidence": "The comparison includes two outputs.",
                                     "deduction_reason": "The rubric requires an explanation of why they differ."}],
                       "score": 8, "feedback": "Next time, explain why the outputs differ. Review Day 2, Context section."}
        self.write_result()

    def write_result(self):
        lp.save(self.pipe.results / self.key / "result.json", self.result)

    def test_end_to_end_redaction_upload_and_ta_export(self):
        self.ready()
        packet = lp.read_json(self.pipe.released / self.key / "packet.json")
        self.assertIn(self.key, packet["submission"])
        self.assertIn("[PERSON REDACTED]", packet["submission"])
        self.pipe.validate()
        self.pipe.export_review()
        with zipfile.ZipFile(self.pipe.labroot / "grading/ta-review.zip") as archive:
            exported = b"".join(archive.read(n) for n in archive.namelist()).decode()
            for term in ("Zyra", "Valenwood", "Quillon", "Emberfall", "zvalen", "@example.invalid"):
                self.assertNotIn(term, exported)
        self.pipe.plan_upload(self.api)
        self.pipe.upload(self.api)
        self.assertFalse(self.api.feedback["IsGraded"])
        self.assertIn("Points deducted: 2", self.api.feedback["Feedback"]["Text"])
        self.pipe.plan_upload(self.api, publish=True)
        self.pipe.upload(self.api)
        self.assertTrue(self.api.feedback["IsGraded"])
        self.assertEqual(self.api.feedback["Score"], 8)
        self.assertTrue((self.pipe.private / "verified-feedback-hosts.json").exists())
        self.pipe.upload(self.api)
        self.assertEqual(self.api.posts, 2)

    def test_ids_persist_across_labs_and_retries(self):
        self.ready()
        first_packet = lp.read_json(self.pipe.released / self.key / "packet.json")
        self.pipe.download(self.api)
        self.pipe.prepare()
        self.assertEqual(self.pipe.records()[0]["key"], self.key)
        self.assertEqual(lp.read_json(self.pipe.released / self.key / "packet.json"), first_packet)
        lp.save(self.root / "labs/lab-two/grading-config.json", config())
        second = lp.Pipeline(self.root, "lab-two")
        second.download(self.api)
        self.assertEqual(second.records()[0]["key"], self.key)

    def test_release_text_checks_exact_bytes_without_newline_translation(self):
        self.ready()
        self.pipe.release(self.record, "Synthetic line\rsecond line\r\nthird line", "automatic")
        packet=lp.read_json(self.pipe.released / self.key / "packet.json")
        self.result["package_digest"]=packet["package_digest"]
        self.write_result()
        self.pipe.validate()
        lp.save(self.pipe.released / self.key / "submission.txt", packet["submission"].replace("\r", "").encode())
        with self.assertRaisesRegex(lp.Stop,"RELEASE_CHANGED"):
            self.pipe.validate()

    def test_new_course_has_different_id(self):
        self.ready()
        lp.save(self.root / "labs/lab-two/grading-config.json", config("another-course"))
        second = lp.Pipeline(self.root, "lab-two")
        second.download(self.api)
        self.assertNotEqual(second.records()[0]["key"], self.key)

    def test_provisional_review_export_does_not_enable_upload(self):
        self.ready()
        self.result["status"] = "needs_review"
        self.write_result()
        with self.assertRaisesRegex(lp.Stop, "PROVISIONAL_RESULT"):
            self.pipe.validate()
        self.pipe.export_review(include_provisional=True)
        html = (self.pipe.results / self.key / "feedback.html").read_text()
        self.assertIn("PROVISIONAL", html)
        with self.assertRaisesRegex(lp.Stop, "PROVISIONAL_RESULT"):
            self.pipe.plan_upload(self.api)
        self.assertEqual(self.api.posts, 0)

    def test_generated_feedback_allows_ordinary_prose_but_not_full_names(self):
        redactor = lp.Redactor({"1": {"FirstName": "You", "LastName": "Valenwood"}}, "1", "S-"+"a"*32)
        redactor.check_result("You can check the result.")
        with self.assertRaisesRegex(lp.Stop, "RESULT_NEEDS_REDACTION_REVIEW"):
            redactor.check_result("You Valenwood scored eight points.")
        cleaned, _ = redactor.clean("You Valenwood")
        self.assertNotIn("Valenwood", cleaned)

    def test_visual_hash_change_blocks_review_and_upload(self):
        self.ready()
        path = self.pipe.state / "releases" / (self.key + ".json")
        release = lp.read_json(path)
        packet = release["packet"]
        packet["visuals"] = [{"path":"page-001.png", "sha256":lp.digest(b"synthetic-image")}]
        packet["package_digest"] = lp.digest({k:v for k,v in packet.items() if k != "package_digest"})
        lp.save(path,release)
        lp.save(self.pipe.released / self.key / "packet.json",packet)
        lp.save(self.pipe.released / self.key / "page-001.png",b"changed-image")
        self.result["package_digest"] = packet["package_digest"]
        self.write_result()
        with self.assertRaisesRegex(lp.Stop,"RELEASE_CHANGED"):
            self.pipe.export_review(include_provisional=True)

    def test_missing_roster_identity_is_held_without_blocking_download(self):
        self.api.people = self.api.people[1:]
        self.assertEqual(self.pipe.download(self.api), {"downloaded": 1})
        self.assertEqual(self.pipe.prepare(), {"released": 0, "held": 1})
        record = self.pipe.records()[0]
        self.assertFalse(record["identity_verified"])
        self.assertEqual(self.pipe.status()["hold_reasons"], {"IDENTITY_LOOKUP_INCOMPLETE": 1})
        lp.save(self.pipe.state / "review" / record["key"] / "submission.txt", b"Reviewed text")
        with self.assertRaisesRegex(lp.Stop, "IDENTITY_LOOKUP_INCOMPLETE"):
            self.pipe.release_reviewed(record["key"])
        # A retry must not treat envelope display-name aliases as verified identity.
        self.pipe.download(self.api)
        self.assertEqual(self.pipe.prepare(), {"released": 0, "held": 1})

    def test_roster_resolution_preserves_key_and_allows_release(self):
        person = self.api.people.pop(0)
        self.pipe.download(self.api)
        key = self.pipe.records()[0]["key"]
        self.pipe.prepare()
        self.api.people.append(person)
        self.pipe.download(self.api)
        self.assertEqual(self.pipe.records()[0]["key"], key)
        self.assertEqual(self.pipe.prepare(), {"released": 1, "held": 0})
        self.assertEqual(self.pipe.status()["hold_reasons"], {})

    def test_redaction_boundaries_case_variants_and_unicode(self):
        redactor = lp.Redactor({"401": self.api.people[0]}, "401", "S-" + "a" * 32)
        text, _ = redactor.clean("ZYRA\u200b VALENWOOD, zvalen; Valenwoodian. 9000401\nwww.example.invalid/account")
        self.assertNotIn("ZYRA", text)
        self.assertNotIn("9000401", text)
        self.assertIn("Valenwoodian", text)
        self.assertNotIn("www.", text)

    def test_overlapping_names_do_not_map_peer_to_owner(self):
        people = {"401": self.api.people[0], "402": {**self.api.people[1], "LastName": "Valenwood"}}
        text, _ = lp.Redactor(people, "401", "S-" + "a" * 32).clean("Valenwood")
        self.assertEqual(text, "[PERSON REDACTED]")

    def test_unsupported_file_held_and_manual_release(self):
        self.api.entities[0]["Submissions"][0]["Files"][0]["FileName"] = "private-photo.png"
        self.pipe.download(self.api)
        self.assertEqual(self.pipe.prepare(), {"released": 0, "held": 1})
        record = self.pipe.records()[0]
        key = record["key"]
        self.assertFalse((self.pipe.released / key / "packet.json").exists())
        lp.save(self.pipe.state / "review" / key / "submission.txt", b"Human transcribed the complete evidence.")
        self.pipe.release_reviewed(key)
        self.assertTrue((self.pipe.released / key / "packet.json").exists())

    def test_incomplete_download_cannot_restore_old_release(self):
        self.ready()
        self.api.entities[0]["Submissions"][0]["Files"][0]["Size"] += 1
        with self.assertRaisesRegex(lp.Stop, "FILE_SIZE_MISMATCH"):
            self.pipe.download(self.api)
        self.assertFalse(self.pipe.released.exists())
        with self.assertRaisesRegex(lp.Stop, "DOWNLOAD_INCOMPLETE"):
            self.pipe.prepare()

    def test_new_submission_rejects_result_before_upload(self):
        self.ready()
        self.pipe.plan_upload(self.api)
        new = copy.deepcopy(self.api.entities[0]["Submissions"][0])
        new.update(Id=502, SubmissionDate="2026-09-11T00:00:00Z")
        self.api.entities[0]["Submissions"].append(new)
        with self.assertRaisesRegex(lp.Stop, "NEW_SUBMISSION_DOWNLOAD_AGAIN"):
            self.pipe.upload(self.api)
        self.assertEqual(self.api.posts, 0)

    def test_redownload_new_attempt_invalidates_old_grade(self):
        self.ready()
        self.api.entities[0]["Submissions"][0]["SubmissionDate"] = "2026-09-12T00:00:00Z"
        self.pipe.download(self.api)
        self.pipe.prepare()
        with self.assertRaisesRegex(lp.Stop, "RESULT_IDENTITY_MISMATCH"):
            self.pipe.validate()

    def test_missing_justification_is_rejected(self):
        self.ready()
        self.result["criteria"][0]["deduction_reason"] = ""
        self.write_result()
        with self.assertRaisesRegex(lp.Stop, "MISSING_DEDUCTION_REASON"):
            self.pipe.validate()

    def test_wrong_sum_nan_and_unknown_criteria_rejected(self):
        self.ready()
        self.result["score"] = 9
        self.write_result()
        with self.assertRaisesRegex(lp.Stop, "SCORE_SUM_MISMATCH"):
            self.pipe.validate()
        self.result["score"] = 8
        self.result["criteria"][0]["id"] = "invented"
        self.write_result()
        with self.assertRaisesRegex(lp.Stop, "RESULT_RUBRIC_MISMATCH"):
            self.pipe.validate()
        with self.assertRaises(lp.Stop):
            lp.number(float("nan"))
        with self.assertRaises(lp.Stop):
            lp.number(True)

    def test_result_identity_and_extra_fields_rejected(self):
        self.ready()
        self.result["extra"] = "Zyra Valenwood"
        self.write_result()
        with self.assertRaisesRegex(lp.Stop, "RESULT_SCHEMA_MISMATCH"):
            self.pipe.validate()
        del self.result["extra"]
        self.result["feedback"] = "Zyra, try again."
        self.write_result()
        with self.assertRaisesRegex(lp.Stop, "RESULT_NEEDS_REDACTION_REVIEW"):
            self.pipe.validate()

    def test_feedback_html_is_escaped_and_note_urls_allowed(self):
        self.ready()
        self.result["feedback"] += ' See https://course.example.invalid/day-2. <script>alert(1)</script>'
        self.write_result()
        self.pipe.validate()
        html = (self.pipe.results / self.key / "feedback.html").read_text()
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)

    def test_changed_result_and_packet_rejected(self):
        self.ready()
        self.pipe.plan_upload(self.api)
        self.result["feedback"] += " Another suggestion."
        self.write_result()
        with self.assertRaisesRegex(lp.Stop, "RESULT_CHANGED_REPLAN"):
            self.pipe.upload(self.api)
        packet = lp.read_json(self.pipe.released / self.key / "packet.json")
        packet["submission"] = "Changed"
        lp.save(self.pipe.released / self.key / "packet.json", packet)
        with self.assertRaisesRegex(lp.Stop, "RELEASE_CHANGED"):
            self.pipe.validate()

    def test_existing_feedback_is_not_overwritten(self):
        self.ready()
        self.api.feedback = {"Score": 9, "Feedback": {"Text": "Instructor comment"}, "IsGraded": True}
        with self.assertRaisesRegex(lp.Stop, "EXISTING_FEEDBACK_CONFLICT"):
            self.pipe.plan_upload(self.api)
        self.assertEqual(self.api.posts, 0)

    def test_concurrent_feedback_change_is_not_overwritten(self):
        self.ready()
        self.pipe.plan_upload(self.api)
        self.api.feedback = {"Score": 9, "Feedback": {"Text": "New comment"}, "IsGraded": False}
        with self.assertRaisesRegex(lp.Stop, "FEEDBACK_CHANGED_REPLAN"):
            self.pipe.upload(self.api)
        self.assertEqual(self.api.posts, 0)

    def test_independent_gradebook_value_is_not_overwritten(self):
        self.ready()
        self.api.grade_override = 9
        with self.assertRaisesRegex(lp.Stop, "EXISTING_GRADE_CONFLICT"):
            self.pipe.plan_upload(self.api, publish=True)
        self.assertEqual(self.api.posts, 0)

    def test_gradebook_change_after_plan_stops_upload(self):
        self.ready()
        self.pipe.plan_upload(self.api, publish=True)
        self.api.grade_override = 9
        with self.assertRaisesRegex(lp.Stop, "GRADE_CHANGED_REPLAN"):
            self.pipe.upload(self.api)
        self.assertEqual(self.api.posts, 0)

    def test_gradebook_readback_must_match_after_publication(self):
        self.ready()
        self.pipe.plan_upload(self.api, publish=True)
        original_post = self.api.post
        def wrong_grade(path, payload):
            original_post(path, payload)
            self.api.grade_override = 9
        self.api.post = wrong_grade
        with self.assertRaisesRegex(lp.Stop, "GRADE_READBACK_FAILED"):
            self.pipe.upload(self.api)
        self.assertFalse((self.pipe.private / "verified-feedback-hosts.json").exists())

    def test_timeout_after_save_reconciles_without_duplicate_post(self):
        self.ready()
        self.pipe.plan_upload(self.api)
        self.api.timeout_after_write = True
        with self.assertRaises(TimeoutError):
            self.pipe.upload(self.api)
        self.pipe.upload(self.api)
        self.assertEqual(self.api.posts, 1)

    def test_silent_feedback_loss_stops_and_does_not_retry(self):
        self.ready()
        self.pipe.plan_upload(self.api)
        self.api.drop_feedback_text = True
        with self.assertRaisesRegex(lp.Stop, "FEEDBACK_READBACK_FAILED"):
            self.pipe.upload(self.api)
        with self.assertRaisesRegex(lp.Stop, "UNCERTAIN_WRITE_REVIEW_REQUIRED"):
            self.pipe.upload(self.api)
        self.assertEqual(self.api.posts, 1)

    def test_production_requires_test_course_round_trip(self):
        self.ready()
        self.pipe.config["test_course"] = False
        # Write profile before a fresh download so all digests bind the new config.
        lp.save(self.pipe.labroot / "grading-config.json", self.pipe.config)
        self.ready()
        self.pipe.plan_upload(self.api)
        with self.assertRaisesRegex(lp.Stop, "LIVE_FEEDBACK_TEST_REQUIRED"):
            self.pipe.upload(self.api)

    def test_wrong_course_group_and_denominator_stop(self):
        self.api.folder["GroupTypeId"] = 77
        with self.assertRaisesRegex(lp.Stop, "GROUP_WORK_UNSUPPORTED"):
            self.pipe.download(self.api)
        self.api.folder["GroupTypeId"] = None
        self.api.folder["Assessment"]["ScoreDenominator"] = 20
        with self.assertRaisesRegex(lp.Stop, "RUBRIC_TOTAL_MISMATCH"):
            self.pipe.download(self.api)

    def test_config_path_and_symlinks_refused(self):
        with self.assertRaisesRegex(lp.Stop, "INVALID_LAB"):
            lp.Pipeline(self.root, "../elsewhere")
        self.ready()
        result_path = self.pipe.results / self.key / "result.json"
        result_path.unlink()
        result_path.symlink_to(self.pipe.mapping)
        with self.assertRaisesRegex(lp.Stop, "SYMLINK_REFUSED"):
            self.pipe.validate()

    def test_latest_attempt_includes_all_its_files(self):
        old = copy.deepcopy(self.api.entities[0]["Submissions"][0])
        old.update(Id=500, SubmissionDate="2026-09-09T00:00:00Z")
        self.api.entities[0]["Submissions"].insert(0, old)
        self.api.entities[0]["Submissions"][1]["Files"].append(
            {"FileId": 602, "FileName": "second.txt", "Size": len(self.api.data)})
        self.pipe.download(self.api)
        record = self.pipe.records()[0]
        self.assertEqual(len(record["files"]), 3)
        self.pipe.prepare()
        text = (self.pipe.released / record["key"] / "submission.txt").read_text()
        self.assertIn("file 2", text)
        self.assertNotIn("Attempt 2", text)

    def test_private_permissions(self):
        self.ready()
        self.assertEqual(self.pipe.mapping.stat().st_mode & 0o777, 0o600)

    def test_lock_prevents_second_writer(self):
        path = self.root / "pipeline.lock"
        with lp.lock(path):
            with self.assertRaisesRegex(lp.Stop, "BUSY"):
                with lp.lock(path):
                    pass


class ExtractionTests(unittest.TestCase):
    def test_docx_text_ignores_metadata_and_holds_images(self):
        doc = io.BytesIO()
        with zipfile.ZipFile(doc, "w") as archive:
            archive.writestr("docProps/core.xml", "Secret author metadata")
            archive.writestr("word/document.xml", '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Required evidence.</w:t></w:r></w:p></w:body></w:document>')
        self.assertEqual(lp.extract(doc.getvalue(), ".docx"), "Required evidence.")
        with zipfile.ZipFile(doc, "a") as archive:
            archive.writestr("word/media/image1.png", b"image")
        with self.assertRaisesRegex(lp.Stop, "VISUAL_REVIEW_REQUIRED"):
            lp.extract(doc.getvalue(), ".docx")

    def test_html_never_exports_scripts_links_or_image(self):
        text = lp.extract(b'<p>Work</p><script>secret()</script><a href="https://secret.invalid">Evidence</a>', ".html")
        self.assertNotIn("secret", text)
        with self.assertRaisesRegex(lp.Stop, "VISUAL_REVIEW_REQUIRED"):
            lp.extract(b'<p>Work</p><img src="private.png">', ".html")

    @unittest.skipUnless(importlib.util.find_spec("fitz"), "PyMuPDF optional dependency")
    def test_pdf_text_and_scanned_hold(self):
        import fitz
        document = fitz.open()
        document.new_page().insert_text((72, 72), "Synthetic evidence.")
        document.set_metadata({"author": "Hidden identity"})
        self.assertIn("Synthetic evidence", lp.extract(document.tobytes(), ".pdf"))
        blank = fitz.open()
        blank.new_page()
        with self.assertRaisesRegex(lp.Stop, "OCR_REVIEW_REQUIRED"):
            lp.extract(blank.tobytes(), ".pdf")

    def test_markdown_images_and_binary_text_held(self):
        with self.assertRaises(lp.Stop):
            lp.extract(b"![screenshot](private.png)", ".md")
        with self.assertRaises(lp.Stop):
            lp.extract(b"\x00bad", ".txt")


class TransportTests(unittest.TestCase):
    def test_both_pagination_shapes(self):
        api = object.__new__(lp.Brightspace)
        api.host = "example.invalid"
        pages = iter([{"Items": [1], "PagingInfo": {"HasMoreItems": True, "Bookmark": "next"}},
                      {"Items": [2], "PagingInfo": {"HasMoreItems": False}}])
        api.get = lambda path: next(pages)
        self.assertEqual(api.collection("/d2l/api/test"), [1, 2])
        pages = iter([{"Objects": [1], "Next": "/d2l/api/next"}, {"Objects": [2], "Next": None}])
        self.assertEqual(api.collection("/d2l/api/test"), [1, 2])

    def test_foreign_pagination_cannot_send_token(self):
        api = object.__new__(lp.Brightspace)
        api.host = "example.invalid"
        with self.assertRaisesRegex(lp.Stop, "API_DESTINATION_REFUSED"):
            api.get("https://evil.invalid/d2l/api/test")

    def test_cli_diagnostics_hide_input_and_exception(self):
        result = subprocess.run([sys.executable, str(Path(lp.__file__)), "prepare", "--lab", "Zyra-Valenwood"],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("Zyra", result.stdout + result.stderr)
        self.assertNotIn("Traceback", result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)["code"], "INVALID_LAB")

    def test_native_and_python_diagnostics_are_suppressed(self):
        with tempfile.TemporaryDirectory(dir=TEST_TMP) as temporary:
            script = Path(temporary) / "synthetic-diagnostics.py"
            script.write_text(
                "import sys, os\n"
                + "sys.path.insert(0, " + repr(str(Path(lp.__file__).parent)) + ")\n"
                + "import lab_pipeline as lp\n"
                + "def fake(argv):\n"
                + "    print('Zyra Valenwood')\n"
                + "    os.write(2, b'Quillon Emberfall')\n"
                + "    raise ValueError('zyra@example.invalid')\n"
                + "lp.main = fake\n"
                + "sys.exit(lp.safe_main([]))\n")
            result = subprocess.run([sys.executable, str(script)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 1)
            self.assertEqual(json.loads(result.stdout)["code"], "LOCAL_OPERATION_FAILED")
            self.assertNotIn("Zyra", result.stdout + result.stderr)
            self.assertNotIn("Quillon", result.stdout + result.stderr)
            self.assertNotIn("@", result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
