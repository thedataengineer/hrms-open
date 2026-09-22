# Focus aids in Hubble

RTB has a **Focus Mode** (Alt+A, or the brain button) for people who find pages of forms and options hard:
banners that say what a date means today, lists whose overdue rows glow, drafts that survive an interruption,
a Smart Inbox of what is due, and one-thing-at-a-time pages. Hubble takes part in it in three ways.

## What it is

- **Hubble's own Focus pages and cards** (the four HR tickets from RTB's Focus backlog):
  - *Onboard New Employee* (`/desk/employee-onboarding-wizard`): one screen at a time; nothing is created
    until the review step is confirmed, then everything is created in one go or not at all.
  - *Payroll Cycle Checklist* (`/desk/focus-payroll-checklist`): the six real steps of a payroll month,
    detected from the documents themselves.
  - *Balance at a glance* on a Leave Application: entitlement, used, remaining and this application's days,
    coloured by the same rule the form applies on save.
  - *Receipt check* on an Expense Claim: how many expense rows still lack a receipt.
- **Hubble's forms and lists in RTB's general aids** (`hrms/public/js/adhd/adhd_focus_registrations.js`):
  - deadline banners on Leave Application (the first day of leave still waiting for approval), Interview,
    Appraisal, Payroll Entry, Employee Onboarding and Training Event;
  - a time-box banner on Salary Structure, Payroll Entry, Shift Type, Appraisal Cycle and Leave Policy;
  - list heat by date on Leave Application, Interview, Job Opening, Shift Request and Payroll Entry, with the
    statuses that end the deadline (an approved leave, a cleared interview, a submitted payroll);
  - Hubble's workspaces in the Smart Inbox's "most visited" ranking;
  - draft recovery for an Expense Claim (its expense rows) and a Leave Application (the chosen leave type);
  - a switch in Focus Settings, *Leave Balance and Receipt Checks*, that the two form cards read.
- **What waits on the person, in RTB's Smart Inbox** (`hrms/hr/focus.py`): leave applications and expense
  claims waiting on the signed-in approver whose date has come, and pending interviews the person conducts
  today or earlier.

## How a person uses it

Switch Focus Mode on in RTB. The pages are reachable by their routes (and are meant to get workspace
shortcuts); the banners, list colours, drafts and inbox rows appear on their own. Every switch in Focus
Settings takes effect on the form on screen at once. Nothing here blocks a save or a submit, and nothing
writes to the server from a form aid: the wizard and the checklist are the only pages that create documents,
and only when asked.

## How it is built

- RTB exposes seven registration functions on `erpnext.adhd` (`registerDeadline`, `registerTimebox`,
  `registerListDate`, `registerInboxModule`, `registerDraftRecovery`, `registerFeature` and
  `registerChain`). `adhd_focus_registrations.js` calls them at load with plain configuration and does
  nothing on an RTB that lacks one of them. Its test reads the doctype JSON files, so a renamed field fails
  the test before it fails on a form.
- RTB's inbox asks every app for rows through the `focus_urgent_items` hook; `hrms.hr.focus.urgent_items`
  answers with bounded rows read under the caller's own permissions (`frappe.get_list`), carrying only a
  name, a type and a date, never anyone's reason text.
- The document chain navigator has no HR chain on purpose: it marks a step done when a submitted document is
  linked, and Hubble's hiring flow is mostly unsubmitted records while payroll's next steps are actions on
  the Payroll Entry, not new documents.

## Limits

- The list heat needs a `status` field to know a deadline is over, so Appraisal, Attendance Request and
  Compensatory Leave Request (which have none) are not in the heat map.
- The Employee Onboarding banner reads `boarding_status`, so a cancelled onboarding is silent but a
  completed one that was never submitted is not.
- Tests: `node --test hrms/tests/*.test.js` for the client side and the rollback-only Python suites
  `hrms/tests/test_adhd_*.py` and `hrms/tests/test_focus_urgent_items.py` for the server side.
