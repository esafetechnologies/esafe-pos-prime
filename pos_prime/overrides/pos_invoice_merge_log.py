# Copyright (c) 2026, Ravindu Gajanayaka
# Licensed under GPLv3. See license.txt

import frappe
from frappe import _
from frappe.utils import flt

from erpnext.accounts.doctype.pos_invoice_merge_log.pos_invoice_merge_log import (
    POSInvoiceMergeLog,
)

from pos_prime.api._utils import validate_pos_payment_accounts


class POSPrimePOSInvoiceMergeLog(POSInvoiceMergeLog):
    """Carry POS Prime store-credit allocations into the accounting invoice."""

    def merge_pos_invoice_into(self, invoice, data):
        invoice = super().merge_pos_invoice_into(invoice, data)
        self._canonicalize_payment_accounts(invoice)
        self._merge_store_credit_advances(invoice, data)
        return invoice

    def _canonicalize_payment_accounts(self, invoice):
        """Replace historical payment accounts with the current company mapping."""
        payments = [
            payment
            for payment in invoice.get("payments") or []
            if flt(payment.get("amount"))
        ]
        if not payments:
            return

        profile_context = frappe._dict(
            {
                "name": invoice.get("pos_profile") or "Consolidated POS",
                "company": invoice.company,
                "currency": invoice.currency,
                "payments": [],
            }
        )
        resolved = validate_pos_payment_accounts(
            profile_context,
            payments,
            company=invoice.company,
            currency=invoice.currency,
            require_profile_membership=False,
            require_enabled=False,
        )

        for payment in payments:
            payment.account = resolved[payment.mode_of_payment].default_account

    def process_merging_into_sales_invoice(self, data):
        """Submit through ERPNext, then reconcile advances skipped for POS sales."""
        result = super().process_merging_into_sales_invoice(data)
        invoice = (
            frappe.get_doc("Sales Invoice", result)
            if isinstance(result, str)
            else result
        )
        if invoice.get("advances"):
            # SalesInvoice.on_submit deliberately skips this for is_pos=1.
            # The merge and reconciliation share one transaction, including the
            # source-row locks acquired while the advances are assembled.
            invoice.update_against_document_in_jv()
        return result

    def on_cancel(self):
        """Release consolidated references before ERPNext cancels the invoice."""
        if self.consolidated_invoice:
            invoice = frappe.get_doc("Sales Invoice", self.consolidated_invoice)
            if invoice.docstatus == 1 and invoice.get("advances"):
                from erpnext.accounts.utils import unlink_ref_doc_from_payment_entries

                unlink_ref_doc_from_payment_entries(invoice)

        super().on_cancel()

    def _merge_store_credit_advances(self, invoice, source_invoices):
        advances = {}

        for source in source_invoices:
            if source.doctype != "POS Invoice" or source.docstatus != 1:
                continue

            for row in source.get("advances") or []:
                allocated_amount = flt(row.allocated_amount)
                if allocated_amount <= 0:
                    continue
                if not row.reference_type or not row.reference_name:
                    frappe.throw(
                        _("POS Invoice {0} contains an invalid store-credit allocation.").format(
                            source.name
                        )
                    )

                reference_row = (
                    row.get("reference_row") or ""
                    if row.reference_type == "Journal Entry"
                    else ""
                )
                key = (row.reference_type, row.reference_name, reference_row)
                merged = advances.setdefault(
                    key,
                    {
                        "reference_type": row.reference_type,
                        "reference_name": row.reference_name,
                        "reference_row": reference_row or None,
                        "advance_amount": 0,
                        "allocated_amount": 0,
                        "remarks": row.get("remarks") or "",
                        "account": row.get("account"),
                        "ref_exchange_rate": flt(row.get("ref_exchange_rate")) or 1,
                        "difference_posting_date": row.get("difference_posting_date")
                        or invoice.posting_date,
                    },
                )
                merged["allocated_amount"] += allocated_amount

        invoice.set("advances", [])
        for key in sorted(advances):
            row = advances[key]
            row["advance_amount"] = self._lock_and_get_unadjusted_amount(
                invoice, row
            )
            if row["allocated_amount"] > row["advance_amount"] + 0.005:
                frappe.throw(
                    _(
                        "Store-credit allocations for {0} exceed its available advance."
                    ).format(row["reference_name"])
                )
            invoice.append("advances", row)

        invoice.total_advance = flt(
            sum(row["allocated_amount"] for row in advances.values())
        )

    def _lock_and_get_unadjusted_amount(self, invoice, advance):
        """Lock and return the source amount expected by ERPNext reconciliation."""
        params = {
            "reference_name": advance["reference_name"],
            "reference_row": advance.get("reference_row"),
            "customer": invoice.customer,
            "company": invoice.company,
        }
        if advance["reference_type"] == "Payment Entry":
            rows = frappe.db.sql(
                """
                SELECT
                    pe.unallocated_amount AS amount,
                    pe.paid_from AS account,
                    pe.source_exchange_rate AS ref_exchange_rate
                FROM `tabPayment Entry` pe
                WHERE pe.name = %(reference_name)s
                  AND pe.party_type = 'Customer'
                  AND pe.party = %(customer)s
                  AND pe.company = %(company)s
                  AND pe.payment_type = 'Receive'
                  AND pe.docstatus = 1
                FOR UPDATE
                """,
                params,
                as_dict=True,
            )
        elif advance["reference_type"] == "Journal Entry" and advance.get(
            "reference_row"
        ):
            rows = frappe.db.sql(
                """
                SELECT
                    (jea.credit_in_account_currency - jea.debit_in_account_currency) AS amount,
                    jea.account,
                    jea.exchange_rate AS ref_exchange_rate
                FROM `tabJournal Entry Account` jea
                INNER JOIN `tabJournal Entry` je ON je.name = jea.parent
                WHERE je.name = %(reference_name)s
                  AND jea.name = %(reference_row)s
                  AND jea.party_type = 'Customer'
                  AND jea.party = %(customer)s
                  AND je.company = %(company)s
                  AND je.docstatus = 1
                  AND jea.is_advance = 'Yes'
                  AND IFNULL(jea.reference_type, '') = ''
                FOR UPDATE
                """,
                params,
                as_dict=True,
            )
        else:
            rows = []

        if not rows or flt(rows[0].amount) <= 0:
            frappe.throw(
                _("Store-credit source {0} is no longer available.").format(
                    advance["reference_name"]
                )
            )
        if rows[0].account != invoice.debit_to:
            frappe.throw(
                _("Store-credit source {0} uses a different receivable account.").format(
                    advance["reference_name"]
                )
            )

        reservation_filters = "advance_row.reference_name = %(reference_name)s"
        if advance["reference_type"] == "Journal Entry":
            reservation_filters = "advance_row.reference_row = %(reference_row)s"
        reserved = frappe.db.sql(
            f"""
            SELECT COALESCE(SUM(advance_row.allocated_amount), 0)
            FROM `tabSales Invoice Advance` advance_row
            INNER JOIN `tabPOS Invoice` pos_invoice
                ON pos_invoice.name = advance_row.parent
               AND advance_row.parenttype = 'POS Invoice'
            WHERE advance_row.reference_type = %(reference_type)s
              AND {reservation_filters}
              AND advance_row.docstatus = 1
              AND IFNULL(pos_invoice.consolidated_invoice, '') = ''
            """,
            {**params, "reference_type": advance["reference_type"]},
        )
        reserved_amount = flt(reserved[0][0]) if reserved else 0
        source_amount = flt(rows[0].amount)
        if reserved_amount > source_amount + 0.005:
            frappe.throw(
                _("Store-credit source {0} was modified after allocation.").format(
                    advance["reference_name"]
                )
            )
        advance["ref_exchange_rate"] = flt(rows[0].ref_exchange_rate) or 1
        return source_amount
