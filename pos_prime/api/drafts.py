# Copyright (c) 2026, Ravindu Gajanayaka
# Licensed under GPLv3. See license.txt

import frappe
from frappe import _
import json

from pos_prime.api._utils import (
    apply_item_discount_policy,
    build_item_dict,
    set_invoice_optional_fields,
    format_invoice_response,
    validate_pos_access,
    safe_float,
    get_product_bundle_items,
    validate_bundle_stock,
    set_campaign_from_profile,
    configure_pos_settlement,
    get_allowed_invoice_discount,
    get_remaining_invoice_amount,
    is_pos_manager,
    set_pos_change_amount,
    set_pos_paid_amount,
    validate_pos_document_access,
    validate_supplemental_settlement,
)


@frappe.whitelist()
def save_draft_invoice(
    customer,
    pos_profile,
    items,
    payments=None,
    # Address & contact
    customer_address=None,
    shipping_address_name=None,
    contact_person=None,
    # Currency
    conversion_rate=None,
    price_list_currency=None,
    plc_conversion_rate=None,
    # Commission
    sales_partner=None,
    commission_rate=None,
    # Document details
    project=None,
    cost_center=None,
    remarks=None,
    po_no=None,
    po_date=None,
    set_posting_time=False,
    posting_date=None,
    posting_time=None,
    naming_series=None,
    # Shipping & terms
    shipping_rule=None,
    tc_name=None,
    terms=None,
    # Printing
    letter_head=None,
    select_print_heading=None,
    group_same_items=False,
    language=None,
    # Payment terms
    payment_terms_template=None,
    # Discount
    additional_discount_percentage=0,
    discount_amount=0,
    apply_discount_on="Grand Total",
    coupon_code=None,
):
    """Save a POS Invoice as draft (insert without submit).

    Supports all optional fields for full POS Invoice parity.
    """
    # Permission check
    validate_pos_access(pos_profile, permissions="create", doctype="POS Invoice")

    if isinstance(items, str):
        items = json.loads(items)
    if isinstance(payments, str):
        payments = json.loads(payments)

    # Input validation
    if not items:
        frappe.throw(_("Items cannot be empty"))

    profile = frappe.get_doc("POS Profile", pos_profile)
    additional_discount_percentage, discount_amount = get_allowed_invoice_discount(
        profile, additional_discount_percentage, discount_amount
    )

    invoice = frappe.get_doc(
        {
            "doctype": "POS Invoice",
            "customer": customer,
            "company": profile.company,
            "pos_profile": pos_profile,
            "is_pos": 1,
            "selling_price_list": profile.selling_price_list or "Standard Selling",
            "currency": profile.currency
            or frappe.defaults.get_defaults().get("currency", "USD"),
            "set_warehouse": profile.warehouse,
            "update_stock": getattr(profile, "update_stock", 0),
            "account_for_change_amount": profile.account_for_change_amount,
            "write_off_account": profile.write_off_account,
            "write_off_cost_center": profile.write_off_cost_center,
            "ignore_pricing_rule": 1 if profile.ignore_pricing_rule else 0,
            "disable_rounded_total": 1 if profile.disable_rounded_total else 0,
        }
    )

    # Company address from POS Profile
    if profile.company_address:
        invoice.company_address = profile.company_address

    for item_data in items:
        invoice.append("items", build_item_dict(item_data, profile))

    if payments:
        for payment_data in payments:
            invoice.append(
                "payments",
                {
                    "mode_of_payment": payment_data.get("mode_of_payment"),
                    "amount": safe_float(payment_data.get("amount", 0)),
                },
            )

    # POS Invoice requires at least one payment entry even for drafts
    if not invoice.payments:
        default_mop = (profile.payments[0].mode_of_payment if profile.payments else "Cash")
        invoice.append("payments", {"mode_of_payment": default_mop, "amount": 0})

    # Set taxes template
    if profile.taxes_and_charges:
        invoice.taxes_and_charges = profile.taxes_and_charges
    if profile.tax_category:
        invoice.tax_category = profile.tax_category

    # Campaign from profile (v14/v15: campaign, v16: utm_campaign)
    set_campaign_from_profile(invoice, profile)

    # Discounts
    if additional_discount_percentage:
        invoice.additional_discount_percentage = safe_float(additional_discount_percentage)
    if discount_amount:
        invoice.discount_amount = safe_float(discount_amount)
    if apply_discount_on:
        invoice.apply_discount_on = apply_discount_on
    if coupon_code:
        invoice.coupon_code = coupon_code

    # Set all optional fields
    set_invoice_optional_fields(
        invoice,
        profile,
        customer_address=customer_address,
        shipping_address_name=shipping_address_name,
        contact_person=contact_person,
        conversion_rate=conversion_rate,
        price_list_currency=price_list_currency,
        plc_conversion_rate=plc_conversion_rate,
        sales_partner=sales_partner,
        commission_rate=commission_rate,
        project=project,
        cost_center=cost_center,
        remarks=remarks,
        po_no=po_no,
        po_date=po_date,
        set_posting_time=set_posting_time,
        posting_date=posting_date,
        posting_time=posting_time,
        naming_series=naming_series,
        shipping_rule=shipping_rule,
        tc_name=tc_name,
        terms=terms,
        letter_head=letter_head,
        select_print_heading=select_print_heading,
        group_same_items=group_same_items,
        language=language,
        payment_terms_template=payment_terms_template,
    )

    invoice.set_missing_values()
    apply_item_discount_policy(invoice, items, profile)
    invoice.insert()
    # Do NOT submit -- leave as draft

    return format_invoice_response(invoice)


@frappe.whitelist()
def get_draft_invoices(pos_profile):
    """List draft POS Invoices for the current POS profile."""
    validate_pos_access(pos_profile, permissions="read", doctype="POS Invoice")

    drafts = frappe.get_list(
        "POS Invoice",
        filters={
            "pos_profile": pos_profile,
            "docstatus": 0,
            **({} if is_pos_manager() else {"owner": frappe.session.user}),
        },
        fields=[
            "name",
            "customer",
            "customer_name",
            "grand_total",
            "net_total",
            "posting_date",
            "posting_time",
            "creation",
            "owner",
            "modified",
            "currency",
            "total_qty",
            "status",
            "remarks",
            "sales_partner",
            "po_no",
            "project",
        ],
        order_by="creation desc",
    )

    # Batch fetch item counts in a single query instead of per-draft
    if drafts:
        draft_names = [d.name for d in drafts]
        item_counts = frappe.db.sql(
            """SELECT parent, COUNT(*) as cnt
               FROM `tabPOS Invoice Item`
               WHERE parent IN %(names)s
               GROUP BY parent""",
            {"names": draft_names},
            as_dict=True,
        )
        count_map = {r.parent: r.cnt for r in item_counts}
        for draft in drafts:
            draft["item_count"] = count_map.get(draft.name, 0)

    return drafts


@frappe.whitelist()
def load_draft_invoice(invoice_name):
    """Load a full draft invoice with all items, payments, and fields."""
    invoice = frappe.get_doc("POS Invoice", invoice_name)
    validate_pos_document_access(invoice, permissions="read", owner_field="owner")

    if invoice.docstatus != 0:
        frappe.throw(_("Invoice {0} is not a draft").format(invoice_name))

    return format_invoice_response(invoice)


@frappe.whitelist()
def delete_draft_invoice(invoice_name):
    """Delete a draft POS Invoice."""
    invoice = frappe.get_doc("POS Invoice", invoice_name)
    validate_pos_document_access(invoice, permissions="delete", owner_field="owner")

    if invoice.docstatus != 0:
        frappe.throw(_("Only draft invoices can be deleted"))

    frappe.delete_doc("POS Invoice", invoice_name)
    return {"success": True}


@frappe.whitelist()
def update_and_submit_draft(
    invoice_name,
    customer,
    items,
    payments,
    taxes=None,
    additional_discount_percentage=0,
    discount_amount=0,
    apply_discount_on="Grand Total",
    coupon_code=None,
    # Loyalty
    loyalty_points=0,
    loyalty_program=None,
    redeem_loyalty_points=False,
    loyalty_redemption_account=None,
    loyalty_redemption_cost_center=None,
    # Address & contact
    customer_address=None,
    shipping_address_name=None,
    contact_person=None,
    # Currency
    conversion_rate=None,
    price_list_currency=None,
    plc_conversion_rate=None,
    # Commission
    sales_partner=None,
    commission_rate=None,
    # Document details
    project=None,
    cost_center=None,
    remarks=None,
    po_no=None,
    po_date=None,
    set_posting_time=False,
    posting_date=None,
    posting_time=None,
    # Shipping & terms
    shipping_rule=None,
    tc_name=None,
    terms=None,
    # Printing
    letter_head=None,
    select_print_heading=None,
    group_same_items=False,
    language=None,
    # Payment terms
    payment_terms_template=None,
    allocate_advances_automatically=False,
    # Write-off
    write_off_amount=0,
    debit_to=None,
    # Store credit
    store_credit_amount=0,
    # Sales team
    sales_team=None,
):
    """Update a draft invoice with all fields and submit it."""
    if isinstance(items, str):
        items = json.loads(items)
    if isinstance(payments, str):
        payments = json.loads(payments)
    if isinstance(sales_team, str):
        sales_team = json.loads(sales_team)

    # Input validation
    if not items:
        frappe.throw(_("Items cannot be empty"))

    invoice = frappe.get_doc("POS Invoice", invoice_name)
    validate_pos_document_access(
        invoice,
        permissions=("write", "submit"),
        owner_field="owner",
    )

    if invoice.docstatus != 0:
        frappe.throw(_("Invoice {0} is not a draft").format(invoice_name))

    invoice.customer = customer

    # Clear and re-add items
    invoice.items = []
    profile = frappe.get_doc("POS Profile", invoice.pos_profile)
    additional_discount_percentage, discount_amount = get_allowed_invoice_discount(
        profile, additional_discount_percentage, discount_amount
    )

    payments = payments or []
    if (
        not payments
        and not getattr(profile, "allow_partial_payment", 0)
        and not safe_float(store_credit_amount)
        and not (redeem_loyalty_points and safe_float(loyalty_points))
    ):
        frappe.throw(_("Payments cannot be empty"))

    for item_data in items:
        invoice.append("items", build_item_dict(item_data, profile))

    # Clear and re-add payments
    invoice.payments = []
    total_paid = 0
    for payment_data in payments:
        amount = safe_float(payment_data.get("amount", 0))
        if amount <= 0:
            continue
        total_paid += amount
        invoice.append(
            "payments",
            {
                "mode_of_payment": payment_data.get("mode_of_payment"),
                "amount": amount,
            },
        )

    if not invoice.payments:
        default_mop = profile.payments[0].mode_of_payment if profile.payments else "Cash"
        invoice.append("payments", {"mode_of_payment": default_mop, "amount": 0})

    invoice.paid_amount = total_paid

    # Taxes
    if taxes is not None:
        invoice.taxes_and_charges = taxes or None
        if not taxes:
            invoice.set("taxes", [])

    # Discounts
    invoice.additional_discount_percentage = safe_float(additional_discount_percentage)
    invoice.discount_amount = safe_float(discount_amount)
    invoice.apply_discount_on = apply_discount_on or profile.apply_discount_on or "Grand Total"
    invoice.coupon_code = coupon_code or None

    # Loyalty
    invoice.redeem_loyalty_points = 1 if redeem_loyalty_points and loyalty_points else 0
    invoice.loyalty_points = (
        int(safe_float(loyalty_points)) if invoice.redeem_loyalty_points else 0
    )
    invoice.loyalty_program = loyalty_program if invoice.redeem_loyalty_points else None
    invoice.loyalty_redemption_account = (
        loyalty_redemption_account if invoice.redeem_loyalty_points else None
    )
    invoice.loyalty_redemption_cost_center = (
        loyalty_redemption_cost_center if invoice.redeem_loyalty_points else None
    )
    invoice.loyalty_amount = 0
    if invoice.redeem_loyalty_points and invoice.loyalty_program:
        conversion_factor = safe_float(
            frappe.db.get_value(
                "Loyalty Program", invoice.loyalty_program, "conversion_factor"
            )
        )
        invoice.loyalty_amount = safe_float(invoice.loyalty_points * conversion_factor)

    # Replace child/options data from the checkout snapshot.
    invoice.sales_team = []
    invoice.debit_to = debit_to or None

    # Set all optional fields
    set_invoice_optional_fields(
        invoice,
        profile,
        customer_address=customer_address,
        shipping_address_name=shipping_address_name,
        contact_person=contact_person,
        conversion_rate=conversion_rate,
        price_list_currency=price_list_currency,
        plc_conversion_rate=plc_conversion_rate,
        sales_partner=sales_partner,
        commission_rate=commission_rate,
        project=project,
        cost_center=cost_center,
        remarks=remarks,
        po_no=po_no,
        po_date=po_date,
        set_posting_time=set_posting_time,
        posting_date=posting_date,
        posting_time=posting_time,
        shipping_rule=shipping_rule,
        tc_name=tc_name,
        terms=terms,
        letter_head=letter_head,
        select_print_heading=select_print_heading,
        group_same_items=group_same_items,
        language=language,
        payment_terms_template=payment_terms_template,
        allocate_advances_automatically=allocate_advances_automatically,
        write_off_amount=write_off_amount,
        debit_to=debit_to,
        sales_team=sales_team,
    )

    # Apply disable_rounded_total from profile
    if profile.disable_rounded_total:
        invoice.disable_rounded_total = 1

    # Validate stock availability before submission
    if profile.validate_stock_on_save:
        for item_data in items:
            item_code = item_data.get("item_code")
            qty = safe_float(item_data.get("qty", 1))
            item_warehouse = item_data.get("warehouse") or profile.warehouse

            # Product Bundle: validate component stock instead
            bundle_components = get_product_bundle_items(item_code)
            if bundle_components:
                validate_bundle_stock(item_code, qty, item_warehouse)
                continue

            is_stock_item = frappe.db.get_value("Item", item_code, "is_stock_item")
            if not is_stock_item:
                continue
            actual_qty = frappe.db.get_value(
                "Bin", {"item_code": item_code, "warehouse": item_warehouse}, "actual_qty"
            ) or 0
            if qty > actual_qty:
                frappe.throw(
                    _("{0}: Insufficient stock. Available: {1}, Requested: {2}").format(
                        item_code, actual_qty, qty
                    )
                )

    invoice.account_for_change_amount = profile.account_for_change_amount
    invoice.set_missing_values()
    apply_item_discount_policy(invoice, items, profile)
    if taxes is not None and not taxes:
        invoice.taxes_and_charges = None
        invoice.set("taxes", [])
    # Recalculate after replacing the held cart so credit is capped against the
    # submitted snapshot rather than the draft's previous total.
    invoice.calculate_taxes_and_totals()
    set_pos_paid_amount(invoice)

    # A resumed draft is a full snapshot of the checkout form. Explicitly
    # clear optional values so data saved on hold cannot leak into submission.
    invoice.customer_address = customer_address or None
    invoice.shipping_address_name = shipping_address_name or None
    invoice.contact_person = contact_person or None
    if not customer_address:
        invoice.address_display = None
    if not shipping_address_name:
        invoice.shipping_address = None
    if not contact_person:
        invoice.contact_display = None
        invoice.contact_mobile = None
        invoice.contact_email = None
    invoice.sales_partner = sales_partner or None
    invoice.commission_rate = safe_float(commission_rate) if sales_partner else 0
    invoice.project = project or None
    invoice.remarks = remarks or None
    invoice.po_no = po_no or None
    invoice.po_date = po_date or None
    invoice.shipping_rule = shipping_rule or None
    invoice.tc_name = tc_name or None
    invoice.terms = terms or None
    invoice.payment_terms_template = payment_terms_template or None
    invoice.allocate_advances_automatically = (
        1 if allocate_advances_automatically else 0
    )
    invoice.write_off_amount = safe_float(write_off_amount)
    invoice.letter_head = letter_head or None
    invoice.select_print_heading = select_print_heading or None
    invoice.group_same_items = 1 if group_same_items else 0
    invoice.language = language or None

    store_credit = safe_float(store_credit_amount)
    if store_credit < 0:
        frappe.throw(_("Store credit cannot be negative."))

    # Drafts should never retain stale advance allocations. Lock and rebuild
    # them from the submitted request, matching the new-invoice path.
    invoice.advances = []
    invoice.total_advance = 0
    if store_credit > 0:
        from pos_prime.api.customer_profile import get_store_credit_for_update

        credit_data = get_store_credit_for_update(customer, profile.company)
        store_credit = min(
            store_credit,
            safe_float(credit_data.get("total_advance", 0)),
            get_remaining_invoice_amount(invoice),
        )

        remaining = store_credit
        for advance in credit_data.get("advances", []):
            if remaining <= 0:
                break
            allocated = min(remaining, safe_float(advance.amount))
            if allocated <= 0:
                continue
            invoice.append(
                "advances",
                {
                    "reference_type": advance.reference_type,
                    "reference_name": advance.reference_name,
                    "reference_row": advance.get("reference_row") or None,
                    "advance_amount": safe_float(advance.amount),
                    "allocated_amount": allocated,
                    "remarks": advance.get("remarks", ""),
                    "account": advance.get("account"),
                    "ref_exchange_rate": safe_float(
                        advance.get("ref_exchange_rate")
                    )
                    or 1,
                    "difference_posting_date": invoice.posting_date,
                },
            )
            remaining -= allocated

        invoice.total_advance = safe_float(store_credit - remaining)

    set_pos_change_amount(invoice)
    validate_supplemental_settlement(invoice)
    configure_pos_settlement(invoice)

    invoice.save()
    invoice.submit()

    return format_invoice_response(invoice)
