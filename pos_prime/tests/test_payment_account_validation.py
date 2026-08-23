from unittest import TestCase
from unittest.mock import Mock, call, patch

import frappe

from pos_prime.api import pos_session
from pos_prime.api._utils import validate_pos_payment_accounts


def payment_profile(*, currency="USD", allow_in_returns=1):
    return frappe._dict(
        {
            "name": "Test POS Profile",
            "company": "Test Company",
            "currency": currency,
            "payments": [
                frappe._dict(
                    {
                        "mode_of_payment": "Ecocash",
                        "allow_in_returns": allow_in_returns,
                    }
                )
            ],
        }
    )


def payment_database(*, account_currency="USD", **account_overrides):
    account = {
        "company": "Test Company",
        "account_currency": account_currency,
        "is_group": 0,
        "disabled": 0,
    }
    account.update(account_overrides)

    def get_value(doctype, name_or_filters, fieldname=None, as_dict=False, **_kwargs):
        if doctype == "Company":
            return "USD"
        if doctype == "Mode of Payment":
            return frappe._dict({"enabled": 1, "type": "Phone"})
        if doctype == "Mode of Payment Account":
            return "Ecocash - TC"
        if doctype == "Account":
            return frappe._dict(account)
        raise AssertionError(f"Unexpected lookup for {doctype}")

    return get_value


class DocumentLikePayment:
    """Minimal child-document shape without mapping containment support."""

    def __init__(self, mode_of_payment, amount):
        self.values = {"mode_of_payment": mode_of_payment, "amount": amount}

    def get(self, fieldname):
        return self.values.get(fieldname)


class TestPaymentAccountValidation(TestCase):
    @patch.object(frappe.db, "get_value")
    def test_non_iterable_child_document_payment_is_supported(self, get_value):
        get_value.side_effect = payment_database()

        result = validate_pos_payment_accounts(
            payment_profile(),
            [DocumentLikePayment("Ecocash", 25)],
        )

        self.assertEqual(result["Ecocash"].default_account, "Ecocash - TC")

    @patch.object(frappe.db, "get_value")
    def test_company_currency_account_is_allowed(self, get_value):
        get_value.side_effect = payment_database(account_currency="USD")

        result = validate_pos_payment_accounts(
            payment_profile(currency="EUR"),
            [{"mode_of_payment": "Ecocash", "account": "Ecocash - TC"}],
            currency="EUR",
        )

        self.assertEqual(result["Ecocash"].default_account, "Ecocash - TC")

    @patch.object(frappe.db, "get_value")
    def test_invoice_currency_account_is_allowed(self, get_value):
        get_value.side_effect = payment_database(account_currency="EUR")

        result = validate_pos_payment_accounts(
            payment_profile(currency="EUR"),
            [{"mode_of_payment": "Ecocash"}],
            currency="EUR",
        )

        self.assertEqual(result["Ecocash"].account_currency, "EUR")

    @patch.object(frappe.db, "get_value")
    def test_unrelated_account_currency_is_rejected_before_sale(self, get_value):
        get_value.side_effect = payment_database(account_currency="ZWG")

        with self.assertRaisesRegex(
            frappe.ValidationError, "Account Currency must be USD"
        ):
            validate_pos_payment_accounts(
                payment_profile(currency="USD"),
                [{"mode_of_payment": "Ecocash"}],
            )

    @patch.object(frappe.db, "get_value")
    def test_payment_mode_must_belong_to_profile(self, get_value):
        get_value.side_effect = payment_database()

        with self.assertRaisesRegex(frappe.ValidationError, "not configured"):
            validate_pos_payment_accounts(
                payment_profile(),
                [{"mode_of_payment": "Card"}],
            )

        self.assertNotIn(
            call("Mode of Payment", "Card", ["enabled", "type"], as_dict=True),
            get_value.call_args_list,
        )

    @patch.object(frappe.db, "get_value")
    def test_return_mode_requires_allow_in_returns(self, get_value):
        get_value.side_effect = payment_database()

        with self.assertRaisesRegex(frappe.ValidationError, "not allowed for returns"):
            validate_pos_payment_accounts(
                payment_profile(allow_in_returns=0),
                [{"mode_of_payment": "Ecocash"}],
                returns_only=True,
            )

    @patch.object(frappe.db, "get_value")
    def test_mode_and_account_must_be_active_leafs_for_company(self, get_value):
        invalid_accounts = (
            ({"is_group": 1}, "ledger account"),
            ({"disabled": 1}, "disabled"),
            ({"company": "Other Company"}, "does not belong"),
        )

        for overrides, message in invalid_accounts:
            with self.subTest(overrides=overrides):
                get_value.reset_mock()
                get_value.side_effect = payment_database(**overrides)
                with self.assertRaisesRegex(frappe.ValidationError, message):
                    validate_pos_payment_accounts(
                        payment_profile(),
                        [{"mode_of_payment": "Ecocash"}],
                    )

        get_value.side_effect = payment_database()

        def disabled_mode(doctype, *args, **kwargs):
            if doctype == "Mode of Payment":
                return frappe._dict({"enabled": 0, "type": "Phone"})
            return payment_database()(doctype, *args, **kwargs)

        get_value.side_effect = disabled_mode
        with self.assertRaisesRegex(frappe.ValidationError, "Mode of Payment.*disabled"):
            validate_pos_payment_accounts(
                payment_profile(),
                [{"mode_of_payment": "Ecocash"}],
            )

    @patch.object(frappe.db, "get_value")
    def test_disabled_legacy_mode_can_close_with_a_valid_current_mapping(
        self, get_value
    ):
        valid_database = payment_database()

        def disabled_mode(doctype, *args, **kwargs):
            if doctype == "Mode of Payment":
                return frappe._dict({"enabled": 0, "type": "Phone"})
            return valid_database(doctype, *args, **kwargs)

        get_value.side_effect = disabled_mode

        result = validate_pos_payment_accounts(
            payment_profile(),
            [{"mode_of_payment": "Ecocash", "amount": 25}],
            require_profile_membership=False,
            require_enabled=False,
        )

        self.assertEqual(result["Ecocash"].default_account, "Ecocash - TC")

    @patch.object(frappe.db, "get_value")
    def test_stale_persisted_account_does_not_override_current_mapping(self, get_value):
        get_value.side_effect = payment_database()

        result = validate_pos_payment_accounts(
            payment_profile(),
            [
                {
                    "mode_of_payment": "Ecocash",
                    "account": "Old Ecocash ZWG - TC",
                }
            ],
        )

        self.assertEqual(result["Ecocash"].default_account, "Ecocash - TC")

    @patch.object(frappe.db, "get_value")
    def test_zero_placeholder_tender_does_not_block_non_cash_settlement(
        self, get_value
    ):
        get_value.side_effect = payment_database(account_currency="ZWG")

        result = validate_pos_payment_accounts(
            payment_profile(),
            [{"mode_of_payment": "Ecocash", "amount": 0}],
        )

        self.assertEqual(result, {})
        get_value.assert_called_once_with(
            "Company", "Test Company", "default_currency"
        )


class TestClosingPaymentPreflight(TestCase):
    def test_retry_snapshot_includes_v16_sales_invoices(self):
        closing = frappe._dict(
            {
                "pos_invoices": [
                    frappe._dict({"pos_invoice": "POS-INV-0001"})
                ],
                "sales_invoices": [
                    frappe._dict({"sales_invoice": "SINV-0001"})
                ],
            }
        )

        self.assertEqual(
            pos_session._closing_invoice_snapshot(closing),
            {
                ("POS Invoice", "POS-INV-0001"),
                ("Sales Invoice", "SINV-0001"),
            },
        )

    def test_v16_retry_allows_sales_invoices_already_linked_by_failed_job(self):
        failed = frappe._dict(
            {
                "sales_invoices": [
                    frappe._dict({"sales_invoice": "SINV-0001"})
                ]
            }
        )
        current = frappe._dict({"sales_invoices": []})

        self.assertTrue(
            pos_session._failed_closing_matches_current(failed, current)
        )

    def test_v16_retry_rejects_new_sales_invoice(self):
        failed = frappe._dict(
            {
                "sales_invoices": [
                    frappe._dict({"sales_invoice": "SINV-0001"})
                ]
            }
        )
        current = frappe._dict(
            {
                "sales_invoices": [
                    frappe._dict({"sales_invoice": "SINV-0002"})
                ]
            }
        )

        self.assertFalse(
            pos_session._failed_closing_matches_current(failed, current)
        )

    @patch.object(pos_session, "validate_pos_payment_accounts")
    @patch.object(pos_session.frappe, "get_doc")
    def test_legacy_invoice_mode_is_validated_without_current_membership(
        self, get_doc, validate_accounts
    ):
        profile = payment_profile()
        opening = frappe._dict(
            {
                "pos_profile": profile.name,
                "company": profile.company,
                "balance_details": [
                    frappe._dict({"mode_of_payment": "Ecocash"})
                ],
            }
        )
        invoice = frappe._dict(
            {
                "company": profile.company,
                "currency": "USD",
                "payments": [
                    frappe._dict(
                        {
                            "mode_of_payment": "Legacy Card",
                            "account": "Legacy Card - TC",
                            "amount": 25,
                        }
                    )
                ],
            }
        )
        closing = frappe._dict(
            {
                "pos_transactions": [
                    frappe._dict({"pos_invoice": "POS-INV-0001"})
                ]
            }
        )
        get_doc.side_effect = [profile, invoice]

        pos_session._validate_closing_payment_accounts(opening, closing)

        self.assertEqual(validate_accounts.call_count, 1)
        self.assertFalse(
            validate_accounts.call_args.kwargs["require_profile_membership"]
        )
        self.assertFalse(validate_accounts.call_args.kwargs["require_enabled"])
        self.assertEqual(validate_accounts.call_args.kwargs["currency"], "USD")

    def test_invalid_legacy_payment_does_not_create_failed_closing_document(self):
        opening = frappe._dict(
            {
                "name": "POS-OPE-0001",
                "status": "Open",
                "docstatus": 1,
                "pos_profile": "Test POS Profile",
                "company": "Test Company",
            }
        )
        closing = Mock()
        error = frappe.ValidationError("Invalid payment account currency")
        opening.reload = Mock()

        with (
            patch.object(pos_session.frappe.db, "exists", return_value=True),
            patch.object(pos_session.frappe.db, "get_value", return_value=None),
            patch.object(pos_session.frappe.db, "sql"),
            patch.object(pos_session.frappe, "get_doc", return_value=opening),
            patch.object(pos_session, "_validate_opening_access"),
            patch.object(pos_session, "validate_doctype_permissions"),
            patch(
                "erpnext.accounts.doctype.pos_closing_entry.pos_closing_entry."
                "make_closing_entry_from_opening",
                return_value=closing,
            ),
            patch.object(
                pos_session,
                "_validate_closing_payment_accounts",
                side_effect=error,
            ),
        ):
            with self.assertRaises(frappe.ValidationError):
                pos_session.close_shift(opening.name)

        closing.insert.assert_not_called()
        closing.submit.assert_not_called()

    def test_queued_closing_is_returned_without_creating_a_duplicate(self):
        opening = frappe._dict(
            {
                "name": "POS-OPE-0001",
                "status": "Open",
                "docstatus": 1,
                "pos_profile": "Test POS Profile",
                "company": "Test Company",
                "reload": Mock(),
            }
        )
        existing = frappe._dict(
            {"name": "POS-CLO-0001", "status": "Queued", "reload": Mock()}
        )

        with (
            patch.object(pos_session.frappe.db, "exists", return_value=True),
            patch.object(
                pos_session.frappe.db, "get_value", return_value=existing.name
            ),
            patch.object(pos_session.frappe.db, "sql") as lock_shift,
            patch.object(
                pos_session.frappe, "get_doc", side_effect=[opening, existing]
            ),
            patch.object(pos_session, "_validate_opening_access"),
            patch.object(pos_session, "validate_doctype_permissions"),
            patch(
                "erpnext.accounts.doctype.pos_closing_entry.pos_closing_entry."
                "make_closing_entry_from_opening"
            ) as make_closing,
        ):
            result = pos_session.close_shift(opening.name)

        self.assertEqual(result, {"name": existing.name, "status": "Queued"})
        lock_shift.assert_called_once()
        make_closing.assert_not_called()

    def test_failed_closing_retries_same_document_when_invoice_set_is_unchanged(self):
        opening = frappe._dict(
            {
                "name": "POS-OPE-0001",
                "status": "Open",
                "docstatus": 1,
                "pos_profile": "Test POS Profile",
                "company": "Test Company",
                "reload": Mock(),
            }
        )
        rows = [frappe._dict({"pos_invoice": "POS-INV-0001"})]
        existing = frappe._dict(
            {
                "name": "POS-CLO-0001",
                "status": "Failed",
                "pos_transactions": rows,
                "retry": Mock(),
                "reload": Mock(),
            }
        )
        current = frappe._dict({"pos_transactions": rows})

        with (
            patch.object(pos_session.frappe.db, "exists", return_value=True),
            patch.object(
                pos_session.frappe.db, "get_value", return_value=existing.name
            ),
            patch.object(pos_session.frappe.db, "sql"),
            patch.object(
                pos_session.frappe, "get_doc", side_effect=[opening, existing]
            ),
            patch.object(pos_session, "_validate_opening_access"),
            patch.object(pos_session, "validate_doctype_permissions"),
            patch.object(pos_session, "_validate_closing_payment_accounts") as preflight,
            patch(
                "erpnext.accounts.doctype.pos_closing_entry.pos_closing_entry."
                "make_closing_entry_from_opening",
                return_value=current,
            ),
        ):
            result = pos_session.close_shift(opening.name)

        self.assertEqual(result["name"], existing.name)
        preflight.assert_called_once_with(opening, existing)
        existing.retry.assert_called_once_with()
        existing.reload.assert_called_once_with()

    def test_failed_closing_is_not_retried_after_invoice_set_changes(self):
        opening = frappe._dict(
            {
                "name": "POS-OPE-0001",
                "status": "Open",
                "docstatus": 1,
                "pos_profile": "Test POS Profile",
                "company": "Test Company",
                "reload": Mock(),
            }
        )
        existing = frappe._dict(
            {
                "name": "POS-CLO-0001",
                "status": "Failed",
                "pos_transactions": [
                    frappe._dict({"pos_invoice": "POS-INV-0001"})
                ],
                "retry": Mock(),
                "reload": Mock(),
            }
        )
        current = frappe._dict(
            {
                "pos_transactions": [
                    frappe._dict({"pos_invoice": "POS-INV-0001"}),
                    frappe._dict({"pos_invoice": "POS-INV-0002"}),
                ]
            }
        )

        with (
            patch.object(pos_session.frappe.db, "exists", return_value=True),
            patch.object(
                pos_session.frappe.db, "get_value", return_value=existing.name
            ),
            patch.object(pos_session.frappe.db, "sql"),
            patch.object(
                pos_session.frappe, "get_doc", side_effect=[opening, existing]
            ),
            patch.object(pos_session, "_validate_opening_access"),
            patch.object(pos_session, "validate_doctype_permissions"),
            patch.object(pos_session, "_validate_closing_payment_accounts") as preflight,
            patch(
                "erpnext.accounts.doctype.pos_closing_entry.pos_closing_entry."
                "make_closing_entry_from_opening",
                return_value=current,
            ),
        ):
            with self.assertRaisesRegex(frappe.ValidationError, "no longer matches"):
                pos_session.close_shift(opening.name)

        preflight.assert_not_called()
        existing.retry.assert_not_called()
