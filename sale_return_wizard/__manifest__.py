# -*- coding: utf-8 -*-
{
    "name": "Sale Return & Credit Wizard",
    "version": "19.0.1.3.0",
    "category": "Sales/Sales",
    "summary": "Line-by-line returns, cancellations and customer credit notes "
               "for high-volume Sale Orders, with full traceability",
    "description": """
Sale Return & Credit Wizard
===========================
* "Return / Credit" button on confirmed Sale Orders and on their customer
  deliveries (scoped to that delivery's quantities).
* Popup listing every order line with server-side search/filter
  (product name, SKU/barcode, ordered / delivered / invoiced / returnable
  quantity thresholds) and bulk actions on the filtered lines.
* Per-line disposition: Keep, Return & Refund, Credit Only, Cancel Undelivered.
* Strict quantity validation (onchange + @api.constrains + re-check at execution).
* Automatic document routing:
    - delivered & invoiced  -> return picking + posted customer credit note
    - delivered, not invoiced -> return picking, SO quantities adjusted, no credit
    - draft-invoiced quantities -> draft credit note for review
    - undelivered quantities -> pending deliveries reduced / cancelled,
      over-invoiced amounts credited
* Return log (SRET/xxxx) linking every picking and credit note produced.
""",
    "author": "Ibrahim Yahia",
    "website": "https://www.linkedin.com/in/ibrahim-yahia",
    "support": "ibrahim.yahiah@gmail.com",
    "license": "LGPL-3",
    "depends": ["sale_stock", "account"],
    "data": [
        "security/sale_return_security.xml",
        "security/ir.model.access.csv",
        "data/ir_sequence_data.xml",
        "wizard/sale_return_wizard_views.xml",
        "views/sale_return_log_views.xml",
        "views/sale_order_views.xml",
        "views/stock_picking_views.xml",
    ],
    "assets": {
        "web.assets_backend": [
            "sale_return_wizard/static/src/js/sale_return_wizard_form.js",
        ],
    },
    "images": ["static/description/banner.png"],
    "installable": True,
    "application": False,
}
