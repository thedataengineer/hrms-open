# Journeys

## What it is

A Journey is a guided sequence for a life event at work — onboarding, offboarding, a promotion, a
transfer, coming back from leave, or a custom event you define — built from a reusable **Journey
Template**. Instead of one flat checklist owned by one person, a Journey's steps can each belong to a
different owner (the employee, their manager, HR, a role, or a named person), and a step only becomes
visible to its owner once whatever it depends on is done. Nobody sees the whole list of everything that
could eventually happen to them; they see what is next.

## How a person uses it

- On an **Employee's** form, a "Journeys" panel shows that employee's active journeys: a progress bar and
  the next open step for each. HR can click **Start a journey** to pick a template that applies to this
  employee (matched by company, and by department/designation when the template restricts to one) and a
  start date.
- Onboarding, offboarding, a submitted promotion or a submitted transfer can also start a journey
  automatically, if a template says so (its **Trigger**) — see "Automatic starts" below.
- A **Journey**'s own form shows a board: **Now** (open — act on these), **Next** (one step away), **Later**
  (further out) and **Done**. Each open task has a one-click **Mark done**, and a quieter **Skip**. The
  full task table is still there underneath, collapsed, for anyone who wants every row or wants to export
  it.
- Whoever a step is assigned to gets a normal Frappe assignment (their **ToDo** list, and the desk's
  assignment bell) — nothing is emailed. If a template turns on **Send Reminders**, an overdue open task
  also gets an in-app notification (Notification Log), at most once a day, until it is done or skipped.

## How it is built

- **DocTypes** (`hrms/hr/doctype/`): `Journey Template` (+ child `Journey Template Step`) is the reusable
  definition; `Journey` (+ child `Journey Task`) is one running instance for one employee. Journey Task
  keeps enough of its originating step (owner type/role/configured user, the dependency's title) to resolve
  its real owner lazily, when it actually opens — not when the journey starts.
- **Engine** (`hrms/hr/journeys.py`) is the only place that mutates a Journey: `start_journey`,
  `complete_task`, `skip_task`, `cancel_journey`, the daily `send_overdue_reminders`, and the event handlers
  below. Every one of them checks the caller's own permission; none of it depends on a wide DocType
  permission for "Employee" (see "Permissions" below).
- **Dependencies** are matched by step **title** within one template (validated: unique titles, no cycles,
  no dangling reference) rather than a hidden id, so a template stays readable as JSON and in the grid.
- **Owner resolution** always picks exactly one person, never several — a step assigned to "HR" or a "Role"
  picks one enabled holder of that role, deterministically (alphabetically first), rather than fanning the
  same ToDo out to everyone who could plausibly do it. When nobody can be resolved (no manager set, an
  empty role, a disabled user), the step still opens — routed to the first person in the HR queue
  (enabled users holding **HR Manager**) — and is flagged `owner_unresolved` so it is visible, never
  silently dropped.
- **Automatic starts**: `on_employee_after_insert` (Employee Joined), and `on_submit` handlers for
  Employee Separation, Employee Promotion and Employee Transfer. Each looks up enabled, matching templates
  and calls `start_journey` with `triggered_by_doctype`/`triggered_by` set, which makes it idempotent (a
  second call for the same trigger document returns the existing Journey instead of creating a duplicate).
  A bad template can never block the document that triggered it: the handler catches its own exceptions
  and logs them (`frappe.log_error`) rather than failing the employee's save or the promotion's submit.
  "Return from Leave" and "Custom" have no natural single triggering document in this codebase, so they are
  Manual-only today (started from the "Start a journey" dialog, or by calling `start_journey` directly) —
  a deliberate scope decision, not an oversight.
- **A helper** builds a Journey Template from an existing **Employee Onboarding Template** or **Employee
  Separation Template**, copying each Boarding Activity into a step (its `user`/`role` becomes a User/Role
  owner, otherwise HR; `begin_on` becomes the due offset; `required_for_employee_creation` becomes
  Required).

## Permissions

`Journey` and `Journey Template` grant DocType permission only to HR Manager, HR User and System Manager —
never to "Employee". An employee's own read access to one specific Journey comes from the ordinary Frappe
assignment mechanism: when `frappe.desk.form.assign_to.add` gives someone a ToDo for a document they could
not already read, it shares that one document with them (`frappe.share`). That is what "self-service
through the controller, not by widening DocPerm" means in practice here — every whitelisted function
(`get_employee_journeys`, `complete_task`, `skip_task`, ...) still checks the caller explicitly (the
Employee form section calls `frappe.has_permission("Employee", "read", ...)`; completing or skipping a
task requires being that task's resolved owner, HR, or a System Manager).

## Scale

- Every list is bounded: applicable templates, an employee's journeys, HR/role lookups for owner
  resolution are all capped (`limit_page_length`, typically 10-20 rows) — nothing here scans every
  employee or every journey in one request.
- Starting a journey does exactly two writes (`insert`, then `save` after opening the first ready tasks) —
  not one query per step.
- The daily reminder job (`send_overdue_reminders`) is a single bounded, indexed join (Journey Task status
  + due date, joined to its Journey's status and its Journey Template's `send_reminders`), capped at 500
  rows per run and ordered oldest-due-first. Each task's own `last_reminder_sent_on` field is the
  watermark: a run only considers tasks not yet reminded today, so a big backlog is worked down over
  several days instead of starving newly-overdue tasks, and nothing is ever reminded twice in one day.

## Limits

- A step's "depends on" is a single dependency (not an arbitrary AND/OR of several steps).
- If the same person happens to already own another open task on the same Journey, Frappe's own assignment
  de-duplication means a second task for them reuses that one ToDo rather than creating a second row; both
  tasks still show correctly on the board and both can be completed independently, but closing one ToDo
  early (by completing the first task) does not remove the person's assignment bell entry until the second
  task is also resolved.
- HR or a System Manager can, in principle, edit a Journey's task table directly (desk grid) instead of
  using Mark done / Skip. Doing so is honored, but it will not cascade to open dependents or recompute
  progress until the next `complete_task`, `skip_task`, `cancel_journey` call or the next daily job touches
  that Journey — use the board's buttons to keep everything in sync.
- Reminders are in-app only (Notification Log) by design; nothing here ever sends email.
