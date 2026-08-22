# Copyright (c) 2026, Ravindu Gajanayaka
# Licensed under GPLv3. See license.txt

import frappe
from frappe.utils import flt

from pos_prime.api._utils import (
    validate_doctype_permissions,
    validate_pos_access,
)


def _validate_customer_read(customer):
    validate_pos_access()
    if not frappe.db.exists("Customer", customer):
        return None
    customer_doc = frappe.get_doc("Customer", customer)
    validate_doctype_permissions("Customer", "read", doc=customer_doc)
    return customer_doc


@frappe.whitelist()
def get_store_credit(customer, company):
    """Get available store credit from unallocated Payment Entries and Journal Entries.

    Returns individual advances (for populating the advances table on
    POS Invoice) and the total available amount.
    """
    return _get_store_credit(customer, company)


def get_store_credit_for_update(customer, company):
    """Return available credit while locking its source rows for allocation."""
    return _get_store_credit(customer, company, lock=True)


def _get_store_credit(customer, company, lock=False):
    customer_doc = _validate_customer_read(customer)
    if not customer_doc or not company:
        return {"advances": [], "total_advance": 0}

    if not frappe.db.exists("Company", company):
        return {"advances": [], "total_advance": 0}

    if lock:
        frappe.db.sql(
            """
            SELECT pe.name
            FROM `tabPayment Entry` pe
            WHERE pe.party_type = 'Customer'
              AND pe.party = %(customer)s
              AND pe.company = %(company)s
              AND pe.payment_type = 'Receive'
              AND pe.docstatus = 1
              AND pe.unallocated_amount > 0
            FOR UPDATE
            """,
            {"customer": customer, "company": company},
        )
        frappe.db.sql(
            """
            SELECT jea.name
            FROM `tabJournal Entry Account` jea
            INNER JOIN `tabJournal Entry` je ON je.name = jea.parent
            WHERE jea.party_type = 'Customer'
              AND jea.party = %(customer)s
              AND je.company = %(company)s
              AND je.docstatus = 1
              AND jea.is_advance = 'Yes'
              AND IFNULL(jea.reference_type, '') = ''
            FOR UPDATE
            """,
            {"customer": customer, "company": company},
        )

    # 1. Payment Entry advances (unallocated receipts)
    pe_advances = frappe.db.sql(
        """
        SELECT
            'Payment Entry' as reference_type,
            pe.name as reference_name,
            '' as reference_row,
            (pe.unallocated_amount - IFNULL(pos_alloc.total_allocated, 0)) as amount,
            pe.paid_from as account,
            pe.source_exchange_rate as ref_exchange_rate,
            pe.remarks,
            pe.posting_date
        FROM `tabPayment Entry` pe
        LEFT JOIN (
            SELECT reference_name, SUM(allocated_amount) AS total_allocated
            FROM `tabSales Invoice Advance` advance_row
            INNER JOIN `tabPOS Invoice` pos_invoice
                ON pos_invoice.name = advance_row.parent
                AND advance_row.parenttype = 'POS Invoice'
            WHERE advance_row.reference_type = 'Payment Entry'
              AND advance_row.docstatus = 1
              AND IFNULL(pos_invoice.consolidated_invoice, '') = ''
            GROUP BY advance_row.reference_name
        ) pos_alloc ON pos_alloc.reference_name = pe.name
        WHERE pe.party_type = 'Customer'
          AND pe.party = %(customer)s
          AND pe.company = %(company)s
          AND pe.payment_type = 'Receive'
          AND pe.docstatus = 1
          AND (pe.unallocated_amount - IFNULL(pos_alloc.total_allocated, 0)) > 0
        ORDER BY pe.posting_date ASC
        """,
        {"customer": customer, "company": company},
        as_dict=True,
    )

    # 2. Journal Entry advances (manual credits, adjustments)
    receivable_account = frappe.get_cached_value(
        "Company", company, "default_receivable_account"
    )
    je_advances = []
    if receivable_account:
        je_advances = frappe.db.sql(
            """
            SELECT
                'Journal Entry' as reference_type,
                jea.parent as reference_name,
                jea.name as reference_row,
                (jea.credit_in_account_currency - jea.debit_in_account_currency
                 - IFNULL(alloc.total_allocated, 0)) as amount,
                jea.account as account,
                jea.exchange_rate as ref_exchange_rate,
                je.remark as remarks,
                je.posting_date
            FROM `tabJournal Entry Account` jea
            JOIN `tabJournal Entry` je ON je.name = jea.parent
                LEFT JOIN (
                    SELECT advance_row.reference_row,
                           SUM(advance_row.allocated_amount) as total_allocated
                FROM `tabSales Invoice Advance` advance_row
                LEFT JOIN `tabPOS Invoice` pos_invoice
                    ON pos_invoice.name = advance_row.parent
                    AND advance_row.parenttype = 'POS Invoice'
                    WHERE advance_row.reference_type = 'Journal Entry'
                      AND advance_row.docstatus = 1
                      AND advance_row.parenttype = 'POS Invoice'
                      AND IFNULL(pos_invoice.consolidated_invoice, '') = ''
                GROUP BY advance_row.reference_row
            ) alloc ON alloc.reference_row = jea.name
            WHERE jea.party_type = 'Customer'
              AND jea.party = %(customer)s
              AND je.company = %(company)s
              AND jea.account = %(receivable_account)s
              AND je.docstatus = 1
              AND jea.is_advance = 'Yes'
              AND IFNULL(jea.reference_type, '') = ''
              AND (jea.credit_in_account_currency - jea.debit_in_account_currency
                   - IFNULL(alloc.total_allocated, 0)) > 0
            ORDER BY je.posting_date ASC
            """,
            {
                "customer": customer,
                "company": company,
                "receivable_account": receivable_account,
            },
            as_dict=True,
        )

    # Combine and sort by posting date (FIFO)
    advances = sorted(
        list(pe_advances) + list(je_advances),
        key=lambda a: a.posting_date or "",
    )
    total = sum(flt(a.amount) for a in advances)
    return {"advances": advances, "total_advance": flt(total, 2)}


@frappe.whitelist()
def get_customer_pos_invoices(customer, company="", limit=10):
    """Get recent submitted POS Invoices for a customer.

    If company is provided, only returns invoices from that company.
    """
    if not _validate_customer_read(customer):
        return []

    limit = max(1, min(int(limit), 50))

    filters = {"customer": customer, "docstatus": 1}
    if company:
        filters["company"] = company

    invoices = frappe.get_list(
        "POS Invoice",
        filters=filters,
        fields=[
            "name", "posting_date", "grand_total", "status",
            "is_return", "currency", "total_qty",
        ],
        order_by="posting_date desc, creation desc",
        limit_page_length=limit,
    )
    return invoices


@frappe.whitelist()
def get_customer_outstanding(customer, company=""):
    """Get outstanding balance and credit limit for a customer.

    If company is provided, only calculates outstanding from that company's
    invoices and returns the credit limit for that company.
    """
    customer_doc = _validate_customer_read(customer)
    if not customer_doc:
        return {"outstanding": 0, "credit_limit": 0}

    # Use permission-aware document queries; raw SQL here would bypass Company
    # user permissions and could disclose accounting balances.
    validate_doctype_permissions("Sales Invoice", "read")
    invoice_filters = {"customer": customer, "docstatus": 1}
    if company:
        invoice_filters["company"] = company

    sales_outstanding = frappe.get_list(
        "Sales Invoice",
        filters=invoice_filters,
        pluck="outstanding_amount",
        limit_page_length=0,
    )
    pos_filters = dict(invoice_filters)
    pos_filters["consolidated_invoice"] = ["is", "not set"]
    pos_outstanding = frappe.get_list(
        "POS Invoice",
        filters=pos_filters,
        pluck="outstanding_amount",
        limit_page_length=0,
    )
    outstanding = sum(flt(value) for value in sales_outstanding + pos_outstanding)

    # Credit limit from Customer Credit Limit child table
    credit_limit = 0
    credit_limits = customer_doc.get("credit_limits") or []
    matching_limits = [
        row for row in credit_limits if not company or row.company == company
    ]
    if matching_limits:
        credit_limit = flt(matching_limits[0].credit_limit)

    return {
        "outstanding": outstanding,
        "credit_limit": credit_limit,
    }
