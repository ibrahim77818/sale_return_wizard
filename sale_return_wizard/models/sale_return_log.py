# -*- coding: utf-8 -*-
from odoo import _, api, fields, models

RETURN_ACTIONS = [
    ("keep", "Keep"),
    ("return", "Return & Refund"),
    ("credit", "Credit Only"),
    ("cancel", "Cancel Undelivered"),
]


class SaleReturnLog(models.Model):
    """Persistent trace of every wizard execution.

    One record per validation of the wizard: what was asked per line and
    which pickings / credit notes were produced.
    """

    _name = "sale.return.log"
    _description = "Sale Return / Credit Log"
    _inherit = ["mail.thread"]
    _order = "id desc"

    name = fields.Char(required=True, copy=False, readonly=True, default=lambda self: _("New"))
    sale_order_id = fields.Many2one(
        "sale.order", required=True, readonly=True, index=True, ondelete="restrict"
    )
    partner_id = fields.Many2one(related="sale_order_id.partner_id", store=True)
    company_id = fields.Many2one(related="sale_order_id.company_id", store=True)
    currency_id = fields.Many2one(related="sale_order_id.currency_id")
    origin_picking_id = fields.Many2one(
        "stock.picking", string="From Delivery", readonly=True, index="btree_not_null", ondelete="set null",
        help="Set when the wizard was opened from a delivery: every limit was scoped to it.",
    )
    user_id = fields.Many2one("res.users", default=lambda self: self.env.user, readonly=True)
    date = fields.Datetime(default=fields.Datetime.now, readonly=True)
    reason = fields.Char(readonly=True)
    line_ids = fields.One2many("sale.return.log.line", "log_id", readonly=True)
    picking_ids = fields.Many2many(
        "stock.picking", "sale_return_log_picking_rel", "log_id", "picking_id",
        string="Return Pickings", readonly=True,
    )
    credit_note_ids = fields.Many2many(
        "account.move", "sale_return_log_move_rel", "log_id", "move_id",
        string="Credit Notes", readonly=True,
    )
    picking_count = fields.Integer(compute="_compute_counts")
    credit_note_count = fields.Integer(compute="_compute_counts")
    amount_credited = fields.Monetary(compute="_compute_counts", currency_field="currency_id")
    state = fields.Selection(
        [("waiting", "Waiting Goods"), ("done", "Done")],
        compute="_compute_state", store=True,
    )

    @api.depends("picking_ids", "credit_note_ids.amount_total", "credit_note_ids.state")
    def _compute_counts(self):
        for log in self:
            log.picking_count = len(log.picking_ids)
            log.credit_note_count = len(log.credit_note_ids)
            # credit notes are issued in the SO currency
            log.amount_credited = sum(
                log.credit_note_ids.filtered(lambda m: m.state != "cancel").mapped("amount_total")
            )

    @api.depends("picking_ids.state")
    def _compute_state(self):
        for log in self:
            pending = log.picking_ids.filtered(lambda p: p.state not in ("done", "cancel"))
            log.state = "waiting" if pending else "done"

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get("name", _("New")) == _("New"):
                vals["name"] = self.env["ir.sequence"].next_by_code("sale.return.log") or _("New")
        return super().create(vals_list)

    def action_view_pickings(self):
        self.ensure_one()
        action = self.env["ir.actions.act_window"]._for_xml_id("stock.action_picking_tree_all")
        action["domain"] = [("id", "in", self.picking_ids.ids)]
        action["context"] = {"create": False}
        if len(self.picking_ids) == 1:
            action.update(views=[(False, "form")], res_id=self.picking_ids.id)
        return action

    def action_view_credit_notes(self):
        self.ensure_one()
        action = self.env["ir.actions.act_window"]._for_xml_id("account.action_move_out_refund_type")
        action["domain"] = [("id", "in", self.credit_note_ids.ids)]
        action["context"] = {"create": False, "default_move_type": "out_refund"}
        if len(self.credit_note_ids) == 1:
            action.update(views=[(False, "form")], res_id=self.credit_note_ids.id)
        return action


class SaleReturnLogLine(models.Model):
    _name = "sale.return.log.line"
    _description = "Sale Return / Credit Log Line"

    log_id = fields.Many2one("sale.return.log", required=True, ondelete="cascade", index=True)
    sale_line_id = fields.Many2one("sale.order.line", required=True, ondelete="restrict", index=True)
    product_id = fields.Many2one(related="sale_line_id.product_id", store=True)
    product_uom_id = fields.Many2one(related="sale_line_id.product_uom")
    action = fields.Selection(RETURN_ACTIONS, required=True)
    quantity = fields.Float(digits="Product Unit of Measure", help="Quantity requested in the wizard.")
    credit_qty = fields.Float("Qty Credited", digits="Product Unit of Measure", help="Quantity credited on credit notes.")
    so_qty_reduced = fields.Float(
        "Ordered Qty Removed", digits="Product Unit of Measure",
        help="Ordered quantity removed from the sale order line by this operation.",
    )
    credit_move_ids = fields.Many2many(
        "account.move", "sale_return_log_line_move_rel", "line_id", "move_id", string="Credit Notes"
    )
    return_move_ids = fields.One2many("stock.move", "sale_return_log_line_id", string="Return Moves")
    qty_received = fields.Float(
        "Qty Received Back", compute="_compute_qty_received", digits="Product Unit of Measure",
        help="Returned quantity already received back in stock.",
    )

    @api.depends("return_move_ids.state", "return_move_ids.quantity")
    def _compute_qty_received(self):
        for line in self:
            line.qty_received = sum(
                m.product_uom._compute_quantity(m.quantity, line.product_uom_id)
                for m in line.return_move_ids.filtered(lambda m: m.state == "done")
            )
