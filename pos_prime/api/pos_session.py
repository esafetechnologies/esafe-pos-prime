# Copyright (c) 2026, Ravindu Gajanayaka
# Licensed under GPLv3. See license.txt

import frappe
from frappe import _
from frappe.utils import flt, nowdate, nowtime
import json
import erpnext
from pos_prime.api._utils import (
    is_pos_manager,
    validate_doctype_permissions,
    validate_pos_access,
    validate_pos_document_access,
    validate_pos_payment_accounts,
)


def _erpnext_major_version():
    try:
        return int(erpnext.__version__.split(".")[0])
    except (AttributeError, TypeError, ValueError):
        return 15


def _build_payment_summary(opening, closing_entry):
    """Normalize v15/v16 reconciliation rows to opening + sales totals."""
    opening_amounts = {}
    mode_order = []
    for detail in opening.balance_details:
        mode = detail.mode_of_payment
        if mode not in opening_amounts:
            mode_order.append(mode)
            opening_amounts[mode] = 0
        opening_amounts[mode] += flt(detail.opening_amount)

    sales_amounts = {mode: 0 for mode in mode_order}
    is_v16 = _erpnext_major_version() >= 16
    for row in closing_entry.payment_reconciliation:
        mode = row.mode_of_payment
        if mode not in mode_order:
            mode_order.append(mode)
            opening_amounts[mode] = flt(row.get("opening_amount"))
            sales_amounts[mode] = 0

        if is_v16:
            sales_amounts[mode] += flt(row.expected_amount)
        else:
            row_opening = opening_amounts.get(mode, flt(row.get("opening_amount")))
            sales_amounts[mode] += flt(row.expected_amount) - row_opening

    return [
        {
            "mode_of_payment": mode,
            "opening_amount": flt(opening_amounts.get(mode)),
            "sales_amount": flt(sales_amounts.get(mode)),
            "expected_amount": flt(opening_amounts.get(mode))
            + flt(sales_amounts.get(mode)),
        }
        for mode in mode_order
    ]


def _validate_opening_access(opening, permissions="read"):
    validate_pos_document_access(
        opening,
        permissions=permissions,
        owner_field="user",
    )


def _validate_closing_payment_accounts(opening, closing):
    """Preflight every payment account before a closing document is inserted."""
    profile = frappe.get_doc("POS Profile", opening.pos_profile)

    modes_by_currency = {}
    for invoice_name in sorted(_closing_invoice_names(closing)):
        invoice = frappe.get_doc("POS Invoice", invoice_name)
        key = (invoice.company, invoice.currency)
        modes = modes_by_currency.setdefault(key, set())
        for payment in invoice.get("payments") or []:
            if flt(payment.get("amount")):
                modes.add(payment.get("mode_of_payment"))

    for (company, currency), modes in modes_by_currency.items():
        if not modes:
            continue
        validate_pos_payment_accounts(
            profile,
            [
                {"mode_of_payment": mode, "amount": 1}
                for mode in sorted(modes, key=lambda value: str(value))
            ],
            company=company,
            currency=currency,
            # A legacy invoice must remain closable if its payment mode was
            # later removed or disabled. Validate its current company mapping;
            # the historical account value is intentionally not authoritative.
            require_profile_membership=False,
            require_enabled=False,
        )


def _closing_invoice_names(closing):
    transactions = closing.get("pos_transactions") or closing.get("pos_invoices") or []
    return {
        row.get("pos_invoice") for row in transactions if row.get("pos_invoice")
    }


def _closing_invoice_snapshot(closing):
    """Return a version-neutral, typed invoice set for safe failed retries."""
    invoice_keys = set()
    for collection, fieldname, doctype in (
        ("pos_transactions", "pos_invoice", "POS Invoice"),
        ("pos_invoices", "pos_invoice", "POS Invoice"),
        ("sales_invoices", "sales_invoice", "Sales Invoice"),
    ):
        for row in closing.get(collection) or []:
            if row.get(fieldname):
                invoice_keys.add((doctype, row.get(fieldname)))
    return invoice_keys


def _failed_closing_matches_current(failed_closing, current_closing):
    """Reject retry only when its frozen invoice scope is no longer safe."""
    failed = _closing_invoice_snapshot(failed_closing)
    current = _closing_invoice_snapshot(current_closing)
    failed_pos = {name for doctype, name in failed if doctype == "POS Invoice"}
    current_pos = {name for doctype, name in current if doctype == "POS Invoice"}
    failed_sales = {name for doctype, name in failed if doctype == "Sales Invoice"}
    current_sales = {name for doctype, name in current if doctype == "Sales Invoice"}

    # POS Invoices remain discoverable until they consolidate, so a difference
    # indicates new invoices or a partial merge. V16 links Sales Invoices to the
    # closing before an async job finishes, so they may disappear from a fresh
    # snapshot; only newly discovered Sales Invoices make the retry unsafe.
    return current_pos == failed_pos and current_sales.issubset(failed_sales)


@frappe.whitelist()
def get_opening_data():
    """Get data needed for opening a POS shift."""
    validate_pos_access(permissions="read", doctype="POS Opening Entry")
    user = frappe.session.user

    # Check for existing open entry
    existing_entries = frappe.get_list(
        "POS Opening Entry",
        filters={"user": user, "status": "Open", "docstatus": 1},
        fields=["name", "pos_profile", "company"],
        limit_page_length=1,
    )
    existing = existing_entries[0] if existing_entries else None

    # Get available POS Profiles
    profiles = frappe.get_list(
        "POS Profile",
        filters={"disabled": 0},
        fields=["name", "company"],
    )
    profiles = [
        profile
        for profile in profiles
        if not frappe.db.exists(
            "POS Profile User",
            {"parent": profile.name, "parenttype": "POS Profile"},
        )
        or frappe.db.exists(
            "POS Profile User",
            {
                "parent": profile.name,
                "parenttype": "POS Profile",
                "user": user,
            },
        )
    ]

    return {
        "opening_entry": existing,
        "pos_profiles": profiles,
    }


@frappe.whitelist()
def get_shift_summary(opening_entry):
    """Get per-payment-method breakdown for closing shift.

    Uses ERPNext's built-in make_closing_entry_from_opening to get
    accurate invoice data with proper timestamp range filtering.
    """
    from erpnext.accounts.doctype.pos_closing_entry.pos_closing_entry import (
        make_closing_entry_from_opening,
    )

    opening = frappe.get_doc("POS Opening Entry", opening_entry)
    _validate_opening_access(opening)
    closing_entry = make_closing_entry_from_opening(opening)
    payment_summary = _build_payment_summary(opening, closing_entry)

    # v14/v15 use pos_transactions, v16 uses pos_invoices
    transactions = (
        closing_entry.get("pos_transactions")
        or closing_entry.get("pos_invoices")
        or []
    )

    return {
        "grand_total": closing_entry.grand_total,
        "net_total": closing_entry.net_total,
        "total_quantity": closing_entry.total_quantity,
        "num_invoices": len(transactions),
        "payment_summary": payment_summary,
    }


@frappe.whitelist()
def check_opening_entry(user=""):
    """Check if user has an open POS Opening Entry."""
    validate_pos_access(permissions="read", doctype="POS Opening Entry")
    user = user or frappe.session.user
    if user != frappe.session.user and not is_pos_manager():
        frappe.throw(
            _("You can only check your own POS opening entry."),
            frappe.PermissionError,
        )
    entries = frappe.get_list(
        "POS Opening Entry",
        filters={"user": user, "docstatus": 1, "status": "Open"},
        fields=["name", "pos_profile", "company"],
    )
    return entries


@frappe.whitelist()
def create_opening_entry(pos_profile, company, balance_details):
    """Create a new POS Opening Entry."""
    validate_pos_access(
        pos_profile,
        permissions=("create", "submit"),
        doctype="POS Opening Entry",
    )
    if isinstance(balance_details, str):
        balance_details = json.loads(balance_details)
    if not isinstance(balance_details, list):
        frappe.throw(_("Opening balances must be a list."))

    profile_company = frappe.db.get_value("POS Profile", pos_profile, "company")
    if company != profile_company:
        frappe.throw(_("Company must match the selected POS Profile."))

    profile = frappe.get_doc("POS Profile", pos_profile)
    allowed_modes = {row.mode_of_payment for row in profile.payments}
    seen_modes = set()

    doc = frappe.get_doc({
        "doctype": "POS Opening Entry",
        "user": frappe.session.user,
        "pos_profile": pos_profile,
        "company": company,
        "period_start_date": nowdate() + " " + nowtime(),
    })
    for detail in balance_details:
        mode_of_payment = detail.get("mode_of_payment")
        opening_amount = flt(detail.get("opening_amount"))
        if (
            not mode_of_payment
            or mode_of_payment in seen_modes
            or mode_of_payment not in allowed_modes
        ):
            frappe.throw(_("Opening balances contain an invalid payment mode."))
        if opening_amount < 0:
            frappe.throw(_("Opening amount cannot be negative."))
        seen_modes.add(mode_of_payment)
        doc.append("balance_details", {
            "mode_of_payment": mode_of_payment,
            "opening_amount": opening_amount,
        })
    doc.insert()
    doc.submit()
    return doc.as_dict()


@frappe.whitelist()
def get_pos_profile(pos_profile):
    """Get full POS Profile document with company default currency."""
    validate_pos_access(pos_profile, permissions="read", doctype="POS Profile")
    doc = frappe.get_doc("POS Profile", pos_profile)
    result = doc.as_dict()
    if not result.get("currency") and result.get("company"):
        result["currency"] = frappe.db.get_value(
            "Company", result["company"], "default_currency"
        )

    # Normalize v14/v15/v16 field differences so frontend reads consistent keys:
    # v16 renamed disable_grand_total_to_default_mop → set_grand_total_to_default_mop (inverted)
    if "set_grand_total_to_default_mop" in result and "disable_grand_total_to_default_mop" not in result:
        result["disable_grand_total_to_default_mop"] = not result["set_grand_total_to_default_mop"]
    # v14: field doesn't exist at all — default to False (pre-fill enabled)
    if "disable_grand_total_to_default_mop" not in result:
        result["disable_grand_total_to_default_mop"] = False

    # Expose ERPNext major version so frontend can toggle version-specific features
    result["erpnext_version"] = int(erpnext.__version__.split(".")[0])

    return result


@frappe.whitelist()
def get_branding(company=""):
    """Get app logo and favicon for the POS UI.

    Reads from Website Settings (app_logo, favicon) with fallback to Company logo.
    Uses frappe.db.get_single_value so no doctype read permission is needed.
    """
    validate_pos_access()
    app_logo = frappe.db.get_single_value("Website Settings", "app_logo") or ""
    favicon = frappe.db.get_single_value("Website Settings", "favicon") or ""

    company_logo = ""
    company_abbr = ""
    company_name = ""
    if company:
        company_doc = frappe.get_doc("Company", company)
        validate_doctype_permissions("Company", "read", doc=company_doc)
        comp_data = frappe.db.get_value(
            "Company", company, ["company_logo", "abbr", "company_name"], as_dict=True
        )
        if comp_data:
            company_logo = comp_data.company_logo or ""
            company_abbr = comp_data.abbr or ""
            company_name = comp_data.company_name or company

    return {
        "app_logo": app_logo,
        "favicon": favicon,
        "company_logo": company_logo,
        "company_abbr": company_abbr,
        "company_name": company_name,
    }


@frappe.whitelist()
def get_invoice_option_lists():
    """Get dropdown options for Invoice Options panel.

    Returns only the option lists the current user is allowed to read.
    """
    validate_pos_access()
    def permitted_list(doctype, filters=None, limit=50):
        if not frappe.has_permission(doctype, ptype="read"):
            return []
        return frappe.get_list(
            doctype,
            filters=filters,
            fields=["name"],
            limit_page_length=limit,
            order_by="name asc",
        )

    return {
        "sales_partners": permitted_list("Sales Partner", limit=100),
        "projects": permitted_list("Project", {"status": "Open"}, limit=100),
        "shipping_rules": permitted_list("Shipping Rule"),
        "terms_and_conditions": permitted_list("Terms and Conditions"),
        "payment_terms_templates": permitted_list("Payment Terms Template"),
    }


@frappe.whitelist()
def get_user_info():
    """Get current user's display info (full name and avatar).

    Uses frappe.db.get_value so no User doctype read permission is needed.
    """
    validate_pos_access()
    user = frappe.session.user
    data = frappe.db.get_value(
        "User", user, ["full_name", "user_image"], as_dict=True
    )
    return {
        "full_name": data.full_name if data else user,
        "user_image": data.user_image if data else None,
    }


@frappe.whitelist()
def get_opening_entry_detail(entry_name):
    """Get POS Opening Entry status details for kiosk initialization.

    Returns essential fields without requiring direct doctype read permission.
    """
    if not frappe.db.exists("POS Opening Entry", entry_name):
        frappe.throw(_("POS Opening Entry {0} does not exist").format(entry_name))
    opening = frappe.get_doc("POS Opening Entry", entry_name)
    _validate_opening_access(opening)
    return {
        field: opening.get(field)
        for field in ("name", "docstatus", "status", "pos_profile", "company", "user")
    }


@frappe.whitelist()
def close_shift(opening_entry, closing_amounts=None):
    """Create a POS Closing Entry using ERPNext's built-in logic.

    Uses make_closing_entry_from_opening which correctly fetches invoices
    by timestamp range, user, pos_profile, and excludes already-consolidated ones.
    """
    from erpnext.accounts.doctype.pos_closing_entry.pos_closing_entry import (
        make_closing_entry_from_opening,
    )

    if isinstance(closing_amounts, str):
        closing_amounts = json.loads(closing_amounts)

    # Validate opening entry exists
    if not frappe.db.exists("POS Opening Entry", opening_entry):
        frappe.throw(_("Opening entry {0} does not exist").format(opening_entry))

    opening = frappe.get_doc("POS Opening Entry", opening_entry)
    _validate_opening_access(opening)
    validate_doctype_permissions(
        "POS Closing Entry",
        permissions=("create", "submit"),
    )

    # Serialize close attempts for this shift. ERPNext commits after synchronous
    # consolidation, and queued closes persist before the request releases the
    # lock, so a second request will observe the same closing document.
    frappe.db.sql(
        "SELECT name FROM `tabPOS Opening Entry` WHERE name = %s FOR UPDATE",
        (opening_entry,),
    )
    opening.reload()
    _validate_opening_access(opening)

    # A failed consolidation is still a submitted closing document. Reuse it
    # instead of creating duplicates on every retry.
    existing_closing_name = frappe.db.get_value(
        "POS Closing Entry",
        {"pos_opening_entry": opening_entry, "docstatus": 1},
        "name",
        order_by="creation desc",
    )
    if existing_closing_name:
        existing_closing = frappe.get_doc(
            "POS Closing Entry", existing_closing_name
        )
        if existing_closing.status == "Failed":
            if opening.status != "Open" or opening.docstatus != 1:
                frappe.throw(
                    _("Opening entry {0} is not available for retry.").format(
                        opening_entry
                    )
                )

            current_closing = make_closing_entry_from_opening(opening)
            if not _failed_closing_matches_current(
                existing_closing, current_closing
            ):
                frappe.throw(
                    _(
                        "Failed closing entry {0} no longer matches the shift invoices. "
                        "Resolve or cancel it before closing this shift again."
                    ).format(existing_closing.name)
                )

            _validate_closing_payment_accounts(opening, existing_closing)
            existing_closing.retry()
            existing_closing.reload()

        return {
            "name": existing_closing.name,
            "status": existing_closing.status,
        }

    if opening.status != "Open" or opening.docstatus != 1:
        frappe.throw(_("Opening entry {0} is not open or not submitted").format(opening_entry))

    # Use ERPNext's built-in function to build the closing entry
    closing = make_closing_entry_from_opening(opening)
    _validate_closing_payment_accounts(opening, closing)

    payment_summary = _build_payment_summary(opening, closing)

    # Apply user-provided closing amounts
    closing_lookup = {}
    if closing_amounts:
        for ca in closing_amounts:
            mode = ca.get("mode_of_payment")
            if not mode or mode in closing_lookup:
                frappe.throw(_("Closing amounts contain a missing or duplicate payment mode."))
            amount = flt(ca.get("closing_amount"))
            if amount < 0:
                frappe.throw(_("Closing amount cannot be negative."))
            closing_lookup[mode] = amount

    expected_modes = {row["mode_of_payment"] for row in payment_summary}
    unknown_modes = set(closing_lookup) - expected_modes
    if unknown_modes:
        frappe.throw(
            _("Unknown payment mode(s): {0}").format(", ".join(sorted(unknown_modes)))
        )

    # ERPNext v16's server helper omits the opening rows that its Desk client
    # normally prepends. Rebuild the table so physical closing cash is compared
    # with opening cash plus the shift's net payments on every supported version.
    closing.set("payment_reconciliation", [])
    for summary in payment_summary:
        closing_amount = closing_lookup.get(summary["mode_of_payment"], 0)
        closing.append(
            "payment_reconciliation",
            {
                "mode_of_payment": summary["mode_of_payment"],
                "opening_amount": summary["opening_amount"],
                "expected_amount": summary["expected_amount"],
                "closing_amount": closing_amount,
                "difference": flt(closing_amount - summary["expected_amount"]),
            },
        )

    closing.posting_date = nowdate()
    closing.posting_time = nowtime()

    closing.insert()
    closing.submit()
    closing.reload()

    return {"name": closing.name, "status": closing.status}
