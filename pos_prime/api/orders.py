# Copyright (c) 2026, Ravindu Gajanayaka
# Licensed under GPLv3. See license.txt

import frappe
from frappe import _
from frappe.utils import flt
import json

from pos_prime.api._utils import (
    build_item_dict,
    format_invoice_response,
    validate_pos_access,
    validate_pos_document_access,
    validate_pos_payment_accounts,
    set_campaign_from_profile,
)


def _format_order_response(invoice):
    """Add stable source-row and tracking metadata to an invoice response."""
    response = format_invoice_response(invoice)
    tracking_by_item = {}

    for item, item_response in zip(invoice.items, response.get("items", [])):
        if item.item_code not in tracking_by_item:
            tracking_by_item[item.item_code] = frappe.get_cached_value(
                "Item",
                item.item_code,
                ["has_serial_no", "has_batch_no"],
                as_dict=True,
            ) or {}

        tracking = tracking_by_item[item.item_code]
        item_response.update(
            {
                "name": item.name,
                "source_row_name": item.get("pos_invoice_item") or item.name,
                "pos_invoice_item": item.get("pos_invoice_item"),
                "use_serial_batch_fields": item.get("use_serial_batch_fields") or 0,
                "has_serial_no": item.get("has_serial_no")
                or tracking.get("has_serial_no")
                or 0,
                "has_batch_no": item.get("has_batch_no")
                or tracking.get("has_batch_no")
                or 0,
            }
        )

    return response


def _parse_list(value, label):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            frappe.throw(_("{0} must be valid JSON.").format(label))

    if not isinstance(value, list):
        frappe.throw(_("{0} must be a list.").format(label))

    return value


def _normalise_serial_nos(value):
    if not value:
        return []
    if isinstance(value, (list, tuple)):
        serial_nos = value
    else:
        serial_nos = str(value).replace(",", "\n").splitlines()
    return [str(serial_no).strip() for serial_no in serial_nos if str(serial_no).strip()]


def _get_source_tracking(source_row_name):
    item_meta = frappe.get_meta("POS Invoice Item")
    fields = [
        fieldname
        for fieldname in (
            "serial_and_batch_bundle",
            "use_serial_batch_fields",
            "serial_no",
            "batch_no",
        )
        if item_meta.has_field(fieldname)
    ]
    if not fields:
        return {}
    return frappe.db.get_value(
        "POS Invoice Item", source_row_name, fields, as_dict=True
    ) or {}


def _validate_serial_selection(invoice_item, requested_item, requested_qty):
    """Validate legacy serial fields without overriding v15 bundle returns."""
    available_serials = _normalise_serial_nos(invoice_item.get("serial_no"))
    if not available_serials:
        if requested_item.get("serial_no"):
            frappe.throw(
                _("Serial numbers cannot be changed for item {0}.").format(
                    invoice_item.item_code
                )
            )
        return

    requested_serials = _normalise_serial_nos(requested_item.get("serial_no"))
    if not requested_serials:
        requested_serials = available_serials

    if len(requested_serials) != len(set(requested_serials)):
        frappe.throw(
            _("Duplicate serial numbers were selected for item {0}.").format(
                invoice_item.item_code
            )
        )

    unknown_serials = set(requested_serials) - set(available_serials)
    if unknown_serials:
        frappe.throw(
            _("Serial number {0} was not sold on this invoice row.").format(
                sorted(unknown_serials)[0]
            )
        )

    stock_qty = abs(flt(requested_qty) * flt(invoice_item.conversion_factor or 1))
    expected_count = round(stock_qty)
    if abs(stock_qty - expected_count) > 0.000001:
        frappe.throw(
            _("Return quantity for serialized item {0} must resolve to a whole number.").format(
                invoice_item.item_code
            )
        )
    if len(requested_serials) != expected_count:
        frappe.throw(
            _("Select {0} serial number(s) for item {1}.").format(
                expected_count, invoice_item.item_code
            )
        )

    invoice_item.serial_no = "\n".join(requested_serials)


def _apply_return_item_selection(invoice, requested_items):
    """Apply return quantities by original child-row identity."""
    requested_by_source_row = {}
    for requested_item in requested_items:
        if not isinstance(requested_item, dict):
            frappe.throw(_("Each return item must be an object."))

        source_row_name = requested_item.get("source_row_name") or requested_item.get(
            "pos_invoice_item"
        )
        if not source_row_name:
            frappe.throw(_("Every return item must include its original invoice row."))
        if source_row_name in requested_by_source_row:
            frappe.throw(
                _("Invoice row {0} was included more than once.").format(source_row_name)
            )
        requested_by_source_row[source_row_name] = requested_item

    invoice_rows = {}
    for invoice_item in invoice.items:
        source_row_name = invoice_item.get("pos_invoice_item")
        if not source_row_name:
            frappe.throw(
                _("Return invoice row {0} is not linked to its source invoice.").format(
                    invoice_item.idx
                )
            )
        invoice_rows[source_row_name] = invoice_item

    unknown_rows = set(requested_by_source_row) - set(invoice_rows)
    if unknown_rows:
        frappe.throw(
            _("Invoice row {0} is not available for return.").format(
                sorted(unknown_rows)[0]
            )
        )

    selected_rows = []
    for source_row_name, invoice_item in invoice_rows.items():
        requested_item = requested_by_source_row.get(source_row_name)
        if not requested_item:
            continue

        requested_qty = flt(requested_item.get("qty"))
        if requested_qty > 0:
            frappe.throw(
                _("Return quantity for item {0} must be zero or negative.").format(
                    invoice_item.item_code
                )
            )
        if requested_qty == 0:
            continue

        maximum_qty = abs(flt(invoice_item.qty))
        if abs(requested_qty) > maximum_qty + 0.000001:
            frappe.throw(
                _("Return quantity for item {0} cannot exceed {1}.").format(
                    invoice_item.item_code, maximum_qty
                )
            )

        source_tracking = _get_source_tracking(source_row_name)
        bundle_only = bool(
            source_tracking.get("serial_and_batch_bundle")
            and not source_tracking.get("use_serial_batch_fields")
        )
        if bundle_only and abs(abs(requested_qty) - maximum_qty) > 0.000001:
            frappe.throw(
                _(
                    "Item {0} uses a Serial and Batch Bundle and must be returned as a full invoice row."
                ).format(invoice_item.item_code)
            )
        if bundle_only and (
            requested_item.get("serial_no") or requested_item.get("batch_no")
        ):
            frappe.throw(
                _("Serial or batch allocation cannot be changed for item {0}.").format(
                    invoice_item.item_code
                )
            )

        requested_batch = requested_item.get("batch_no")
        if invoice_item.get("batch_no") and requested_batch not in (
            None,
            "",
            invoice_item.batch_no,
        ):
            frappe.throw(
                _("Batch number cannot be changed for item {0}.").format(
                    invoice_item.item_code
                )
            )
        _validate_serial_selection(invoice_item, requested_item, requested_qty)
        invoice_item.qty = requested_qty
        selected_rows.append(invoice_item)

    if not selected_rows:
        frappe.throw(_("At least one item with a non-zero quantity is required."))

    invoice.set("items", selected_rows)


def _get_allowed_return_payment(profile, mode_of_payment):
    if not mode_of_payment:
        frappe.throw(_("Select a refund method."))

    allowed_modes = {
        row.mode_of_payment
        for row in profile.get("payments") or []
        if row.get("allow_in_returns")
    }
    if mode_of_payment not in allowed_modes:
        frappe.throw(
            _("Mode of Payment {0} is not allowed for returns in POS Profile {1}.").format(
                mode_of_payment, profile.name
            )
        )

    return mode_of_payment


def _get_requested_payment_mode(payments):
    payments = _parse_list(payments, _("Payments"))
    if len(payments) != 1 or not isinstance(payments[0], dict):
        frappe.throw(_("Select exactly one refund method."))
    return payments[0].get("mode_of_payment")


def _set_return_payment(invoice, mode_of_payment, profile=None):
    """Set the refund from ERPNext's calculated total, never client arithmetic."""
    invoice.calculate_taxes_and_totals()
    invoice_total = flt(invoice.rounded_total) or flt(invoice.grand_total)
    if invoice_total > 0:
        frappe.throw(_("A return invoice cannot have a positive total."))

    profile = profile or frappe.get_doc("POS Profile", invoice.pos_profile)
    if invoice_total:
        payment_info = validate_pos_payment_accounts(
            profile,
            [{"mode_of_payment": mode_of_payment, "amount": invoice_total}],
            company=invoice.company,
            currency=invoice.currency,
            returns_only=True,
        )[mode_of_payment]
    else:
        # A zero-total return needs ERPNext's placeholder row but posts no
        # payment GL. Resolve the current mapping without applying a currency
        # rule to an account that will not be used.
        from erpnext.accounts.doctype.sales_invoice.sales_invoice import (
            get_mode_of_payment_info,
        )

        payment_rows = get_mode_of_payment_info(mode_of_payment, invoice.company)
        if not payment_rows:
            frappe.throw(
                _("Mode of Payment {0} has no account for company {1}.").format(
                    mode_of_payment, invoice.company
                )
            )
        payment_info = payment_rows[0]
    base_amount = flt(invoice_total * flt(invoice.conversion_rate or 1))
    invoice.set("payments", [])
    invoice.append(
        "payments",
        {
            "mode_of_payment": mode_of_payment,
            "type": payment_info.get("type"),
            "account": payment_info.get("default_account")
            or payment_info.get("account"),
            "default": payment_info.get("default") or 0,
            "amount": invoice_total,
            "base_amount": base_amount,
        },
    )
    invoice.paid_amount = invoice_total
    invoice.base_paid_amount = base_amount
    invoice.change_amount = 0
    invoice.base_change_amount = 0


def _resync_return_payment(invoice, mode_of_payment, profile=None):
    expected_total = flt(invoice.rounded_total) or flt(invoice.grand_total)
    current_amount = flt(invoice.payments[0].amount) if len(invoice.payments) == 1 else 0
    if flt(invoice.paid_amount) != expected_total or current_amount != expected_total:
        _set_return_payment(invoice, mode_of_payment, profile)
        invoice.save()


def _validate_manual_return_item(item_data):
    if not isinstance(item_data, dict):
        frappe.throw(_("Each return item must be an object."))

    item_code = item_data.get("item_code")
    qty = flt(item_data.get("qty"))
    rate = flt(item_data.get("rate"))
    if not item_code:
        frappe.throw(_("Item code is required for every return row."))
    if qty <= 0:
        frappe.throw(
            _("Quantity for item {0} must be greater than zero.").format(item_code)
        )
    if rate < 0:
        frappe.throw(_("Rate for item {0} cannot be negative.").format(item_code))

    tracking = frappe.get_cached_value(
        "Item",
        item_code,
        ["has_serial_no", "has_batch_no"],
        as_dict=True,
    )
    if not tracking:
        frappe.throw(_("Item {0} does not exist.").format(item_code))
    if tracking.has_serial_no:
        frappe.throw(
            _(
                "Serialized item {0} must be returned from Past Orders so its original serial numbers can be verified."
            ).format(item_code)
        )
    if tracking.has_batch_no:
        batch_no = item_data.get("batch_no")
        if not batch_no:
            frappe.throw(_("Batch number is required for item {0}.").format(item_code))
        if not frappe.db.exists("Batch", {"name": batch_no, "item": item_code}):
            frappe.throw(
                _("Batch {0} does not belong to item {1}.").format(
                    batch_no, item_code
                )
            )
        item_data["use_serial_batch_fields"] = 1

    return qty, rate


@frappe.whitelist()
def get_past_orders(pos_profile, search_term="", status="", limit=20, start=0):
    """Get paginated list of past POS Invoices with search/filter."""
    validate_pos_access(pos_profile)

    try:
        filters = {
            "pos_profile": pos_profile,
        }

        if status == "Paid":
            filters["docstatus"] = 1
            filters["is_return"] = 0
            filters["outstanding_amount"] = ["<=", 0]
        elif status == "Return":
            filters["docstatus"] = 1
            filters["is_return"] = 1
        elif status == "Draft":
            filters["docstatus"] = 0
        elif status == "Consolidated":
            filters["docstatus"] = 1
            filters["status"] = "Consolidated"
        else:
            # All -- no extra filter
            pass

        or_filters = {}
        if search_term:
            search = f"%{search_term}%"
            or_filters = {
                "name": ["like", search],
                "customer_name": ["like", search],
                "customer": ["like", search],
            }

        orders = frappe.get_list(
            "POS Invoice",
            filters=filters,
            or_filters=or_filters if or_filters else None,
            fields=[
                "name",
                "customer",
                "customer_name",
                "grand_total",
                "net_total",
                "paid_amount",
                "posting_date",
                "posting_time",
                "status",
                "docstatus",
                "is_return",
                "return_against",
                "owner",
                "modified",
                "currency",
                "total_qty",
                "rounded_total",
                "change_amount",
                "outstanding_amount",
                "total_taxes_and_charges",
                "discount_amount",
                "additional_discount_percentage",
                "write_off_amount",
                "loyalty_points",
                "loyalty_amount",
                "coupon_code",
                "sales_partner",
                "po_no",
                "project",
                "remarks",
                "consolidated_invoice",
            ],
            order_by="creation desc",
            start=int(start),
            page_length=int(limit),
        )

        return orders
    except frappe.PermissionError:
        raise
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "POS Prime: get_past_orders failed")
        frappe.throw(_("Failed to fetch past orders: {0}").format(str(e)))


@frappe.whitelist()
def get_order_detail(invoice_name):
    """Get full details of a POS Invoice with ALL fields."""
    try:
        if not invoice_name:
            frappe.throw(_("Invoice name is required."))

        if not frappe.has_permission("POS Invoice", "read", invoice_name):
            frappe.throw(
                _("You do not have permission to view this invoice."),
                frappe.PermissionError,
            )

        invoice = frappe.get_doc("POS Invoice", invoice_name)
        validate_pos_access(invoice.pos_profile)
        return _format_order_response(invoice)
    except frappe.PermissionError:
        raise
    except frappe.DoesNotExistError:
        frappe.throw(_("Invoice {0} does not exist").format(invoice_name))
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "POS Prime: get_order_detail failed")
        frappe.throw(_("Failed to fetch order detail: {0}").format(str(e)))


@frappe.whitelist()
def create_return_invoice(source_invoice):
    """Create a return POS Invoice from an existing one using ERPNext's built-in method."""
    try:
        if not source_invoice:
            frappe.throw(_("Source invoice is required."))
        if not frappe.db.exists("POS Invoice", source_invoice):
            frappe.throw(_("Source invoice {0} does not exist").format(source_invoice))

        source_doc = frappe.get_doc("POS Invoice", source_invoice)
        source_doc.check_permission("read")
        validate_pos_access(
            source_doc.pos_profile,
            permissions=("read", "create"),
        )
        if source_doc.docstatus != 1:
            frappe.throw(_("Source invoice {0} must be submitted before creating a return").format(source_invoice))
        if source_doc.is_return:
            frappe.throw(_("A return cannot be created against another return invoice."))

        try:
            from erpnext.accounts.doctype.pos_invoice.pos_invoice import make_sales_return
        except ImportError:
            try:
                from erpnext.accounts.doctype.sales_invoice.sales_invoice import make_sales_return
            except ImportError:
                make_sales_return = None

        if make_sales_return is None:
            frappe.throw(_("Return invoice creation is not supported in this ERPNext version."))

        return_doc = make_sales_return(source_invoice)
        return_doc.insert()
        return _format_order_response(return_doc)
    except frappe.PermissionError:
        raise
    except frappe.ValidationError:
        raise
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "POS Prime: create_return_invoice failed")
        frappe.throw(_("Failed to create return invoice: {0}").format(str(e)))


@frappe.whitelist()
def submit_return_invoice(invoice_name, items, payments):
    """Adjust return items and submit."""
    try:
        if not isinstance(invoice_name, str) or not invoice_name.strip():
            frappe.throw(_("Return invoice name is required."))

        items = _parse_list(items, _("Items"))

        if not frappe.db.exists("POS Invoice", invoice_name):
            frappe.throw(_("Invoice {0} does not exist").format(invoice_name))

        invoice = frappe.get_doc("POS Invoice", invoice_name)
        validate_pos_access(
            invoice.pos_profile,
            permissions=("write", "submit"),
        )
        validate_pos_document_access(
            invoice,
            permissions=("write", "submit"),
            owner_field="owner",
        )
        invoice.check_permission("write")
        invoice.check_permission("submit")

        if invoice.docstatus != 0:
            frappe.throw(_("Invoice {0} is not a draft").format(invoice_name))
        if not invoice.is_return or not invoice.return_against:
            frappe.throw(_("Invoice {0} is not a linked return draft.").format(invoice_name))

        profile = frappe.get_doc("POS Profile", invoice.pos_profile)
        mode_of_payment = _get_allowed_return_payment(
            profile, _get_requested_payment_mode(payments)
        )

        _apply_return_item_selection(invoice, items)
        _set_return_payment(invoice, mode_of_payment, profile)
        invoice.save()
        _resync_return_payment(invoice, mode_of_payment, profile)
        invoice.submit()

        return _format_order_response(invoice)
    except frappe.PermissionError:
        raise
    except frappe.ValidationError:
        raise
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "POS Prime: submit_return_invoice failed")
        frappe.throw(_("Failed to submit return invoice: {0}").format(str(e)))


@frappe.whitelist()
def discard_return_invoice(invoice_name):
    """Delete an unsubmitted linked-return draft when its dialog is abandoned."""
    if not isinstance(invoice_name, str) or not invoice_name.strip():
        frappe.throw(_("Return invoice name is required."))

    if not frappe.db.exists("POS Invoice", invoice_name):
        return {"discarded": False}

    invoice = frappe.get_doc("POS Invoice", invoice_name)
    validate_pos_document_access(
        invoice,
        permissions="delete",
        owner_field="owner",
    )
    invoice.check_permission("delete")
    if invoice.docstatus != 0 or not invoice.is_return or not invoice.return_against:
        frappe.throw(_("Only an unsubmitted linked-return draft can be discarded."))

    invoice.delete()
    return {"discarded": True}


@frappe.whitelist()
def email_invoice(invoice_name, recipient, content=""):
    """Email a POS Invoice receipt as PDF to the given recipient."""
    if not frappe.has_permission("POS Invoice", "read", invoice_name):
        frappe.throw(
            _("You do not have permission to view this invoice."),
            frappe.PermissionError,
        )

    if not recipient or not recipient.strip():
        frappe.throw(_("Recipient email is required"))

    invoice = frappe.get_doc("POS Invoice", invoice_name)

    # Determine print format from POS Profile or fallback
    print_format = None
    if invoice.pos_profile:
        print_format = frappe.db.get_value("POS Profile", invoice.pos_profile, "print_format")
    if not print_format:
        print_format = frappe.db.get_single_value("Print Settings", "print_format") or "Standard"

    subject = _("Invoice {0}").format(invoice_name)
    message = content.strip() if content and content.strip() else _("Please find your invoice attached.")

    attachments = [frappe.attach_print("POS Invoice", invoice_name, print_format=print_format)]

    frappe.sendmail(
        recipients=[recipient.strip()],
        subject=subject,
        message=message,
        reference_doctype="POS Invoice",
        reference_name=invoice_name,
        attachments=attachments,
    )

    return {"success": True, "message": _("Email sent to {0}").format(recipient.strip())}


@frappe.whitelist()
def create_manual_return(customer, pos_profile, items, mode_of_payment):
    """Create and submit a return POS Invoice from manually entered items.

    Items should have positive qty/rate — they will be negated automatically.
    """
    try:
        validate_pos_access(
            pos_profile,
            permissions=("create", "submit"),
        )
        items = _parse_list(items, _("Items"))
        if not items:
            frappe.throw(_("At least one item is required for a return"))

        profile = frappe.get_doc("POS Profile", pos_profile)
        mode_of_payment = _get_allowed_return_payment(profile, mode_of_payment)

        invoice = frappe.get_doc({
            "doctype": "POS Invoice",
            "customer": customer,
            "company": profile.company,
            "pos_profile": pos_profile,
            "is_pos": 1,
            "is_return": 1,
            "selling_price_list": profile.selling_price_list or "Standard Selling",
            "currency": profile.currency
            or frappe.defaults.get_defaults().get("currency", "USD"),
            "set_warehouse": profile.warehouse,
            "account_for_change_amount": profile.account_for_change_amount,
            "write_off_account": profile.write_off_account,
            "write_off_cost_center": profile.write_off_cost_center,
            "apply_discount_on": profile.apply_discount_on or "Grand Total",
            "ignore_pricing_rule": 1 if profile.ignore_pricing_rule else 0,
        })

        if profile.company_address:
            invoice.company_address = profile.company_address

        if profile.taxes_and_charges:
            invoice.taxes_and_charges = profile.taxes_and_charges
        if profile.tax_category:
            invoice.tax_category = profile.tax_category

        # Campaign from profile (v14/v15: campaign, v16: utm_campaign)
        set_campaign_from_profile(invoice, profile)

        # Add items with negative qty
        for item_data in items:
            qty, rate = _validate_manual_return_item(item_data)

            item_dict = build_item_dict(item_data, profile)
            item_dict["qty"] = -qty
            item_dict["rate"] = rate
            item_dict["price_list_rate"] = rate
            invoice.append("items", item_dict)

        invoice.set_missing_values()
        _set_return_payment(invoice, mode_of_payment, profile)
        invoice.insert()
        _resync_return_payment(invoice, mode_of_payment, profile)
        invoice.submit()

        return _format_order_response(invoice)
    except frappe.PermissionError:
        raise
    except frappe.ValidationError:
        raise
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "POS Prime: create_manual_return failed")
        frappe.throw(_("Failed to process return: {0}").format(str(e)))
