# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

"""Tests for the Journeys engine (hrms/hr/journeys.py): owner resolution, lazy dependency opening,
progress/status, cancellation, idempotent starts and permissions.

Plain unittest, rolled back, and deliberately without hrms.tests.utils / erpnext.tests.utils (importing
either bootstraps and commits a large amount of fixture data into whatever site it runs on — see the
shared brief). Field names used to build fixtures below are verified against the real DocType JSON:
- Employee: first_name, company, date_of_joining, status, user_id, reports_to, department, designation
  (hrms/../frappe-bench field probe against the live site's Employee meta).
- User: email, first_name, send_welcome_email, roles (hrms-open Employee/User metas).
- Journey Template / Journey Template Step / Journey / Journey Task: this repo,
  hrms/hr/doctype/journey_template/journey_template.json etc. (written in this change).
"""

import unittest
from unittest.mock import patch

import frappe
from frappe.utils import add_days, getdate, nowdate

from hrms.hr import journeys


class JourneysTestCase(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		frappe.set_user("Administrator")
		cls.company = (
			frappe.db.get_single_value("Global Defaults", "default_company")
			or frappe.get_all("Company", pluck="name", limit_page_length=1)[0]
		)

	def setUp(self):
		frappe.set_user("Administrator")
		frappe.flags.mute_emails = True
		self._uid_counter = 0
		# a test must never persist anything: any attempt to commit fails loudly instead of silently
		# leaving data behind
		self._commit_patcher = patch.object(
			frappe.db, "commit", side_effect=AssertionError("test tried to commit")
		)
		self._commit_patcher.start()

	def tearDown(self):
		self._commit_patcher.stop()
		frappe.db.rollback()
		frappe.set_user("Administrator")
		frappe.flags.mute_emails = False

	def _uid(self) -> str:
		self._uid_counter += 1
		return f"{self._uid_counter}{frappe.generate_hash(length=6)}"

	# --- fixtures ---

	def make_employee(self, **fields):
		uid = self._uid()
		fields.setdefault("first_name", f"Journeys Test {uid}")
		fields.setdefault("company", self.company)
		fields.setdefault("date_of_joining", nowdate())
		fields.setdefault("status", "Active")
		fields.setdefault("gender", "Female")
		fields.setdefault("date_of_birth", add_days(nowdate(), -365 * 25))
		return frappe.get_doc({"doctype": "Employee", **fields}).insert(ignore_permissions=True)

	def make_user(self, roles=None, **fields):
		uid = self._uid()
		fields.setdefault("email", f"journeys-test-{uid}@example.com")
		fields.setdefault("first_name", f"Journeys User {uid}")
		fields.setdefault("send_welcome_email", 0)
		fields.setdefault("roles", [{"role": role} for role in (roles or [])])
		doc = frappe.get_doc({"doctype": "User", **fields})
		doc.flags.no_welcome_mail = True
		doc.insert(ignore_permissions=True)
		return doc

	def make_template(self, steps, **fields):
		uid = self._uid()
		fields.setdefault("title", f"Journeys Test Template {uid}")
		fields.setdefault("journey_type", "Custom")
		fields.setdefault("trigger", "Manual")
		fields.setdefault("company", self.company)
		return frappe.get_doc({"doctype": "Journey Template", "steps": steps, **fields}).insert(
			ignore_permissions=True
		)


class TestOwnerResolution(JourneysTestCase):
	def test_employee_owner(self):
		user = self.make_user()
		employee = self.make_employee(user_id=user.name)
		resolved, unresolved = journeys.resolve_owner(employee, "Employee", None, None)
		self.assertEqual(resolved, user.name)
		self.assertFalse(unresolved)

	def test_employee_owner_falls_back_when_no_user_linked(self):
		hr_user = self.make_user(roles=["HR Manager"])
		employee = self.make_employee()  # no user_id
		# the real "HR Manager" role's population on the dev site is not ours to depend on (it may hold
		# a demo organisation's real HR users, or none) — patch the HR queue so the fallback is deterministic
		with patch.object(journeys, "get_hr_queue_users", return_value=[hr_user.name]):
			resolved, unresolved = journeys.resolve_owner(employee, "Employee", None, None)
		self.assertEqual(resolved, hr_user.name)
		self.assertTrue(unresolved)

	def test_managers_manager_owner(self):
		manager_user = self.make_user()
		manager = self.make_employee(user_id=manager_user.name)
		employee = self.make_employee(reports_to=manager.name)
		resolved, unresolved = journeys.resolve_owner(employee, "Employee's Manager", None, None)
		self.assertEqual(resolved, manager_user.name)
		self.assertFalse(unresolved)

	def test_manager_owner_unresolved_without_reports_to(self):
		hr_user = self.make_user()
		employee = self.make_employee()  # no reports_to
		with patch.object(journeys, "get_hr_queue_users", return_value=[hr_user.name]):
			resolved, unresolved = journeys.resolve_owner(employee, "Employee's Manager", None, None)
		self.assertEqual(resolved, hr_user.name)
		self.assertTrue(unresolved)

	def test_hr_owner_picks_first_from_the_queue(self):
		queue_user = self.make_user()
		employee = self.make_employee()
		# resolve_owner's "HR" branch is just "ask the queue, take the front" — the queue's own ordering
		# is proven for real (against the database, not a mock) below in test_role_owner_picks_alphabetically
		with patch.object(journeys, "get_hr_queue_users", return_value=[queue_user.name, "someone-else"]):
			resolved, unresolved = journeys.resolve_owner(employee, "HR", None, None)
		self.assertEqual(resolved, queue_user.name)
		self.assertFalse(unresolved)

	def test_role_owner(self):
		role_name = f"Journeys Test Role {self._uid()}"
		frappe.get_doc({"doctype": "Role", "role_name": role_name, "desk_access": 1}).insert(
			ignore_permissions=True
		)
		role_user = self.make_user(roles=[role_name])
		employee = self.make_employee()
		resolved, unresolved = journeys.resolve_owner(employee, "Role", role_name, None)
		self.assertEqual(resolved, role_user.name)
		self.assertFalse(unresolved)

	def test_role_owner_picks_alphabetically(self):
		# a fresh Role has no pre-existing holders, so ordering is deterministic without mocking anything —
		# this is the real query (frappe.qb against Has Role/User) that get_hr_queue_users also uses
		role_name = f"Journeys Test Order Role {self._uid()}"
		frappe.get_doc({"doctype": "Role", "role_name": role_name, "desk_access": 1}).insert(
			ignore_permissions=True
		)
		self.make_user(roles=[role_name], email=f"journeys-zz-{self._uid()}@example.com")
		first = self.make_user(roles=[role_name], email=f"journeys-aa-{self._uid()}@example.com")
		employee = self.make_employee()
		resolved, unresolved = journeys.resolve_owner(employee, "Role", role_name, None)
		self.assertEqual(resolved, first.name)
		self.assertFalse(unresolved)

	def test_role_owner_unresolved_when_role_empty(self):
		hr_user = self.make_user()
		employee = self.make_employee()
		role_name = f"Journeys Test Empty Role {self._uid()}"
		frappe.get_doc({"doctype": "Role", "role_name": role_name, "desk_access": 1}).insert(
			ignore_permissions=True
		)
		with patch.object(journeys, "get_hr_queue_users", return_value=[hr_user.name]):
			resolved, unresolved = journeys.resolve_owner(employee, "Role", role_name, None)
		self.assertEqual(resolved, hr_user.name)
		self.assertTrue(unresolved)

	def test_user_owner(self):
		configured = self.make_user()
		employee = self.make_employee()
		resolved, unresolved = journeys.resolve_owner(employee, "User", None, configured.name)
		self.assertEqual(resolved, configured.name)
		self.assertFalse(unresolved)

	def test_disabled_configured_user_is_unresolved(self):
		hr_user = self.make_user()
		configured = self.make_user(enabled=0)
		employee = self.make_employee()
		with patch.object(journeys, "get_hr_queue_users", return_value=[hr_user.name]):
			resolved, unresolved = journeys.resolve_owner(employee, "User", None, configured.name)
		self.assertEqual(resolved, hr_user.name)
		self.assertTrue(unresolved)

	def test_unresolved_with_no_hr_queue_returns_none(self):
		employee = self.make_employee()
		with patch.object(journeys, "get_hr_queue_users", return_value=[]):
			resolved, unresolved = journeys.resolve_owner(employee, "User", None, None)
		self.assertIsNone(resolved)
		self.assertTrue(unresolved)


class TestStartJourney(JourneysTestCase):
	def _hr_user(self):
		user = self.make_user(roles=["HR Manager"])
		frappe.set_user(user.name)
		return user

	def test_start_opens_only_ready_steps_and_creates_todo(self):
		owner = self.make_user()
		employee = self.make_employee(user_id=owner.name)
		template = self.make_template(
			[
				{"title": "Step A", "owner_type": "Employee", "due_offset_days": 0, "action": "Task"},
				{
					"title": "Step B",
					"owner_type": "Employee",
					"due_offset_days": 1,
					"depends_on": "Step A",
					"action": "Task",
				},
			]
		)
		self._hr_user()
		journey_name = journeys.start_journey(template=template.name, employee=employee.name)
		journey = frappe.get_doc("Journey", journey_name)

		by_title = {t.title: t for t in journey.tasks}
		self.assertEqual(by_title["Step A"].status, "Open")
		self.assertEqual(by_title["Step A"].owner_user, owner.name)
		self.assertTrue(by_title["Step A"].todo)
		self.assertEqual(by_title["Step B"].status, "Blocked")
		self.assertFalse(by_title["Step B"].owner_user)
		self.assertFalse(by_title["Step B"].todo)
		self.assertEqual(journey.status, "In Progress")

		todo = frappe.get_doc("ToDo", by_title["Step A"].todo)
		self.assertEqual(todo.reference_type, "Journey")
		self.assertEqual(todo.reference_name, journey.name)
		self.assertEqual(todo.allocated_to, owner.name)
		self.assertEqual(todo.status, "Open")

	def test_non_hr_cannot_start_journey_manually(self):
		employee = self.make_employee()
		template = self.make_template([{"title": "Step A", "owner_type": "HR", "action": "Task"}])
		random_user = self.make_user()
		frappe.set_user(random_user.name)
		with self.assertRaises(journeys.JourneyPermissionError):
			journeys.start_journey(template=template.name, employee=employee.name)

	def test_start_from_trigger_is_idempotent(self):
		employee = self.make_employee()
		template = self.make_template([{"title": "Step A", "owner_type": "HR", "action": "Task"}])

		first = journeys.start_journey(
			template=template.name,
			employee=employee.name,
			triggered_by_doctype="Employee",
			triggered_by=employee.name,
		)
		second = journeys.start_journey(
			template=template.name,
			employee=employee.name,
			triggered_by_doctype="Employee",
			triggered_by=employee.name,
		)
		self.assertEqual(first, second)
		self.assertEqual(
			frappe.db.count(
				"Journey",
				{"journey_template": template.name, "employee": employee.name, "status": ("!=", "Cancelled")},
			),
			1,
		)

	def test_disabled_template_cannot_be_started(self):
		self._hr_user()
		employee = self.make_employee()
		template = self.make_template([{"title": "Step A", "owner_type": "HR", "action": "Task"}], disabled=1)
		with self.assertRaises(frappe.ValidationError):
			journeys.start_journey(template=template.name, employee=employee.name)

	def test_zero_step_template_completes_immediately(self):
		self._hr_user()
		employee = self.make_employee()
		template = self.make_template([])
		journey_name = journeys.start_journey(template=template.name, employee=employee.name)
		journey = frappe.get_doc("Journey", journey_name)
		self.assertEqual(journey.status, "Completed")
		self.assertEqual(journey.progress_percent, 100)

	def test_due_date_uses_offset_from_start_date(self):
		self._hr_user()
		employee = self.make_employee()
		template = self.make_template(
			[{"title": "Prep", "owner_type": "HR", "due_offset_days": -3, "action": "Task"}]
		)
		start_date = add_days(nowdate(), 10)
		journey_name = journeys.start_journey(
			template=template.name, employee=employee.name, start_date=start_date
		)
		journey = frappe.get_doc("Journey", journey_name)
		self.assertEqual(getdate(journey.tasks[0].due_date), getdate(add_days(start_date, -3)))


class TestTaskTransitions(JourneysTestCase):
	def _start(self, steps, owner_user=None):
		hr_user = self.make_user(roles=["HR Manager"])
		employee = self.make_employee(user_id=owner_user.name if owner_user else None)
		template = self.make_template(steps)
		frappe.set_user(hr_user.name)
		journey_name = journeys.start_journey(template=template.name, employee=employee.name)
		frappe.set_user("Administrator")
		return frappe.get_doc("Journey", journey_name), hr_user, employee

	def test_owner_can_complete_their_own_task(self):
		owner = self.make_user()
		journey, _hr, _employee = self._start(
			[{"title": "Step A", "owner_type": "Employee", "action": "Task"}], owner_user=owner
		)
		task = journey.tasks[0]
		frappe.set_user(owner.name)
		result = journeys.complete_task(journey.name, task.name)
		self.assertEqual(result["status"], "Completed")

		journey.reload()
		self.assertEqual(journey.tasks[0].status, "Done")
		self.assertEqual(journey.tasks[0].completed_by, owner.name)
		todo = frappe.get_doc("ToDo", journey.tasks[0].todo)
		self.assertEqual(todo.status, "Closed")

	def test_stranger_cannot_complete_someone_elses_task(self):
		owner = self.make_user()
		journey, _hr, _employee = self._start(
			[{"title": "Step A", "owner_type": "Employee", "action": "Task"}], owner_user=owner
		)
		task = journey.tasks[0]
		stranger = self.make_user()
		frappe.set_user(stranger.name)
		with self.assertRaises(journeys.JourneyPermissionError):
			journeys.complete_task(journey.name, task.name)

	def test_hr_can_complete_any_task(self):
		owner = self.make_user()
		journey, hr_user, _employee = self._start(
			[{"title": "Step A", "owner_type": "Employee", "action": "Task"}], owner_user=owner
		)
		task = journey.tasks[0]
		frappe.set_user(hr_user.name)
		result = journeys.complete_task(journey.name, task.name)
		self.assertEqual(result["status"], "Completed")

	def test_blocked_task_cannot_be_completed(self):
		journey, hr_user, _employee = self._start(
			[
				{"title": "Step A", "owner_type": "HR", "action": "Task"},
				{"title": "Step B", "owner_type": "HR", "depends_on": "Step A", "action": "Task"},
			]
		)
		blocked_task = next(t for t in journey.tasks if t.title == "Step B")
		frappe.set_user(hr_user.name)
		with self.assertRaises(frappe.ValidationError):
			journeys.complete_task(journey.name, blocked_task.name)

	def test_completing_a_task_opens_its_dependent(self):
		journey, hr_user, _employee = self._start(
			[
				{"title": "Step A", "owner_type": "HR", "action": "Task"},
				{"title": "Step B", "owner_type": "HR", "depends_on": "Step A", "action": "Task"},
			]
		)
		step_a = next(t for t in journey.tasks if t.title == "Step A")
		frappe.set_user(hr_user.name)
		journeys.complete_task(journey.name, step_a.name)

		journey.reload()
		step_b = next(t for t in journey.tasks if t.title == "Step B")
		self.assertEqual(step_b.status, "Open")
		self.assertTrue(step_b.todo)
		self.assertEqual(journey.status, "In Progress")
		self.assertAlmostEqual(journey.progress_percent, 50, places=2)

	def test_skip_counts_toward_progress_and_completion(self):
		journey, hr_user, _employee = self._start(
			[
				{"title": "Step A", "owner_type": "HR", "action": "Task"},
				{"title": "Step B", "owner_type": "HR", "depends_on": "Step A", "action": "Task"},
			]
		)
		step_a = next(t for t in journey.tasks if t.title == "Step A")
		frappe.set_user(hr_user.name)
		journeys.skip_task(journey.name, step_a.name, note="not needed")

		journey.reload()
		step_b = next(t for t in journey.tasks if t.title == "Step B")
		self.assertEqual(step_b.status, "Open")  # Skipped also satisfies a dependency

		journeys.complete_task(journey.name, step_b.name)
		journey.reload()
		self.assertEqual(journey.status, "Completed")
		self.assertEqual(journey.progress_percent, 100)


class TestCancelJourney(JourneysTestCase):
	def test_cancel_closes_open_todos_and_blocks_further_transitions(self):
		hr_user = self.make_user(roles=["HR Manager"])
		employee = self.make_employee()
		template = self.make_template([{"title": "Step A", "owner_type": "HR", "action": "Task"}])
		frappe.set_user(hr_user.name)
		journey_name = journeys.start_journey(template=template.name, employee=employee.name)
		journey = frappe.get_doc("Journey", journey_name)
		todo_name = journey.tasks[0].todo

		result = journeys.cancel_journey(journey_name)
		self.assertEqual(result["status"], "Cancelled")

		todo = frappe.get_doc("ToDo", todo_name)
		self.assertEqual(todo.status, "Cancelled")

		journey.reload()
		with self.assertRaises(frappe.ValidationError):
			journeys.complete_task(journey_name, journey.tasks[0].name)

	def test_non_hr_cannot_cancel(self):
		hr_user = self.make_user(roles=["HR Manager"])
		employee = self.make_employee()
		template = self.make_template([{"title": "Step A", "owner_type": "HR", "action": "Task"}])
		frappe.set_user(hr_user.name)
		journey_name = journeys.start_journey(template=template.name, employee=employee.name)

		random_user = self.make_user()
		frappe.set_user(random_user.name)
		with self.assertRaises(journeys.JourneyPermissionError):
			journeys.cancel_journey(journey_name)


class TestGetEmployeeJourneys(JourneysTestCase):
	def test_permission_denied_for_unrelated_user(self):
		employee = self.make_employee()
		stranger = self.make_user()
		frappe.set_user(stranger.name)
		with self.assertRaises(frappe.PermissionError):
			journeys.get_employee_journeys(employee.name)

	def test_hr_sees_active_journeys_with_next_task(self):
		hr_user = self.make_user(roles=["HR Manager"])
		employee = self.make_employee()
		template = self.make_template(
			[
				{"title": "Step A", "owner_type": "HR", "action": "Task"},
				{"title": "Step B", "owner_type": "HR", "depends_on": "Step A", "action": "Task"},
			]
		)
		frappe.set_user(hr_user.name)
		journey_name = journeys.start_journey(template=template.name, employee=employee.name)

		result = journeys.get_employee_journeys(employee.name)
		self.assertEqual(len(result), 1)
		self.assertEqual(result[0]["name"], journey_name)
		self.assertEqual(result[0]["next_task"]["title"], "Step A")


class TestBuildTemplateFromBoardingTemplate(JourneysTestCase):
	def test_builds_steps_from_onboarding_activities(self):
		hr_user = self.make_user(roles=["HR Manager"])
		role_holder = self.make_user(roles=["Employee"])
		source = frappe.get_doc(
			{
				"doctype": "Employee Onboarding Template",
				"title": f"Journeys Test Boarding {self._uid()}",
				"company": self.company,
				"activities": [
					{"activity_name": "Collect laptop", "role": "Employee", "begin_on": 0, "duration": 1},
					{
						"activity_name": "Sign paperwork",
						"user": role_holder.name,
						"begin_on": 1,
						"required_for_employee_creation": 1,
					},
					{"activity_name": "Orientation", "begin_on": 2},
				],
			}
		).insert(ignore_permissions=True)

		frappe.set_user(hr_user.name)
		template_name = journeys.build_template_from_boarding_template(
			"Employee Onboarding Template", source.name
		)
		template = frappe.get_doc("Journey Template", template_name)

		self.assertEqual(template.journey_type, "Onboarding")
		self.assertEqual(template.trigger, "Employee Joined")
		by_title = {s.title: s for s in template.steps}
		self.assertEqual(by_title["Collect laptop"].owner_type, "Role")
		self.assertEqual(by_title["Collect laptop"].owner_role, "Employee")
		self.assertEqual(by_title["Sign paperwork"].owner_type, "User")
		self.assertEqual(by_title["Sign paperwork"].owner_user, role_holder.name)
		self.assertEqual(by_title["Sign paperwork"].required, 1)
		self.assertEqual(by_title["Orientation"].owner_type, "HR")

	def test_only_hr_can_build_a_template(self):
		source = frappe.get_doc(
			{
				"doctype": "Employee Onboarding Template",
				"title": f"Journeys Test Boarding {self._uid()}",
				"company": self.company,
				"activities": [{"activity_name": "Collect laptop", "begin_on": 0}],
			}
		).insert(ignore_permissions=True)

		random_user = self.make_user()
		frappe.set_user(random_user.name)
		with self.assertRaises(journeys.JourneyPermissionError):
			journeys.build_template_from_boarding_template("Employee Onboarding Template", source.name)


class TestWhitelistedTypeAnnotations(JourneysTestCase):
	"""hooks.py sets require_type_annotated_api_methods: every argument of every @frappe.whitelist()
	function must have a type annotation, or a real browser request (which sends everything as strings)
	gets HTTP 417 even though a direct Python call would look fine. See the shared brief, rule 1."""

	def test_whitelisted_functions_accept_string_arguments(self):
		from frappe.utils.typing_validations import validate_argument_types

		hr_user = self.make_user(roles=["HR Manager"])
		employee = self.make_employee()
		template = self.make_template([{"title": "Step A", "owner_type": "HR", "action": "Task"}])
		frappe.set_user(hr_user.name)

		wrapped_start = validate_argument_types(
			journeys.start_journey, apply_condition=lambda: True, force_types=True
		)
		journey_name = wrapped_start(template=template.name, employee=employee.name, start_date=None)
		self.assertTrue(frappe.db.exists("Journey", journey_name))

		journey = frappe.get_doc("Journey", journey_name)
		task_name = journey.tasks[0].name

		wrapped_complete = validate_argument_types(
			journeys.complete_task, apply_condition=lambda: True, force_types=True
		)
		wrapped_complete(journey=journey_name, task=task_name, note=None)

		wrapped_get = validate_argument_types(
			journeys.get_employee_journeys, apply_condition=lambda: True, force_types=True
		)
		# a browser sends booleans as the string "0"/"1", never a Python bool
		wrapped_get(employee=employee.name, include_completed="0")

		wrapped_cancel = validate_argument_types(
			journeys.cancel_journey, apply_condition=lambda: True, force_types=True
		)
		journey_name_2 = journeys.start_journey(template=template.name, employee=employee.name)
		wrapped_cancel(journey=journey_name_2, reason=None)


if __name__ == "__main__":
	unittest.main()
