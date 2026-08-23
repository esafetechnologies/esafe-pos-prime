from unittest import TestCase
from unittest.mock import Mock, patch

import frappe

from pos_prime.api import orders


class InvoiceStub:
	def __init__(self, items=None, rounded_total=-10, grand_total=-10):
		self.items = items or []
		self.payments = []
		self.rounded_total = rounded_total
		self.grand_total = grand_total
		self.company = "Test Company"
		self.conversion_rate = 1
		self.paid_amount = 999

	def set(self, fieldname, value):
		setattr(self, fieldname, list(value))

	def append(self, fieldname, value):
		row = frappe._dict(value)
		getattr(self, fieldname).append(row)
		return row

	def calculate_taxes_and_totals(self):
		return None


def return_row(source_row, item_code="ITEM-1", qty=-2, serial_no=""):
	return frappe._dict(
		{
			"idx": 1,
			"item_code": item_code,
			"qty": qty,
			"conversion_factor": 1,
			"pos_invoice_item": source_row,
			"serial_no": serial_no,
			"batch_no": "",
		}
	)


class TestReturnItemSelection(TestCase):
	@patch.object(orders, "_get_source_tracking", return_value={})
	def test_duplicate_item_codes_are_selected_by_source_row(self, _get_tracking):
		first = return_row("SOURCE-ROW-1", item_code="DUPLICATE")
		second = return_row("SOURCE-ROW-2", item_code="DUPLICATE")
		invoice = InvoiceStub([first, second])

		orders._apply_return_item_selection(
			invoice,
			[
				{"source_row_name": "SOURCE-ROW-1", "qty": -1},
				{"source_row_name": "SOURCE-ROW-2", "qty": 0},
			],
		)

		self.assertEqual(invoice.items, [first])
		self.assertEqual(first.qty, -1)

	@patch.object(orders, "_get_source_tracking", return_value={})
	def test_omitted_row_is_not_returned(self, _get_tracking):
		first = return_row("SOURCE-ROW-1")
		second = return_row("SOURCE-ROW-2", item_code="ITEM-2")
		invoice = InvoiceStub([first, second])

		orders._apply_return_item_selection(
			invoice,
			[{"source_row_name": "SOURCE-ROW-2", "qty": -1}],
		)

		self.assertEqual(invoice.items, [second])

	def test_duplicate_source_identity_is_rejected(self):
		invoice = InvoiceStub([return_row("SOURCE-ROW-1")])

		with self.assertRaises(frappe.ValidationError):
			orders._apply_return_item_selection(
				invoice,
				[
					{"source_row_name": "SOURCE-ROW-1", "qty": -1},
					{"source_row_name": "SOURCE-ROW-1", "qty": -1},
				],
			)

	def test_unknown_source_identity_is_rejected(self):
		invoice = InvoiceStub([return_row("SOURCE-ROW-1")])

		with self.assertRaises(frappe.ValidationError):
			orders._apply_return_item_selection(
				invoice,
				[{"source_row_name": "NOT-ON-INVOICE", "qty": -1}],
			)

	@patch.object(
		orders,
		"_get_source_tracking",
		return_value={
			"serial_and_batch_bundle": "SABB-1",
			"use_serial_batch_fields": 0,
		},
	)
	def test_bundle_only_row_rejects_partial_return(self, _get_tracking):
		invoice = InvoiceStub([return_row("SOURCE-ROW-1")])

		with self.assertRaises(frappe.ValidationError):
			orders._apply_return_item_selection(
				invoice,
				[{"source_row_name": "SOURCE-ROW-1", "qty": -1}],
			)

	def test_legacy_serial_subset_matches_return_quantity(self):
		row = return_row("SOURCE-ROW-1", qty=-2, serial_no="SERIAL-1\nSERIAL-2")

		orders._validate_serial_selection(
			row,
			{"serial_no": "SERIAL-2"},
			-1,
		)

		self.assertEqual(row.serial_no, "SERIAL-2")

	def test_serial_outside_source_row_is_rejected(self):
		row = return_row("SOURCE-ROW-1", qty=-1, serial_no="SERIAL-1")

		with self.assertRaises(frappe.ValidationError):
			orders._validate_serial_selection(
				row,
				{"serial_no": "SERIAL-OTHER"},
				-1,
			)


class TestReturnPayments(TestCase):
	@patch.object(
		orders,
		"validate_pos_payment_accounts",
		return_value={"Cash": frappe._dict({"type": "Cash", "default_account": "Cash - TC"})},
	)
	def test_payment_uses_calculated_document_total(self, _get_payment_info):
		invoice = InvoiceStub(rounded_total=-12.5, grand_total=-12.49)
		invoice.currency = "USD"
		profile = frappe._dict(
			{"name": "POS-PROFILE", "company": "Test Company", "payments": []}
		)

		orders._set_return_payment(invoice, "Cash", profile)

		self.assertEqual(invoice.payments[0].amount, -12.5)
		self.assertEqual(invoice.paid_amount, -12.5)
		self.assertEqual(invoice.payments[0].account, "Cash - TC")

	@patch(
		"erpnext.accounts.doctype.sales_invoice.sales_invoice.get_mode_of_payment_info",
		return_value=[frappe._dict({"type": "Cash", "default_account": "Cash - TC"})],
	)
	def test_zero_total_return_keeps_zero_payment(self, _get_payment_info):
		invoice = InvoiceStub(rounded_total=0, grand_total=0)
		invoice.currency = "USD"
		profile = frappe._dict(
			{"name": "POS-PROFILE", "company": "Test Company", "payments": []}
		)

		orders._set_return_payment(invoice, "Cash", profile)

		self.assertEqual(invoice.payments[0].amount, 0)
		self.assertEqual(invoice.paid_amount, 0)


class TestManualReturnValidation(TestCase):
	@patch.object(
		frappe,
		"get_cached_value",
		return_value=frappe._dict({"has_serial_no": 0, "has_batch_no": 0}),
	)
	def test_zero_rate_stock_return_is_allowed(self, _get_cached_value):
		qty, rate = orders._validate_manual_return_item(
			{"item_code": "FREE-ITEM", "qty": 1, "rate": 0}
		)

		self.assertEqual((qty, rate), (1, 0))

	@patch.object(
		frappe,
		"get_cached_value",
		return_value=frappe._dict({"has_serial_no": 1, "has_batch_no": 0}),
	)
	def test_manual_serial_return_is_rejected(self, _get_cached_value):
		with self.assertRaises(frappe.ValidationError):
			orders._validate_manual_return_item(
				{"item_code": "SERIAL-ITEM", "qty": 1, "rate": 10}
			)

	@patch.object(frappe.db, "exists", return_value=True)
	@patch.object(
		frappe,
		"get_cached_value",
		return_value=frappe._dict({"has_serial_no": 0, "has_batch_no": 1}),
	)
	def test_manual_batch_identity_is_preserved(self, _get_cached_value, db_exists):
		item = {
			"item_code": "BATCH-ITEM",
			"qty": 1,
			"rate": 10,
			"batch_no": "BATCH-001",
		}

		orders._validate_manual_return_item(item)

		db_exists.assert_called_once_with(
			"Batch", {"name": "BATCH-001", "item": "BATCH-ITEM"}
		)
		self.assertEqual(item["use_serial_batch_fields"], 1)


class TestReturnOrderQueries(TestCase):
	@patch.object(orders.frappe, "get_list", return_value=[])
	@patch.object(orders, "validate_pos_access")
	def test_paid_filter_uses_zero_outstanding_without_status_filter(
		self, _validate_access, get_list
	):
		orders.get_past_orders("POS-PROFILE", status="Paid")

		filters = get_list.call_args.kwargs["filters"]
		self.assertEqual(filters["docstatus"], 1)
		self.assertEqual(filters["is_return"], 0)
		self.assertEqual(filters["outstanding_amount"], ["<=", 0])
		self.assertNotIn("status", filters)


class TestReturnDraftLifecycle(TestCase):
	@patch.object(orders, "_format_order_response", return_value={"name": "RETURN-1"})
	@patch(
		"erpnext.accounts.doctype.pos_invoice.pos_invoice.make_sales_return"
	)
	@patch.object(orders, "validate_pos_access")
	@patch.object(orders.frappe, "get_doc")
	@patch.object(orders.frappe.db, "exists", return_value=True)
	def test_create_return_persists_mapped_draft(
		self,
		_exists,
		get_doc,
		_validate_access,
		make_sales_return,
		_format_response,
	):
		source = frappe._dict(
			{
				"docstatus": 1,
				"is_return": 0,
				"pos_profile": "POS-PROFILE",
				"check_permission": Mock(),
			}
		)
		return_doc = Mock()
		get_doc.return_value = source
		make_sales_return.return_value = return_doc

		result = orders.create_return_invoice("POS-INV-1")

		return_doc.insert.assert_called_once_with()
		self.assertEqual(result, {"name": "RETURN-1"})

	@patch.object(orders.frappe.db, "exists")
	def test_submit_rejects_empty_invoice_name_before_database_lookup(self, exists):
		with self.assertRaises(frappe.ValidationError):
			orders.submit_return_invoice("", [], [])

		exists.assert_not_called()
