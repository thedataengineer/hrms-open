# Skills Cloud and Talent Marketplace

## What it is

Hubble learns what skills an employee actually has from the real work already recorded about
them - performance feedback, goals, completed training, logged timesheets - instead of asking
everyone to fill out a skills form from scratch. Every suggestion shows the exact sentence it came
from, so a person can always see *why* Hubble thinks they know something, not just be told they do.

The same skill data powers a small internal marketplace: open roles, projects, mentoring and
stretch assignments that an employee can browse, see a match % and the reasons for it, and express
interest in with one click. Opportunity owners and HR see who is interested, plus a list of other
people who look like a good fit.

No language model is involved. A skill is only ever suggested because its name or one of its
aliases appears as a whole word in a real sentence.

## How a person uses it

- **Employee form -> Skills section.** Shows the employee's confirmed skills, any new suggestions
  (each with its evidence sentence, source, and Accept / Dismiss buttons), and gaps against their
  own designation. A "Check for new suggestions" button runs the on-demand scan for just that
  employee. Accepting a suggestion adds it to their Employee Skill Map at a default level of 3
  out of 5; dismissing it means Hubble never suggests that skill for them again from any source.
- **Talent Marketplace page** (`/app/talent-marketplace`). "Opportunities for you" lists open
  opportunities with a match %, the reasons behind it, and an Express interest / Withdraw button.
  "For owners" lets someone who posted an opportunity pick it from a dropdown and see two lists:
  who has actually expressed interest, and who the matcher suggests but who hasn't applied yet.
- **Skill Gap Analysis report** (HR / manager / self). Employees whose own designation calls for a
  skill they don't have yet, grouped and counted, with a chart of the worst gaps.

## How it's built

- **DocTypes:** `Skill Suggestion` (one per piece of evidence: employee, skill, 0-100 confidence,
  the evidence sentence, the source document, status), `Talent Opportunity` (+ child tables
  `Talent Opportunity Skill` for required/nice-to-have skills and minimum proficiency, and
  `Talent Opportunity Interest`, visible only to the opportunity owner and HR via `permlevel`).
  The existing `Skill` doctype gained an `aliases` field (comma separated, matched the same way as
  the skill's own name).
- **Engine:** `hrms/hr/skills_cloud.py`. `refresh_suggestions()` reads four evidence sources -
  submitted Employee Performance Feedback, Goal, submitted Training Result, submitted Timesheet -
  each in one bounded, incremental query (past a watermark on `modified`, capped at a batch size),
  compiles the whole Skill + alias catalogue into a single word-boundary regex once per run, and
  scores each hit as `source_weight x recency_factor` (recency halves every 180 days). A suggestion
  is never created for a skill the employee already has or has already reviewed, and one employee
  can only accumulate so many pending suggestions before the scan moves on. `match_opportunities`
  and `match_people` score by skill overlap x proficiency (0.75 x required-skill coverage + 0.25 x
  nice-to-have coverage, each skill's credit capped at meeting its minimum proficiency); `match_people`
  never loops over every employee - it starts from the opportunity's own short skill list and only
  touches the employees who already show up against it.
- **Client:** `hrms/public/js/skills/employee_skills.js` (Employee form section, registered as its
  own `frappe.ui.form.on("Employee", ...)` - does not edit `employee.js`) and
  `hrms/hr/page/talent_marketplace/` (the marketplace Page). Styling in
  `hrms/public/scss/_skills.scss`.
- **Permissions:** `Skill Suggestion` and the `interests` section of `Talent Opportunity` grant no
  DocType-level access to the Employee role at all; every employee-facing read or write goes
  through a whitelisted function in `skills_cloud.py` that checks access itself (self, their
  manager at any level via the Employee nested set, or HR) and then reads/writes directly - the
  same "controller, not a wider DocPerm" shape `leave_application.py` already uses.
- **Scheduling:** `refresh_suggestions()` itself never commits, so it's safe to call from a test;
  `scheduled_refresh_suggestions()` is the thin wrapper that calls it and commits, meant for the
  scheduler (see the lead's `hooks.py` addition).

## Limits

- The watermark used for incremental scanning lives in `frappe.cache()`, not a database field: this
  feature had no Settings DocType in its file scope. Losing the cache just means the next run
  rescans from the oldest un-processed evidence in bounded batches again - the
  (employee, skill, source, source document) uniqueness guard on `Skill Suggestion` means that
  never creates a duplicate, just occasionally does a bit more work.
- Gap analysis against a `Designation` (or a `Job Opening`, via its designation) can only ever
  report a skill as **missing**: `Designation Skill` has no minimum-proficiency field, so there is
  nothing to fall short of. Gaps against a `Talent Opportunity` can be missing or a proficiency
  shortfall, because `Talent Opportunity Skill` does carry a minimum proficiency.
- The word matcher is deterministic text matching, not an understanding of the text: it will not
  infer a skill that is never actually named, and two overlapping skill names (e.g. "React" and
  "React Native") resolve to whichever is longer at that exact position in the sentence.
- Matching and listing endpoints are all bounded and paginated (opportunity scans, candidate scans,
  suggestion counts per employee); nothing loops per-employee inside a request.
