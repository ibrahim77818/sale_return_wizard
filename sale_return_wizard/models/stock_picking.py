# -*- coding: utf-8 -*-
from odoo import _, api, fields, models
from odoo.exceptions import UserError


class StockPicking(models.Model):
    _inherit = "stock.picking"

    sale_return_wizard_allowed = fields.Boolean(
        compute="_compute_sale_return_wizard_allowed",
        help="Technical: show the Return / Credit button on customer deliveries of a confirmed sale order.",
    )
    sale_return_log_ids = fields.One2many("sale.return.log", "origin_picking_id", string="Return Logs")
    sale_return_log_count = fields.Integer(compute="_compute_sale_return_log_count")

    @api.depends("picking_type_code", "location_dest_id.usage", "sale_id.state", "state",
                 "move_ids.origin_returned_move_id")
    def _compute_sale_return_wizard_allowed(self):
        for picking in self:
            # sudo: warehouse users may not read sale orders; only a boolean leaves here
            picking.sale_return_wizard_allowed = not picking.sudo()._sret_picking_block_reason()

    def _compute_sale_return_log_count(self):
        data = self.env["sale.return.log"]._read_group(
            [("origin_picking_id", "in", self.ids)], ["origin_picking_id"], ["origin_picking_id"]
        )
        counts = {d["origin_picking_id"][0]: d["origin_picking_id_count"] for d in data}
        for picking in self:
            picking.sale_return_log_count = counts.get(picking.id, 0)

    def _sret_picking_block_reason(self):
        """Why this transfer cannot use the wizard (False when it can).

        Only the step that reaches the customer carries the sale lines' final
        quantities: pick / pack steps, receipts, internal transfers, dropships
        and returns themselves are refused.
        """
        self.ensure_one()
        if not self.sale_id:
            return _("This transfer is not linked to a sale order.")
        if self.picking_type_code != "outgoing" or self.location_dest_id.usage != "customer":
            return _("Only deliveries to the customer can be returned or credited from a transfer. "
                     "Use the last step of a multi-step delivery, or the sale order itself.")
        if self.location_id.usage != "internal":
            return _("Drop-shipped transfers are not handled by the return wizard.")
        if any(m.origin_returned_move_id for m in self.move_ids):
            return _("This transfer is already a return.")
        if self.state == "cancel":
            return _("This transfer is cancelled.")
        if self.sale_id.state != "sale":
            return _("The sale order %s is not confirmed (or is locked).", self.sale_id.name)
        return False

    def action_open_sale_return_wizard(self):
        """Open the Return / Credit wizard limited to this delivery."""
        self.ensure_one()
        reason = self._sret_picking_block_reason()
        if reason:
            raise UserError(reason)
        sale_lines = self.move_ids.filtered(
            lambda m: m.state != "cancel" and m.sale_line_id.order_id == self.sale_id
        ).sale_line_id
        if not sale_lines:
            raise UserError(_("Nothing in %s is linked to a line of %s.", self.name, self.sale_id.name))
        wizard = self.env["sale.return.wizard"].with_company(self.company_id).create({
            "sale_order_id": self.sale_id.id,
            "picking_id": self.id,
        })
        if not wizard.all_line_ids.filtered(
            lambda l: l.returnable_qty > 0 or l.creditable_qty > 0 or l.pending_qty > 0
        ):
            raise UserError(_(
                "Nothing left to return, credit or cancel on %s: it was fully returned, "
                "credited or cancelled already.", self.name,
            ))
        return wizard._action_reopen()

    def action_view_sale_return_logs(self):
        self.ensure_one()
        action = self.env["ir.actions.act_window"]._for_xml_id("sale_return_wizard.action_sale_return_log")
        action["domain"] = [("origin_picking_id", "=", self.id)]
        action["context"] = {"create": False}
        if self.sale_return_log_count == 1:
            action.update(views=[(False, "form")], res_id=self.sale_return_log_ids.id)
        return action
