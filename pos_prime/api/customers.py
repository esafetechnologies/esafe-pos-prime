# Copyright (c) 2026, Ravindu Gajanayaka
# Licensed under GPLv3. See license.txt

import re
import frappe
from frappe import _
from pos_prime.api._utils import validate_doctype_permissions, validate_pos_access


def _is_leaf_tree_node(doctype, name):
    if not name:
        return False
    is_group = frappe.db.get_value(doctype, name, "is_group")
    return is_group in (0, "0", False)


def _expand_leaf_tree_nodes(doctype, names):
    """Expand configured tree groups into their selectable leaf descendants."""
    leaves = []
    for name in names or []:
        node = frappe.db.get_value(
            doctype, name, ["is_group", "lft", "rgt"], as_dict=True
        )
        if not node:
            continue
        if node.is_group in (0, "0", False):
            leaves.append(name)
            continue
        leaves.extend(
            frappe.get_all(
                doctype,
                filters={
                    "is_group": 0,
                    "lft": [">", node.lft],
                    "rgt": ["<", node.rgt],
                },
                pluck="name",
                order_by="lft asc",
            )
        )
    return list(dict.fromkeys(leaves))


def _first_leaf_tree_node(doctype, preferred=None, candidates=None):
    """Return a valid leaf, preferring configured values when possible."""
    for name in [preferred, *(candidates or [])]:
        if _is_leaf_tree_node(doctype, name):
            return name

    leaves = frappe.get_all(
        doctype,
        filters={"is_group": 0},
        pluck="name",
        order_by="name asc",
        limit=1,
    )
    return leaves[0] if leaves else ""


@frappe.whitelist()
def search_customers(search_term="", pos_profile=""):
    """Search customers by name, phone, or email.

    If pos_profile has customer_groups configured, only returns customers
    belonging to those groups.

    Phone numbers are normalized so local format (07xxxxxxxx) matches
    international format (+947xxxxxxxx) and vice versa.
    """
    validate_pos_access(pos_profile or None)
    validate_doctype_permissions("Customer", "read")
    if not search_term or len(search_term) < 2:
        return []

    search = f"%{search_term}%"

    # Normalize phone: extract last 9 digits to match regardless of country code
    digits_only = re.sub(r"\D", "", search_term)
    or_filters = [
        ["Customer", "customer_name", "like", search],
        ["Customer", "name", "like", search],
        ["Customer", "mobile_no", "like", search],
        ["Customer", "email_id", "like", search],
    ]

    if len(digits_only) >= 7:
        core_digits = digits_only[-9:]
        or_filters.append(
            ["Customer", "mobile_no", "like", f"%{core_digits}"]
        )

    # Check if POS Profile restricts customer groups
    filters = {"disabled": 0}

    if pos_profile:
        profile_groups = frappe.get_all(
            "POS Customer Group",
            filters={"parent": pos_profile, "parenttype": "POS Profile"},
            pluck="customer_group",
        )
        profile_groups = _expand_leaf_tree_nodes("Customer Group", profile_groups)
        if profile_groups:
            filters["customer_group"] = ["in", profile_groups]

    customers = frappe.get_list(
        "Customer",
        filters=filters,
        or_filters=or_filters,
        fields=["name", "customer_name", "mobile_no", "email_id"],
        order_by="customer_name asc",
        limit_page_length=20,
    )

    return customers


@frappe.whitelist()
def get_recent_customers(pos_profile="", limit=20):
    """Return customers who had recent POS invoices, ordered by latest transaction.

    If pos_profile is provided, only returns customers with invoices from that
    profile's company, and respects customer_group filtering.
    """
    validate_pos_access(pos_profile or None)
    validate_doctype_permissions("Customer", "read")
    limit = max(1, min(int(limit), 50))
    invoice_filters = {"docstatus": 1}
    profile_groups = []

    if pos_profile:
        company = frappe.db.get_value("POS Profile", pos_profile, "company")
        if company:
            invoice_filters["company"] = company

        profile_groups = frappe.get_all(
            "POS Customer Group",
            filters={"parent": pos_profile, "parenttype": "POS Profile"},
            pluck="customer_group",
        )
        profile_groups = _expand_leaf_tree_nodes("Customer Group", profile_groups)
    recent_invoices = frappe.get_list(
        "POS Invoice",
        filters=invoice_filters,
        fields=["customer", "posting_date"],
        order_by="posting_date desc, creation desc",
        limit_page_length=max(limit * 20, 200),
    )

    latest_by_customer = {}
    ordered_customers = []
    for invoice in recent_invoices:
        if not invoice.customer or invoice.customer in latest_by_customer:
            continue
        latest_by_customer[invoice.customer] = invoice.posting_date
        ordered_customers.append(invoice.customer)

    if not ordered_customers:
        return []

    customer_filters = {"name": ["in", ordered_customers], "disabled": 0}
    if profile_groups:
        customer_filters["customer_group"] = ["in", profile_groups]
    visible_customers = frappe.get_list(
        "Customer",
        filters=customer_filters,
        fields=["name", "customer_name", "mobile_no", "email_id"],
        limit_page_length=0,
    )
    customer_map = {customer.name: customer for customer in visible_customers}

    result = []
    for customer_name in ordered_customers:
        customer = customer_map.get(customer_name)
        if not customer:
            continue
        customer["last_invoice_date"] = latest_by_customer[customer_name]
        result.append(customer)
        if len(result) >= limit:
            break
    return result


@frappe.whitelist()
def get_customer(customer_name):
    """Get customer details for POS.

    Returns essential customer fields after enforcing document read access.
    """
    validate_pos_access()
    if not frappe.db.exists("Customer", customer_name):
        frappe.throw(_("Customer {0} does not exist").format(customer_name))

    customer = frappe.get_doc("Customer", customer_name)
    validate_doctype_permissions("Customer", "read", doc=customer)
    return {
        field: customer.get(field)
        for field in (
            "name",
            "customer_name",
            "email_id",
            "mobile_no",
            "loyalty_program",
            "territory",
            "customer_group",
            "tax_id",
        )
    }


@frappe.whitelist()
def get_quick_entry_options(pos_profile=""):
    """Return dropdown options needed for the new-customer quick entry dialog.

    Returns customer_groups, customer_type options, and quick_entry fields
    from the Customer doctype meta so the frontend can render a matching form.
    """
    validate_pos_access(pos_profile or None)

    # Customer groups — prefer POS Profile groups, fall back to leaf groups
    customer_groups = []
    if pos_profile:
        customer_groups = frappe.get_all(
            "POS Customer Group",
            filters={"parent": pos_profile, "parenttype": "POS Profile"},
            pluck="customer_group",
        )
        customer_groups = _expand_leaf_tree_nodes("Customer Group", customer_groups)
    if not customer_groups:
        customer_groups = frappe.get_list(
            "Customer Group",
            filters={"is_group": 0},
            pluck="name",
            order_by="name asc",
            limit_page_length=50,
        )

    # Customer type options from the Select field
    meta = frappe.get_meta("Customer")
    ct_field = meta.get_field("customer_type")
    customer_types = (ct_field.options or "").split("\n") if ct_field else ["Individual"]

    # Quick entry fields (reqd or allow_in_quick_entry)
    quick_fields = []
    for f in meta.fields:
        if (f.reqd or f.allow_in_quick_entry) and not f.read_only and f.fieldtype not in (
            "Tab Break", "Section Break", "Column Break", "Table",
        ):
            quick_fields.append({
                "fieldname": f.fieldname,
                "label": f.label,
                "fieldtype": f.fieldtype,
                "options": f.options,
                "reqd": f.reqd,
                "default": f.default,
            })

    # Country options for address
    countries = frappe.get_list(
        "Country", pluck="name", order_by="name asc", limit_page_length=0,
    )

    # Default country from system settings
    default_country = frappe.db.get_default("country") or ""

    # Defaults
    default_group = _first_leaf_tree_node(
        "Customer Group",
        preferred=frappe.db.get_single_value("Selling Settings", "customer_group"),
        candidates=customer_groups,
    )
    if customer_groups and default_group not in customer_groups:
        default_group = customer_groups[0]

    default_territory = _first_leaf_tree_node(
        "Territory",
        preferred=frappe.db.get_single_value("Selling Settings", "territory"),
    )

    return {
        "customer_groups": customer_groups,
        "customer_types": [t for t in customer_types if t.strip()],
        "quick_fields": quick_fields,
        "default_customer_group": default_group,
        "default_customer_type": "Individual",
        "default_territory": default_territory,
        "countries": countries,
        "default_country": default_country,
    }


@frappe.whitelist()
def quick_create_customer(customer_name, mobile_no=None, email_id=None,
                          customer_type=None, customer_group=None,
                          territory=None, pos_profile="",
                          first_name=None, last_name=None,
                          address_line1=None, address_line2=None,
                          city=None, state=None, pincode=None, country=None,
                          **extra_fields):
    """Create a customer record from the POS quick entry dialog.

    Accepts all quick_entry fields from the Customer doctype plus
    contact (first_name, last_name) and address fields. ERPNext's
    Customer on_update hook auto-creates Contact and Address records.
    """
    validate_pos_access(pos_profile or None)
    validate_doctype_permissions("Customer", "create")
    if not customer_name or not customer_name.strip():
        frappe.throw(_("Customer name is required"))

    customer_name = customer_name.strip()

    if len(customer_name) > 140:
        frappe.throw(_("Customer name must not exceed 140 characters"))

    if email_id:
        email_pattern = r"^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$"
        if not re.match(email_pattern, email_id):
            frappe.throw(_("Invalid email address format"))

    # Resolve defaults
    profile_groups = []
    if not customer_group and pos_profile:
        profile_groups = frappe.get_all(
            "POS Customer Group",
            filters={"parent": pos_profile, "parenttype": "POS Profile"},
            pluck="customer_group",
            limit=1,
        )
        profile_groups = _expand_leaf_tree_nodes("Customer Group", profile_groups)
        if profile_groups:
            customer_group = _first_leaf_tree_node(
                "Customer Group", candidates=profile_groups
            )

    if customer_group and not _is_leaf_tree_node("Customer Group", customer_group):
        frappe.throw(_("Please select a non-group Customer Group."))
    if territory and not _is_leaf_tree_node("Territory", territory):
        frappe.throw(_("Please select a non-group Territory."))

    customer_group = customer_group or _first_leaf_tree_node(
        "Customer Group",
        preferred=frappe.db.get_single_value("Selling Settings", "customer_group"),
        candidates=profile_groups,
    )
    territory = territory or _first_leaf_tree_node(
        "Territory",
        preferred=frappe.db.get_single_value("Selling Settings", "territory"),
    )
    if not customer_group or not territory:
        frappe.throw(_("A non-group Customer Group and Territory are required."))

    doc_data = {
        "doctype": "Customer",
        "customer_name": customer_name,
        "customer_type": customer_type or "Individual",
        "customer_group": customer_group,
        "territory": territory,
    }

    if mobile_no:
        doc_data["mobile_no"] = mobile_no
    if email_id:
        doc_data["email_id"] = email_id

    # Contact name fields — Customer on_update uses these to create Contact
    if first_name:
        doc_data["first_name"] = first_name
    if last_name:
        doc_data["last_name"] = last_name

    # Address fields — Customer on_update uses these to create Address
    if address_line1:
        doc_data["address_line1"] = address_line1
    if address_line2:
        doc_data["address_line2"] = address_line2
    if city:
        doc_data["city"] = city
    if state:
        doc_data["state"] = state
    if pincode:
        doc_data["pincode"] = pincode
    if country:
        doc_data["country"] = country

    # Apply any extra quick_entry fields (e.g. custom_nic_number)
    meta = frappe.get_meta("Customer")
    allowed_quick = {
        f.fieldname for f in meta.fields
        if (f.reqd or f.allow_in_quick_entry)
        and not f.read_only
        and f.fieldtype not in ("Tab Break", "Section Break", "Column Break", "Table")
    }
    for key, value in extra_fields.items():
        if key in allowed_quick and value:
            doc_data[key] = value

    customer = frappe.get_doc(doc_data)
    customer.insert()

    return customer.name


@frappe.whitelist()
def update_customer_field(customer, fieldname, value=""):
    """Update a customer contact field from POS.

    Mirrors ERPNext's built-in POS approach: updates both the Contact doctype
    (primary contact with email_ids/phone_nos child tables) and the Customer
    doctype's denormalized fields.
    """
    validate_pos_access()

    allowed_fields = {"email_id", "mobile_no", "loyalty_program"}
    if fieldname not in allowed_fields:
        frappe.throw(_("Field {0} cannot be updated from POS").format(fieldname))

    if not frappe.db.exists("Customer", customer):
        frappe.throw(_("Customer {0} does not exist").format(customer))

    customer_doc = frappe.get_doc("Customer", customer)
    validate_doctype_permissions("Customer", "write", doc=customer_doc)

    if fieldname == "email_id" and value:
        email_pattern = r"^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$"
        if not re.match(email_pattern, value):
            frappe.throw(_("Invalid email address format"))

    if fieldname == "loyalty_program":
        customer_doc.loyalty_program = value or None
        customer_doc.save()
        return customer

    # Get or create primary contact (same logic as ERPNext's set_customer_info)
    contact = frappe.get_cached_value("Customer", customer, "customer_primary_contact")

    if not contact:
        contacts = frappe.db.sql(
            """
            SELECT parent FROM `tabDynamic Link`
            WHERE parenttype = 'Contact'
              AND parentfield = 'links'
              AND link_doctype = 'Customer'
              AND link_name = %s
            """,
            (customer,),
            as_dict=True,
        )
        contact = contacts[0].get("parent") if contacts else None

    if not contact:
        validate_doctype_permissions("Contact", "create")
        new_contact = frappe.new_doc("Contact")
        new_contact.is_primary_contact = 1
        new_contact.first_name = frappe.db.get_value("Customer", customer, "customer_name") or customer
        new_contact.set("links", [{"link_doctype": "Customer", "link_name": customer}])
        new_contact.save()
        contact = new_contact.name
        frappe.db.set_value("Customer", customer, "customer_primary_contact", contact)

    # Update the Contact doctype and Customer denormalized field
    contact_doc = frappe.get_doc("Contact", contact)
    validate_doctype_permissions("Contact", "write", doc=contact_doc)

    if fieldname == "email_id":
        primary_rows = [row for row in contact_doc.email_ids if row.is_primary]
        for row in contact_doc.email_ids:
            row.is_primary = 0

        if value:
            matching = next(
                (row for row in contact_doc.email_ids if row.email_id == value),
                None,
            )
            if matching:
                matching.is_primary = 1
            else:
                contact_doc.append("email_ids", {"email_id": value, "is_primary": 1})
        else:
            for row in primary_rows:
                contact_doc.remove(row)
        frappe.db.set_value("Customer", customer, "email_id", value)
    elif fieldname == "mobile_no":
        primary_rows = [row for row in contact_doc.phone_nos if row.is_primary_mobile_no]
        for row in contact_doc.phone_nos:
            row.is_primary_mobile_no = 0

        if value:
            matching = next(
                (row for row in contact_doc.phone_nos if row.phone == value),
                None,
            )
            if matching:
                matching.is_primary_mobile_no = 1
            else:
                contact_doc.append(
                    "phone_nos", {"phone": value, "is_primary_mobile_no": 1}
                )
        else:
            for row in primary_rows:
                contact_doc.remove(row)
        frappe.db.set_value("Customer", customer, "mobile_no", value)

    contact_doc.save()

    return customer
