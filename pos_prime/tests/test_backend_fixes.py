from unittest import TestCase
from unittest.mock import Mock, patch

import frappe

from pos_prime.api import customer_profile, customers, pos_session
from pos_prime.api._utils import (
    apply_item_discount_policy,
    build_item_dict,
    configure_pos_settlement,
    get_allowed_invoice_discount,
    get_remaining_invoice_amount,
    set_pos_change_amount,
    set_pos_paid_amount,
    validate_supplemental_settlement,
)
from pos_prime.overrides.pos_invoice_merge_log import POSPrimePOSInvoiceMergeLog


def settlement_invoice(**overrides):
    values = {
        "rounded_total": 100,
        "grand_total": 100,
        "paid_amount": 80,
        "change_amount": 0,
        "total_advance": 15,
        "write_off_amount": 5,
        "loyalty_amount": 10,
        "redeem_loyalty_points": 1,
        "conversion_rate": 1,
        "payments": [frappe._dict({"amount": 70})],
        "is_return": 0,
        "docstatus": 1,
        "pos_profile": "POS-PROFILE",
        "validate_full_payment": Mock(),
    }
    values.update(overrides)
    return frappe._dict(values)


class TestItemDiscountPricing(TestCase):
    def setUp(self):
        self.profile = frappe._dict(
            {
                "warehouse": "Stores - TC",
                "income_account": "Sales - TC",
                "expense_account": "COGS - TC",
                "cost_center": "Main - TC",
                "allow_rate_change": 1,
                "allow_discount_change": 1,
            }
        )

    def test_percentage_discount_builds_erpnext_effective_rate(self):
        item = build_item_dict(
            {
                "item_code": "ITEM-1",
                "qty": 2,
                "rate": 100,
                "discount_percentage": 10,
            },
            self.profile,
        )

        self.assertEqual(item["price_list_rate"], 100)
        self.assertEqual(item["rate"], 90)
        self.assertEqual(item["discount_percentage"], 10)

    def test_flat_discount_is_per_unit(self):
        item = build_item_dict(
            {
                "item_code": "ITEM-1",
                "qty": 2,
                "rate": 100,
                "discount_amount": 10,
            },
            self.profile,
        )

        self.assertEqual(item["price_list_rate"], 100)
        self.assertEqual(item["rate"], 90)
        self.assertEqual(item["discount_amount"], 10)

    def test_explicit_list_rate_does_not_double_apply_discount(self):
        item = build_item_dict(
            {
                "item_code": "ITEM-1",
                "qty": 1,
                "rate": 90,
                "price_list_rate": 100,
                "discount_percentage": 10,
            },
            self.profile,
        )

        self.assertEqual(item["rate"], 90)

    def test_flat_discount_cannot_exceed_unit_rate(self):
        with self.assertRaises(frappe.ValidationError):
            build_item_dict(
                {
                    "item_code": "ITEM-1",
                    "qty": 1,
                    "rate": 100,
                    "discount_amount": 101,
                },
                self.profile,
            )

    def test_full_flat_discount_keeps_zero_rate_for_erpnext(self):
        item = build_item_dict(
            {
                "item_code": "ITEM-1",
                "qty": 1,
                "rate": 100,
                "discount_amount": 100,
            },
            self.profile,
        )

        self.assertEqual(item["rate"], 0)
        self.assertEqual(item["discount_percentage"], 100)
        self.assertEqual(item["discount_amount"], 100)

    def test_disabled_rate_and_discount_changes_ignore_client_values(self):
        profile = frappe._dict(
            {**self.profile, "allow_rate_change": 0, "allow_discount_change": 0}
        )

        item = build_item_dict(
            {
                "item_code": "ITEM-1",
                "qty": 1,
                "rate": 1,
                "price_list_rate": 100,
                "discount_percentage": 99,
                "discount_amount": 99,
                "margin_type": "Percentage",
                "margin_rate_or_amount": -99,
            },
            profile,
        )

        self.assertNotIn("rate", item)
        self.assertNotIn("price_list_rate", item)
        self.assertNotIn("discount_percentage", item)
        self.assertNotIn("discount_amount", item)
        self.assertNotIn("margin_type", item)
        self.assertNotIn("margin_rate_or_amount", item)

    def test_rate_locked_flat_discount_uses_server_resolved_price(self):
        profile = frappe._dict(
            {**self.profile, "allow_rate_change": 0, "allow_discount_change": 1}
        )
        source_item = {
            "item_code": "ITEM-1",
            "qty": 1,
            "rate": 1,
            "price_list_rate": 1,
            "discount_amount": 1,
        }

        item = build_item_dict(source_item, profile)
        self.assertNotIn("rate", item)
        self.assertNotIn("price_list_rate", item)
        self.assertNotIn("discount_amount", item)
        self.assertEqual(item["discount_percentage"], 0)

        invoice = frappe._dict(
            {
                "items": [
                    frappe._dict(
                        {
                            "price_list_rate": 100,
                            "rate": 100,
                            "discount_percentage": 0,
                            "discount_amount": 0,
                            "is_free_item": 0,
                        }
                    )
                ]
            }
        )
        apply_item_discount_policy(invoice, [source_item], profile)

        priced_item = invoice.get("items")[0]
        self.assertEqual(priced_item.rate, 99)
        self.assertEqual(priced_item.discount_percentage, 1)
        self.assertEqual(priced_item.discount_amount, 1)

    def test_rate_locked_full_flat_discount_uses_server_resolved_price(self):
        profile = frappe._dict(
            {**self.profile, "allow_rate_change": 0, "allow_discount_change": 1}
        )
        source_item = {
            "item_code": "ITEM-1",
            "qty": 1,
            "rate": 1,
            "price_list_rate": 1,
            "discount_amount": 100,
        }
        invoice = frappe._dict(
            {
                "items": [
                    frappe._dict(
                        {
                            "price_list_rate": 100,
                            "rate": 100,
                            "discount_percentage": 0,
                            "discount_amount": 0,
                            "is_free_item": 0,
                        }
                    )
                ]
            }
        )

        apply_item_discount_policy(invoice, [source_item], profile)

        priced_item = invoice.get("items")[0]
        self.assertEqual(priced_item.rate, 0)
        self.assertEqual(priced_item.discount_percentage, 100)
        self.assertEqual(priced_item.discount_amount, 100)

    def test_rate_locked_flat_discount_cannot_exceed_server_price(self):
        profile = frappe._dict(
            {**self.profile, "allow_rate_change": 0, "allow_discount_change": 1}
        )
        invoice = frappe._dict(
            {
                "items": [
                    frappe._dict(
                        {
                            "price_list_rate": 100,
                            "rate": 100,
                            "is_free_item": 0,
                        }
                    )
                ]
            }
        )

        with self.assertRaises(frappe.ValidationError):
            apply_item_discount_policy(
                invoice,
                [{"item_code": "ITEM-1", "discount_amount": 101}],
                profile,
            )

    def test_disabled_invoice_discount_ignores_client_values(self):
        profile = frappe._dict({"allow_discount_change": 0})

        self.assertEqual(get_allowed_invoice_discount(profile, 90, 90), (0, 0))
        self.assertEqual(get_allowed_invoice_discount(profile, 101, -1), (0, 0))


class TestCustomerTreeDefaults(TestCase):
    @patch.object(customers.frappe, "get_all", return_value=["Leaf Group"])
    @patch.object(customers.frappe.db, "get_value", return_value=1)
    def test_group_default_falls_back_to_a_leaf(self, _get_value, _get_all):
        result = customers._first_leaf_tree_node(
            "Customer Group", preferred="All Customer Groups"
        )

        self.assertEqual(result, "Leaf Group")

    @patch.object(customers.frappe, "get_all", return_value=["Retail", "Wholesale"])
    @patch.object(customers.frappe.db, "get_value")
    def test_configured_tree_group_expands_to_leaf_descendants(
        self, get_value, _get_all
    ):
        get_value.return_value = frappe._dict(
            {"is_group": 1, "lft": 1, "rgt": 10}
        )

        result = customers._expand_leaf_tree_nodes(
            "Customer Group", ["All Customer Groups"]
        )

        self.assertEqual(result, ["Retail", "Wholesale"])


class TestSupplementalSettlement(TestCase):
    def test_paid_amount_is_rebuilt_with_loyalty_once(self):
        invoice = settlement_invoice(
            payments=[frappe._dict({"amount": 70})],
            conversion_rate=1,
            paid_amount=999,
        )

        set_pos_paid_amount(invoice)

        self.assertEqual(invoice.paid_amount, 80)
        self.assertEqual(invoice.base_paid_amount, 80)

    def test_remaining_amount_does_not_subtract_loyalty_twice(self):
        invoice = settlement_invoice(total_advance=0, write_off_amount=5)

        self.assertEqual(get_remaining_invoice_amount(invoice), 15)

    def test_mixed_settlement_counts_loyalty_inside_paid_amount(self):
        invoice = settlement_invoice()

        validate_supplemental_settlement(invoice)

    def test_write_off_over_tender_becomes_change_not_extra_income(self):
        invoice = settlement_invoice(
            paid_amount=103,
            total_advance=0,
            write_off_amount=5,
        )

        set_pos_change_amount(invoice)
        validate_supplemental_settlement(invoice)

        self.assertEqual(invoice.change_amount, 8)

    @patch.object(frappe.db, "get_value", return_value=0)
    def test_full_payment_wrapper_accepts_exact_mixed_settlement(self, _get_value):
        invoice = settlement_invoice()

        configure_pos_settlement(invoice)
        invoice.validate_full_payment()

    @patch.object(frappe.db, "get_value", return_value=0)
    def test_full_payment_wrapper_rejects_short_mixed_settlement(self, _get_value):
        invoice = settlement_invoice(
            paid_amount=75,
            payments=[frappe._dict({"amount": 65})],
        )

        configure_pos_settlement(invoice)
        with self.assertRaises(frappe.ValidationError):
            invoice.validate_full_payment()


class TestShiftPaymentSummary(TestCase):
    def setUp(self):
        self.opening = frappe._dict(
            {
                "balance_details": [
                    frappe._dict(
                        {"mode_of_payment": "Cash", "opening_amount": 100}
                    ),
                    frappe._dict(
                        {"mode_of_payment": "Card", "opening_amount": 0}
                    ),
                    frappe._dict(
                        {"mode_of_payment": "Bank", "opening_amount": 25}
                    ),
                ]
            }
        )

    @patch.object(pos_session, "_erpnext_major_version", return_value=15)
    def test_v15_expected_amount_already_contains_opening(self, _version):
        closing = frappe._dict(
            {
                "payment_reconciliation": [
                    frappe._dict(
                        {
                            "mode_of_payment": "Cash",
                            "opening_amount": 100,
                            "expected_amount": 150,
                        }
                    ),
                    frappe._dict(
                        {
                            "mode_of_payment": "Card",
                            "opening_amount": 0,
                            "expected_amount": 20,
                        }
                    ),
                    frappe._dict(
                        {
                            "mode_of_payment": "Bank",
                            "opening_amount": 25,
                            "expected_amount": 25,
                        }
                    ),
                ]
            }
        )

        rows = pos_session._build_payment_summary(self.opening, closing)

        self.assertEqual(
            [(row["sales_amount"], row["expected_amount"]) for row in rows],
            [(50, 150), (20, 20), (0, 25)],
        )

    @patch.object(pos_session, "_erpnext_major_version", return_value=16)
    def test_v16_adds_opening_and_preserves_opening_only_modes(self, _version):
        closing = frappe._dict(
            {
                "payment_reconciliation": [
                    frappe._dict(
                        {
                            "mode_of_payment": "Cash",
                            "opening_amount": 0,
                            "expected_amount": 50,
                        }
                    ),
                    frappe._dict(
                        {
                            "mode_of_payment": "Card",
                            "opening_amount": 0,
                            "expected_amount": 20,
                        }
                    ),
                ]
            }
        )

        rows = pos_session._build_payment_summary(self.opening, closing)

        self.assertEqual(
            [(row["sales_amount"], row["expected_amount"]) for row in rows],
            [(50, 150), (20, 20), (0, 25)],
        )


class InvoiceStub:
    def __init__(self):
        self.posting_date = "2026-08-22"
        self.advances = []
        self.total_advance = 0

    def set(self, fieldname, value):
        setattr(self, fieldname, list(value))

    def append(self, fieldname, value):
        row = frappe._dict(value)
        getattr(self, fieldname).append(row)
        return row


def advance(reference_name, available, allocated, reference_row=None):
    return frappe._dict(
        {
            "reference_type": "Journal Entry" if reference_row else "Payment Entry",
            "reference_name": reference_name,
            "reference_row": reference_row,
            "advance_amount": available,
            "allocated_amount": allocated,
            "remarks": "Store credit",
            "account": "Debtors - TC",
            "ref_exchange_rate": 1,
        }
    )


class TestStoreCreditConsolidation(TestCase):
    @patch.object(
        POSPrimePOSInvoiceMergeLog,
        "_lock_and_get_unadjusted_amount",
        return_value=100,
    )
    def test_allocations_are_deduplicated_and_aggregated_for_close(self, _lock):
        invoice = InvoiceStub()
        merge_log = object.__new__(POSPrimePOSInvoiceMergeLog)
        sources = [
            frappe._dict(
                {
                    "doctype": "POS Invoice",
                    "docstatus": 1,
                    "name": "POS-1",
                    "advances": [advance("PAY-1", 100, 60)],
                }
            ),
            frappe._dict(
                {
                    "doctype": "POS Invoice",
                    "docstatus": 1,
                    "name": "POS-2",
                    "advances": [advance("PAY-1", 40, 40)],
                }
            ),
        ]

        merge_log._merge_store_credit_advances(invoice, sources)

        self.assertEqual(len(invoice.advances), 1)
        self.assertEqual(invoice.advances[0].advance_amount, 100)
        self.assertEqual(invoice.advances[0].allocated_amount, 100)
        self.assertEqual(invoice.total_advance, 100)

    @patch.object(
        POSPrimePOSInvoiceMergeLog,
        "_lock_and_get_unadjusted_amount",
        return_value=50,
    )
    def test_overallocated_reference_is_rejected(self, _lock):
        invoice = InvoiceStub()
        merge_log = object.__new__(POSPrimePOSInvoiceMergeLog)
        source = frappe._dict(
            {
                "doctype": "POS Invoice",
                "docstatus": 1,
                "name": "POS-1",
                "advances": [advance("PAY-1", 50, 60)],
            }
        )

        with self.assertRaises(frappe.ValidationError):
            merge_log._merge_store_credit_advances(invoice, [source])

    @patch(
        "erpnext.accounts.doctype.pos_invoice_merge_log.pos_invoice_merge_log."
        "POSInvoiceMergeLog.process_merging_into_sales_invoice"
    )
    def test_close_reconciles_advances_after_core_submit(self, core_process):
        invoice = frappe._dict(
            {
                "advances": [frappe._dict({"allocated_amount": 25})],
                "update_against_document_in_jv": Mock(),
            }
        )
        core_process.return_value = invoice
        merge_log = object.__new__(POSPrimePOSInvoiceMergeLog)

        result = merge_log.process_merging_into_sales_invoice([])

        self.assertIs(result, invoice)
        invoice.update_against_document_in_jv.assert_called_once_with()

    @patch(
        "erpnext.accounts.doctype.pos_invoice_merge_log.pos_invoice_merge_log."
        "POSInvoiceMergeLog.on_cancel"
    )
    @patch("erpnext.accounts.utils.unlink_ref_doc_from_payment_entries")
    @patch.object(frappe, "get_doc")
    def test_close_cancel_unlinks_advances_before_core_cancel(
        self, get_doc, unlink, core_cancel
    ):
        invoice = frappe._dict(
            {
                "docstatus": 1,
                "advances": [frappe._dict({"allocated_amount": 25})],
            }
        )
        get_doc.return_value = invoice
        merge_log = object.__new__(POSPrimePOSInvoiceMergeLog)
        merge_log.consolidated_invoice = "SINV-1"

        merge_log.on_cancel()

        unlink.assert_called_once_with(invoice)
        core_cancel.assert_called_once_with()

    @patch.object(customer_profile, "_validate_customer_read", return_value=Mock())
    @patch.object(customer_profile.frappe.db, "exists", return_value=True)
    @patch.object(customer_profile.frappe, "get_cached_value", return_value=None)
    @patch.object(customer_profile.frappe.db, "sql")
    def test_consolidated_pos_rows_stop_reserving_cancelled_safe_credit(
        self, db_sql, _cached_value, _exists, _customer_read
    ):
        db_sql.side_effect = [[], []]

        customer_profile._get_store_credit("CUST-1", "Test Company")

        payment_query = db_sql.call_args_list[0].args[0]
        self.assertIn("pos_invoice.consolidated_invoice", payment_query)
        self.assertIn("= ''", payment_query)
