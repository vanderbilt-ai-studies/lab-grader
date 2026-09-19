# Local lab grading pipeline

Implemented September 19, 2026; tested with synthetic records and a simulated
Brightspace API. **No real submissions processed and no live feedback write
verified yet.** This implements mitigation with some residual identification
risk, as requested. It does not certify that all identifying information is gone.

## What runs where

`scripts/lab_pipeline.py` is ordinary Python. It handles Brightspace identity and
raw files locally, prints only counts/fixed status codes, and gives agents newly
generated text packages. The grading itself uses the `lab-grader` skill and its
parallel Sol/medium or Sonnet workers. The Python pipeline does not call a model.

One random key identifies a student across all labs with the same `course_scope`
and tenant. Both course sections should use the same scope. A new semester should
use a new scope. Keys persist across downloads and grading revisions. Back up the
private mapping securely; deleting it breaks continuity with prior reviews.

```text
FERPA-sensitive/lab-grading/
  courses/<course-scope>/identities.json   persistent key-to-account mapping
  labs/<lab>/raw/                         original downloads and API envelopes
  labs/<lab>/review/                      held text and fixed-code reasons
  labs/<lab>/releases/                    exact release and revision records
  labs/<lab>/receipts/                    private upload receipts
labs/<lab>/
  grading-config.json                    course/assignment IDs and rubric points
  submissions/anonymous/<key>/
    packet.json                          authoritative grading input
    submission.txt                       convenient text view
  grading/results/<key>/result.json       agent or TA output
  grading/results/<key>/feedback.html     generated full feedback
  grading/ta-review.zip                   pseudonymous review package for TAs
```

Only the code, instructions, and configuration are versioned. All student-derived
content, including anonymous packages, feedback, and TA archives, is gitignored.
Use a course-approved private channel to share the TA archive. Sharing the archive
does not share the identity mapping. No send/share operation is automated here.

## Setup once per assignment

Use macOS/Linux with Python 3.10+ and `requests`; `PyMuPDF` adds PDF text extraction. The current
course Python environment already has both. The script must live in the course's
`ferpa-safe/scripts/`: its private root is derived from that location, never passed
as a CLI argument. Brightspace uses the existing installed `brightspace-course`
client's own configured authentication. Tokens are never printed or supplied in
command arguments. `AUTH_UNAVAILABLE` means that API login needs renewal; it says
nothing about whether an unrelated browser session is logged in.

Copy `lab-grading-config.example.json` to `labs/<lab>/grading-config.json`. Replace
the example IDs, exact course/assignment names, and criteria with the actual ones.
Add a section entry for each authorized shell; each has its own assignment ID.
Use the published rubric's criterion IDs, labels, and possible points. They are
checked against the assignment's total before download/upload.

Choose `attempt_policy` explicitly: `latest` means all files/comments in the newest
submission event; `all` means all submission events. Use the actual assignment
policy. The downloader preserves every attempt privately in either case. Group
assignments are not supported in this version and stop without grading.

Keep `test_course: false` for real courses. `true` is only for an explicitly
authorized disposable course with synthetic learners and submissions.

## Download and prepare

Run these from the course project, replacing `lab-slug` with the lab folder name:

```sh
python3 ferpa-safe/scripts/lab_pipeline.py check-config --lab lab-slug
python3 ferpa-safe/scripts/lab_pipeline.py download --lab lab-slug
python3 ferpa-safe/scripts/lab_pipeline.py prepare --lab lab-slug
python3 ferpa-safe/scripts/lab_pipeline.py status --lab lab-slug
```

The downloader reads all pages, matches identity by the Brightspace user ID, and
keeps raw records in the private folder. It does not match by name or filename.
It verifies the exact course and assignment names as well as the point total.
Download failure revokes the previous release and prevents preparation from a
partially refreshed snapshot. Retry the read-only download after resolving its
cause; it does not submit grades or mark files read.

Preparation matches known classlist names, name components, usernames, institutional
IDs and email addresses case-insensitively, with escaped literals and word
boundaries. Unicode normalization and whitespace handling catch common formatting
variants. The student's own unambiguous matches become their key. Other/shared
names become `[PERSON REDACTED]`. Generic emails, common phone formats and web
URLs are also removed. Newly generated text contains no source document metadata,
original filenames, submission timestamps, Brightspace user IDs, or active links.

Supported automatic inputs: UTF-8 text/Markdown/CSV/JSON/Python source, text HTML,
text-only DOCX, and text PDFs without images/vector drawings. Student code is
read as text and never executed. Source document formatting is not retained.
Screenshots, image/scanned PDFs, embedded media, tracked DOCX revisions, legacy
Office formats, archives, and extraction failures are **held**, not given zero.
The pipeline does not send anything to remote OCR or another model.

Known tradeoffs: ordinary words that are also student names may be redacted;
unlisted nicknames, unusual phone/identifier formats and identifying narratives
may remain. URLs needed as grading evidence are removed too. The grader must
flag missing evidence for human review rather than penalizing removed content.
Text-only extraction is inappropriate when layout/visual output is graded.

## Held submissions

Only a human opens private originals, `review/<key>/status.json`, and
`review/<key>/submission.txt` locally. Complete the cleaned text using the original
evidence, or grade visual work manually. Do not release a partial transcript as if
it contains all required evidence. Once the text is suitable, the human runs:

```sh
python3 ferpa-safe/scripts/lab_pipeline.py release-reviewed --lab lab-slug --key S-32hexcharacters --human-reviewed
```

The real key has `S-` plus 32 lowercase hexadecimal characters. This command still
runs redaction and binds the release to the current submission revision. Agents
must not claim human review or run this override on their own.

## Grade and review

Give the grading skill only `submissions/anonymous/`, the public assignment/rubric,
instructor guidance and course notes. Start fresh worker contexts; never include
the private mapping, originals, API envelopes, or browser evaluation screens.
Use `packet.json` as the source of the key, package digest and exact criterion IDs.
The skill's `references/pipeline.md` specifies the result format. Put each result at
`labs/<lab>/grading/results/<key>/result.json`.

```sh
python3 ferpa-safe/scripts/lab_pipeline.py validate --lab lab-slug
python3 ferpa-safe/scripts/lab_pipeline.py export-review --lab lab-slug
```

Validation checks package identity, criterion coverage, evidence for each score,
reasons for deductions, arithmetic, completeness, and known identifiers. It
generates both full text and escaped HTML feedback. The TA ZIP contains the
cleaned submission, editable result JSON, and feedback HTML under stable keys.
TAs can return revised result files; replace the corresponding local result JSON,
validate again and create a new upload plan. A changed result invalidates an old
plan. A new submission invalidates an old grade, even if its filename is unchanged.

This is a data-handling boundary, not an OS sandbox. Normal desktop agents may
still have broad filesystem access. The skill prohibits private reads; this
version does not install a container or claim that filesystem isolation exists.

## Return both grade and feedback

After reviewing the results:

```sh
# Read-only plan for saving draft evaluations (no grade/feedback write).
python3 ferpa-safe/scripts/lab_pipeline.py plan-upload --lab lab-slug
# Apply the saved draft plan.
python3 ferpa-safe/scripts/lab_pipeline.py upload --lab lab-slug --execute
# Plan student-visible publication, then apply that plan.
python3 ferpa-safe/scripts/lab_pipeline.py plan-upload --lab lab-slug --publish
python3 ferpa-safe/scripts/lab_pipeline.py upload --lab lab-slug --execute
```

`plan-upload` requires results for all released packets. Held work is excluded;
it is never assigned a zero. `status` reports held and completed counts separately.

The uploader writes the assignment's **Overall Grade** and **Overall Feedback**.
Full rubric breakdown, deduction reasons and teaching feedback are included in
Overall Feedback. The HTML is escaped, so model-generated markup cannot inject
scripts or remote images. Note URLs remain readable as text. This version does
not populate the clickable native Brightspace rubric; the criterion breakdown
appears in Overall Feedback.

Writes stop on an existing evaluation not created by this pipeline, independent
gradebook values, concurrent changes, changed submissions, or changed results.
The uploader verifies the exact feedback text/HTML and score after saving, plus
the linked gradebook score after publication. If the server normalizes the HTML,
verification stops rather than assuming it survived. On uncertain writes, rerun
the same upload to reconcile the receipt; it does not blindly send another POST.
Do not delete receipts to force a retry. Published evaluations are not silently
revised or moved back to draft.

Real-course writes require a successful synthetic publication/grade read-back on
the same tenant. Use a disposable-course config (`test_course: true`) to exercise
download, preparation, a synthetic grading result, draft upload and publication.
The successful test records a private tenant verification receipt. The same
sequence on a simulated API in unit tests does not authorize production writes.
This tenant test remains outstanding; it needs an authorized test assignment and
synthetic learner. Do not label a real course as a test to bypass it.

## Student-facing instruction

> Please omit your name, email address, VUnetID, student ID, and other identifying
> details from your submission and filename. Check screenshots for account names
> or profile information. Brightspace already records who submitted your work.

## Development check

```sh
python3 -m unittest discover -s ferpa-safe/scripts -p test_lab_pipeline.py
```

Tests use generated identities and temporary directories only. Never use real
submissions as public fixtures. See the public lab-grader repository for the
matching portable script and skill instructions.
