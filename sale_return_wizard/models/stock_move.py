# -*- coding: utf-8 -*-
from odoo import fields, models
from odoo.tools import float_compare, float_is_zero


class StockMove(models.Model):
    _inherit = "stock.move"

    sale_return_log_line_id = fields.Many2one(
        "sale.return.log.line",
        string="Sale Return Log Line",
        index="btree_not_null",
        readonly=True,
        help="Return-wizard log line that created this return move. When the "
             "move is done, the ordered quantity of the sale order line is "
             "reduced accordingly (refund return, no replacement).",
    )

    def _sret_processed_qty(self):
        """Quantity of this move that somebody already processed (move UoM).

        v17: ``quantity_done`` is gone. ``quantity`` now holds the reserved
        quantity *and* the done quantity, and only the ``picked`` flag tells
        them apart. An open move that is not picked has nothing processed yet.
        """
        self.ensure_one()
        return self.quantity if self.picked else 0.0

    def _action_done(self, cancel_backorder=False):
        moves_done = super()._action_done(cancel_backorder=cancel_backorder)
        moves_done._sale_return_reduce_ordered_qty()
        return moves_done

    def _sale_return_reduce_ordered_qty(self):
        """Reduce the SO line ordered quantity once returned goods are received.

        The reduction is deferred until the return is *done* because Odoo
        forbids decreasing an SO line below its delivered quantity, and the
        delivered quantity only drops when the return move is validated.
        Doing it here also covers returns validated later by the warehouse,
        and partial receptions (backorders keep the log-line link on copy).
        """
        moves = self.filtered(
            lambda m: m.state == "done"
            and m.sale_return_log_line_id
            and m.sale_line_id
            and m.to_refund
        )
        # sudo: the receipt is usually validated by a warehouse user who has
        # no write access on sale order lines; the change is fully determined
        # by the return the sales user already approved in the wizard.
        for move in moves.sudo():
            sol = move.sale_line_id
            log_line = move.sale_return_log_line_id
            rounding = sol.product_uom_id.rounding
            qty = move.product_uom._compute_quantity(move.quantity, sol.product_uom_id)
            if float_is_zero(qty, precision_rounding=rounding):
                continue
            new_qty = sol.product_uom_qty - qty
            # Never go below what is still delivered (Odoo raises otherwise).
            if float_compare(new_qty, sol.qty_delivered, precision_rounding=rounding) < 0:
                new_qty = sol.qty_delivered
            if float_compare(new_qty, sol.product_uom_qty, precision_rounding=rounding) < 0:
                sol.with_context(sale_return_wizard=True).write({"product_uom_qty": new_qty})
            log_line.so_qty_reduced += qty
