# lab-grader

A small, editable AI grading skill. It uses an assignment, rubric, instructor guidance, submissions, and course notes to prepare proposed grades and useful feedback.

Parallel grading workers use Sol at medium reasoning in Codex, or an available Sonnet model with medium thinking where supported. A coordinator checks deductions, totals, consistency, and note references. Model availability depends on the host environment; the skill does not install a model or agent runner.

## Use it

Install this folder as a skill in your agent environment. For Codex, clone it into `~/.codex/skills/lab-grader`, or symlink that location to your editable checkout. Then request:

> Use $lab-grader to grade these lab submissions. The assignment is at [path], rubric at [path], instructor guidance at [path], and course notes at [path]. Submissions are in [private folder]. Save proposed grades and feedback in [private output folder].

Each student gets rubric scores, a reason for every deduction, and specific suggestions for the next lab. When a submission reveals a conceptual misunderstanding, feedback includes a short explanation, worked example, practice check, and a verified notes reference by day and section. The instructor reviews the results before release.

## Edit it

- [SKILL.md](SKILL.md) contains the grading process and feedback format. Start here to change how the skill behaves.
- [guidance-template.md](guidance-template.md) is a starting point for assignment-specific instructor guidance. Keep completed copies with private course materials.

## Student suggestions

Students are welcome to suggest changes through GitHub issues or pull requests. Tell us which instruction you would change, suggest wording, and explain how it would make feedback clearer, fairer, or more useful. Synthetic examples are welcome.

Please keep names, submissions, grades, and individual grading disputes out of this repository and its issues. Use the course's private channel for questions about your own grade.
