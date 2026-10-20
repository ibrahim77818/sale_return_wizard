# -*- coding: utf-8 -*-
from odoo import _, fields, models
from odoo.exceptions import UserError


class SaleOrder(models.Model):
    _inherit = "sale.order"

    sale_return_log_ids = fields.One2many("sale.return.log", "sale_order_id", string="Return Logs")
    sale_return_log_count = fields.Integer(compute="_compute_sale_return_log_count")

    def _compute_sale_return_log_count(self):
        data = self.env["sale.return.log"]._read_group(
            [("sale_order_id", "in", self.ids)], ["sale_order_id"], ["sale_order_id"]
        )
        counts = {d["sale_order_id"][0]: d["sale_order_id_count"] for d in data}
        for order in self:
            order.sale_return_log_count = counts.get(order.id, 0)

    def action_open_sale_return_wizard(self):
        """Create the wizard server-side (with all its lines) and open it.

        Creating the record up-front, instead of relying on default_get, gives
        the popup a real res_id: the filter / bulk buttons can then save the
        user's edits, change line visibility and re-open the same wizard.
        """
        self.ensure_one()
        if self.state != "sale":
            raise UserError(_("Returns and credit notes can only be processed on confirmed sale orders."))
        wizard = self.env["sale.return.wizard"].create({"sale_order_id": self.id})
        return wizard._action_reopen()

    def action_view_sale_return_logs(self):
        self.ensure_one()
        action = self.env["ir.actions.act_window"]._for_xml_id("sale_return_wizard.action_sale_return_log")
        action["domain"] = [("sale_order_id", "=", self.id)]
        action["context"] = {"create": False}
        if self.sale_return_log_count == 1:
            action.update(views=[(False, "form")], res_id=self.sale_return_log_ids.id)
        return action


class SaleOrderLine(models.Model):
    _inherit = "sale.order.line"

    sale_return_log_line_ids = fields.One2many("sale.return.log.line", "sale_line_id", readonly=True)

    # ------------------------------------------------------------------
    # Quantity helpers (all results expressed in the SO line UoM)
    # ------------------------------------------------------------------
    def _sret_is_stockable(self):
        self.ensure_one()
        return self.product_id.type in ("product", "consu")

    def _sret_returnable_moves(self, picking=None):
        """Done deliveries (internal -> customer) that can still be returned.

        Dropship (supplier -> customer) and return-of-return moves are excluded.
        Moves of another product (kits) are excluded as well, so kit lines are
        never returned through the wizard. With ``picking``, only that
        transfer's moves count.
        """
        self.ensure_one()
        return self.move_ids.filtered(
            lambda m: m.state == "done"
            and m.product_id == self.product_id
            and m.location_dest_id.usage == "customer"
            and m.location_id.usage == "internal"
            and not m.origin_returned_move_id
            and (not picking or m.picking_id == picking)
        )

    def _sret_pending_moves(self, picking=None):
        """Open deliveries to the customer for this line (optionally in one transfer)."""
        self.ensure_one()
        return self.move_ids.filtered(
            lambda m: m.state not in ("done", "cancel")
            and m.product_id == self.product_id
            and m.location_dest_id.usage == "customer"
            and not m.origin_returned_move_id
            and (not picking or m.picking_id == picking)
        )

    @staticmethod
    def _sret_move_cancellable_qty(move):
        """Open demand of a move that nobody has started to process (move UoM)."""
        return max(move.product_uom_qty - move.quantity_done, 0.0)

    @staticmethod
    def _sret_move_remaining_qty(move):
        """Quantity of a done delivery move not yet covered by a return (move UoM)."""
        returned = 0.0
        for ret in move.returned_move_ids.filtered(lambda r: r.state != "cancel"):
            qty = ret.quantity_done if ret.state == "done" else ret.product_uom_qty
            returned += ret.product_uom._compute_quantity(qty, move.product_uom)
        return max(move.quantity_done - returned, 0.0)

    def _sret_posted_invoiced_qty(self):
        self.ensure_one()
        qty = 0.0
        for inv_line in self.invoice_lines.filtered(lambda l: l.move_id.state == "posted"):
            q = inv_line.product_uom_id._compute_quantity(inv_line.quantity, self.product_uom)
            if inv_line.move_id.move_type == "out_invoice":
                qty += q
            elif inv_line.move_id.move_type == "out_refund":
                qty -= q
        return qty

    def _sret_allowance_qty(self):
        """Quantity already credited by 'Credit Only' operations.

        Those credit-note lines are intentionally *not* linked to the SO line
        (otherwise Odoo would consider the quantity invoiceable again), so they
        must be tracked through the return log.
        """
        self.ensure_one()
        return sum(
            self.sale_return_log_line_ids.filtered(
                lambda l: l.action == "credit"
                and any(m.state != "cancel" for m in l.credit_move_ids)
            ).mapped("credit_qty")
        )

    def _sret_get_snapshot(self, picking=None):
        """Current quantities used to seed and to re-validate wizard lines.

        With ``picking`` (wizard opened from a delivery), every limit is capped
        to what THAT transfer delivered or still has open, so a return can
        never take goods from another delivery of the same order.
        """
        self.ensure_one()
        ordered = self.product_uom_qty
        delivered = self.qty_delivered
        invoiced = self.qty_invoiced
        allowance = self._sret_allowance_qty()
        creditable = max(invoiced - allowance, 0.0)
        posted_creditable = min(max(self._sret_posted_invoiced_qty() - allowance, 0.0), creditable)
        returnable = 0.0
        if self._sret_is_stockable():
            from_moves = sum(
                m.product_uom._compute_quantity(self._sret_move_remaining_qty(m), self.product_uom)
                for m in self._sret_returnable_moves(picking)
            )
            returnable = max(min(delivered, from_moves), 0.0)
        pending = max(ordered - delivered, 0.0)
        if picking:
            done_in_picking = sum(
                m.product_uom._compute_quantity(m.quantity_done, self.product_uom)
                for m in self._sret_returnable_moves(picking)
            )
            open_in_picking = sum(
                m.product_uom._compute_quantity(self._sret_move_cancellable_qty(m), self.product_uom)
                for m in self._sret_pending_moves(picking)
            )
            # an allowance from a delivery covers at most what that delivery shipped
            creditable = min(creditable, done_in_picking)
            posted_creditable = min(posted_creditable, creditable)
            pending = min(pending, open_in_picking)
        return {
            "ordered_qty": ordered,
            "delivered_qty": delivered,
            "invoiced_qty": invoiced,
            "creditable_qty": creditable,
            "posted_creditable_qty": posted_creditable,
            "returnable_qty": returnable,
            "pending_qty": pending,
        }
