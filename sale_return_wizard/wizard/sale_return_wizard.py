# -*- coding: utf-8 -*-
from collections import defaultdict

from markupsafe import Markup

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError
from odoo.tools import float_compare, float_is_zero

from ..models.sale_return_log import RETURN_ACTIONS

QTY_FIELDS = [
    ("ordered_qty", "Ordered"),
    ("delivered_qty", "Delivered"),
    ("invoiced_qty", "Invoiced"),
    ("returnable_qty", "Returnable"),
    ("pending_qty", "Undelivered"),
]


FILTER_FIELDS = {
    "filter_text", "filter_qty_field", "filter_qty_min", "filter_qty_max",
    "filter_only_actionable", "filter_only_selected",
}


class SaleReturnWizard(models.TransientModel):
    _name = "sale.return.wizard"
    _description = "Sale Order Return / Credit Wizard"

    sale_order_id = fields.Many2one("sale.order", required=True, readonly=True, ondelete="cascade")
    picking_id = fields.Many2one(
        "stock.picking", string="Delivery", readonly=True, ondelete="cascade",
        help="Set when opened from a delivery: only its lines are listed, and returns, "
             "allowances and cancellations are limited to what this delivery shipped or still has open.",
    )
    info_message = fields.Text(readonly=True)
    company_id = fields.Many2one(related="sale_order_id.company_id")
    currency_id = fields.Many2one(related="sale_order_id.currency_id")
    partner_id = fields.Many2one(related="sale_order_id.partner_id")

    # Options -----------------------------------------------------------
    reason = fields.Char(help="Used as reference on the credit notes and in the return log.")
    auto_validate_return = fields.Boolean(
        "Receive Returned Goods Now", default=True,
        help="Validate return pickings immediately. Lot/serial-tracked products, and "
             "users without Inventory rights, always leave the return for the warehouse.",
    )
    auto_post_credit_note = fields.Boolean(
        "Post Credit Notes", default=True,
        help="Post the credit note covering quantities invoiced on posted invoices. "
             "Quantities only present on draft invoices always go to a draft credit note.",
    )
    return_location_id = fields.Many2one(
        "stock.location", string="Return Location",
        domain="[('usage', '=', 'internal'), ('company_id', 'in', [company_id, False])]",
        help="Leave empty to return goods where they were shipped from.",
    )

    # Lines ---------------------------------------------------------------
    # Same inverse, two views of it: `line_ids` is what the popup shows
    # (filtered by the stored `is_visible` flag, the field domain is applied
    # when the client reads the one2many), `all_line_ids` is what we process.
    line_ids = fields.One2many(
        "sale.return.wizard.line", "wizard_id", string="Lines", domain=[("is_visible", "=", True)]
    )
    all_line_ids = fields.One2many("sale.return.wizard.line", "wizard_id", string="All Lines")

    # Filters ---------------------------------------------------------------
    filter_text = fields.Char(
        "Search", help="Matches product name, internal reference (SKU), barcode or line description. "
                       "Separate several terms with commas to match any of them."
    )
    filter_qty_field = fields.Selection(
        QTY_FIELDS, "Quantity",
        help="Pick a quantity to show lines where it is above 0, or narrow it with min / max.",
    )
    filter_qty_min = fields.Float("Min", digits="Product Unit of Measure")
    filter_qty_max = fields.Float("Max", digits="Product Unit of Measure", help="0 = no upper limit")
    filter_only_actionable = fields.Boolean(
        "Only Actionable", help="Hide lines with nothing to return, credit or cancel."
    )
    filter_only_selected = fields.Boolean("Only Selected", help="Only lines whose action is not 'Keep'.")

    # Summary ----------------------------------------------------------------
    total_line_count = fields.Integer(compute="_compute_summary")
    visible_line_count = fields.Integer(compute="_compute_summary")
    selected_line_count = fields.Integer(compute="_compute_summary")
    estimated_credit_amount = fields.Monetary(
        compute="_compute_summary", currency_field="currency_id",
        help="Untaxed estimate of all credit notes this wizard will create.",
    )

    @api.depends(
        "line_ids.action", "line_ids.quantity", "line_ids.credit_amount",
        "all_line_ids.is_visible", "all_line_ids.action", "all_line_ids.quantity", "all_line_ids.credit_amount",
    )
    def _compute_summary(self):
        for wiz in self:
            # While the popup is being edited, `line_ids` carries the user's unsaved
            # values for the shown lines; hidden lines only exist in `all_line_ids`.
            shown = wiz.line_ids
            shown_ids = {l._origin.id or l.id for l in shown}
            hidden = wiz.all_line_ids.filtered(lambda l: (l._origin.id or l.id) not in shown_ids)
            lines = shown | hidden
            wiz.total_line_count = len(wiz.all_line_ids)
            wiz.visible_line_count = len(shown)
            wiz.selected_line_count = len(lines.filtered(lambda l: l.action != "keep" and l.quantity > 0))
            wiz.estimated_credit_amount = sum(lines.mapped("credit_amount"))

    # ------------------------------------------------------------------
    # Creation
    # ------------------------------------------------------------------
    @api.model_create_multi
    def create(self, vals_list):
        wizards = super().create(vals_list)
        for wiz in wizards:
            if not wiz.all_line_ids:
                wiz._populate_lines()
        return wizards

    def _populate_lines(self):
        self.ensure_one()
        order_lines = self.sale_order_id.order_line.filtered(
            lambda l: not l.display_type and not l.is_downpayment and l.product_id
        )
        notes = []
        if self.picking_id:
            moves = self.picking_id.move_ids.filtered(lambda m: m.state != "cancel")
            order_lines &= moves.sale_line_id
            foreign = moves.filtered(lambda m: m.sale_line_id.order_id != self.sale_order_id)
            if foreign:
                notes.append(_(
                    "Not listed (no line on %(so)s): %(products)s.",
                    so=self.sale_order_id.name,
                    products=", ".join(sorted(set(foreign.product_id.mapped("display_name")))),
                ))
            kits = order_lines.filtered(lambda l: l.product_id not in moves.product_id)
            if kits:
                order_lines -= kits
                notes.append(_(
                    "Kit products are not handled from a delivery, use the sale order: %s.",
                    ", ".join(kits.product_id.mapped("display_name")),
                ))
            notes.append(_(
                "Opened from %s: returns, allowances and cancellations are limited to this delivery.",
                self.picking_id.name,
            ))
        self.info_message = "\n".join(notes) or False
        vals_list = []
        for sol in order_lines:
            vals = sol._sret_get_snapshot(self.picking_id)
            vals.update(wizard_id=self.id, sale_line_id=sol.id, name=sol.name, action="keep", quantity=0.0)
            vals_list.append(vals)
        self.env["sale.return.wizard.line"].create(vals_list)

    def _action_reopen(self):
        self.ensure_one()
        return {
            "name": _("Return / Credit: %s", " / ".join(filter(None, [self.sale_order_id.name, self.picking_id.name]))),
            "type": "ir.actions.act_window",
            "res_model": self._name,
            "res_id": self.id,
            "view_mode": "form",
            "views": [(self.env.ref("sale_return_wizard.view_sale_return_wizard_form").id, "form")],
            "target": "new",
            "context": dict(self.env.context, dialog_size="extra-large"),
        }

    # ------------------------------------------------------------------
    # Search / filter (server side, keeps edits on hidden lines)
    # ------------------------------------------------------------------
    def _get_filter_domain(self):
        self.ensure_one()
        domain = [("wizard_id", "=", self.id)]
        terms = [t.strip() for t in (self.filter_text or "").split(",") if t.strip()]
        if terms:
            text_domain = []
            for term in terms:
                term_domain = [
                    "|", "|", "|",
                    ("product_id.name", "ilike", term),
                    ("product_id.default_code", "ilike", term),
                    ("product_id.barcode", "ilike", term),
                    ("name", "ilike", term),
                ]
                text_domain = ["|"] + text_domain + term_domain if text_domain else term_domain
            domain += text_domain
        if self.filter_qty_field:
            if self.filter_qty_min:
                domain.append((self.filter_qty_field, ">=", self.filter_qty_min))
            if self.filter_qty_max:
                domain.append((self.filter_qty_field, "<=", self.filter_qty_max))
            if not self.filter_qty_min and not self.filter_qty_max:
                domain.append((self.filter_qty_field, ">", 0))
        if self.filter_only_actionable:
            domain += ["|", "|", ("returnable_qty", ">", 0), ("creditable_qty", ">", 0), ("pending_qty", ">", 0)]
        if self.filter_only_selected:
            domain += [("action", "!=", "keep"), ("quantity", ">", 0)]
        return domain

    def write(self, vals):
        res = super().write(vals)
        # The popup saves itself as soon as a filter field changes (see the
        # *_return_wizard_form JS view): recompute visibility right here so the
        # reload that follows the save shows the filtered lines.
        if FILTER_FIELDS.intersection(vals):
            for wiz in self:
                wiz._apply_visibility()
        return res

    def _apply_visibility(self):
        self.ensure_one()
        visible = self.env["sale.return.wizard.line"].search(self._get_filter_domain())
        (self.all_line_ids - visible).filtered("is_visible").write({"is_visible": False})
        (visible - self.all_line_ids.filtered("is_visible")).write({"is_visible": True})
        self.invalidate_recordset(["line_ids"])

    def action_apply_filter(self):
        self.ensure_one()
        self._apply_visibility()
        return self._action_reopen()

    def action_reset_filter(self):
        self.ensure_one()
        self.write({
            "filter_text": False, "filter_qty_field": False, "filter_qty_min": 0.0, "filter_qty_max": 0.0,
            "filter_only_actionable": False, "filter_only_selected": False,
        })
        return self._action_reopen()

    # ------------------------------------------------------------------
    # Bulk actions on the *visible* lines
    # ------------------------------------------------------------------
    def _bulk_set(self, action):
        self.ensure_one()
        visible = self.all_line_ids.filtered("is_visible")
        if action == "keep":
            visible.write({"action": "keep", "quantity": 0.0})
            return self._action_reopen()
        for line in visible:
            max_qty = line._get_max_qty(action)
            if float_compare(max_qty, 0.0, precision_rounding=line.product_uom_id.rounding) > 0:
                line.write({"action": action, "quantity": max_qty})
        return self._action_reopen()

    def action_bulk_return(self):
        return self._bulk_set("return")

    def action_bulk_credit(self):
        return self._bulk_set("credit")

    def action_bulk_cancel(self):
        return self._bulk_set("cancel")

    def action_bulk_keep(self):
        return self._bulk_set("keep")

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------
    def action_confirm(self):
        self.ensure_one()
        order = self.sale_order_id
        if not order._sret_is_open_for_return():
            raise UserError(_("The sale order %s is not confirmed (or is locked).", order.name))

        lines = self.all_line_ids.filtered(
            lambda l: l.action != "keep"
            and float_compare(l.quantity, 0.0, precision_rounding=l.product_uom_id.rounding) > 0
        )
        if not lines:
            raise UserError(_("Set an action and a quantity on at least one line."))

        # Re-check against live data: the SO may have moved since the popup opened.
        lines._refresh_snapshot()
        lines._check_quantities()
        if lines.filtered(lambda l: l.action == "return"):
            self._check_stock_access()

        if self.picking_id:
            reason = self.picking_id._sret_picking_block_reason()
            if reason:
                raise UserError(reason)
        log = self.env["sale.return.log"].create({
            "sale_order_id": order.id,
            "origin_picking_id": self.picking_id.id,
            "reason": self.reason,
            "line_ids": [
                (0, 0, {"sale_line_id": l.sale_line_id.id, "action": l.action, "quantity": l.quantity})
                for l in lines
            ],
        })
        log_line_by_sol = {ll.sale_line_id.id: ll for ll in log.line_ids}

        # 1) credit notes (computed on the pre-operation snapshot)
        credit_notes = self._create_credit_notes(lines, log_line_by_sol)
        # 2) undelivered quantities -> decrease ordered qty, Odoo shrinks pending moves
        self._process_cancellations(lines.filtered(lambda l: l.action == "cancel"), log_line_by_sol)
        # 3) physical returns
        pickings = self._create_return_pickings(lines.filtered(lambda l: l.action == "return"), log_line_by_sol)
        log.write({
            "picking_ids": [(6, 0, pickings.ids)],
            "credit_note_ids": [(6, 0, credit_notes.ids)],
        })
        if pickings and self.auto_validate_return:
            self._validate_return_pickings(pickings)

        self._post_order_message(log)
        return order.action_view_sale_return_logs() if self.env.context.get("open_log") else {
            "type": "ir.actions.act_window_close"
        }

    def _check_stock_access(self):
        if not self.env.su and not self.env.user.has_group("stock.group_stock_user"):
            raise UserError(_("You need Inventory / User rights to create return pickings."))

    # -- credit notes -------------------------------------------------------
    def _create_credit_notes(self, lines, log_line_by_sol):
        """Build at most two credit notes: posted portion and draft portion.

        * return  -> credit min(qty, creditable), linked to the SO line so that
                     qty_invoiced decreases (scenario A). Nothing if the line
                     was never invoiced (scenario B).
        * cancel  -> credit only what would become over-invoiced once the
                     ordered quantity is reduced (invoice-on-order policy).
        * credit  -> pure allowance, NOT linked to the SO line (otherwise the
                     quantity would become invoiceable again).
        Quantities invoiced on posted invoices are credited first; anything only
        covered by draft invoices lands on a draft credit note for review.
        """
        order = self.sale_order_id
        buckets = defaultdict(list)  # 'posted' / 'draft' -> [(wiz_line, qty, linked)]
        for line in lines:
            qty = line.credit_qty
            if float_is_zero(qty, precision_rounding=line.product_uom_id.rounding):
                continue
            posted_part = min(qty, line.posted_creditable_qty)
            draft_part = qty - posted_part
            linked = line.action != "credit"
            target = "posted" if self.auto_post_credit_note else "draft"
            if not float_is_zero(posted_part, precision_rounding=line.product_uom_id.rounding):
                buckets[target].append((line, posted_part, linked))
            if not float_is_zero(draft_part, precision_rounding=line.product_uom_id.rounding):
                buckets["draft"].append((line, draft_part, linked))

        moves = self.env["account.move"]
        posted_invoices = order.invoice_ids.filtered(
            lambda m: m.move_type == "out_invoice" and m.state == "posted"
        )
        for bucket in ("posted", "draft"):
            items = buckets.get(bucket)
            if not items:
                continue
            vals = order.with_company(order.company_id)._prepare_invoice()
            vals.update({
                "move_type": "out_refund",
                "ref": self.reason or _("Return of %s", order.name),
                "payment_reference": False,
                "transaction_ids": [(5, 0, 0)],
                "invoice_line_ids": [],
            })
            if len(posted_invoices) == 1:
                vals["reversed_entry_id"] = posted_invoices.id
            for line, qty, linked in items:
                line_vals = line.sale_line_id._prepare_invoice_line(quantity=qty)
                if not linked:
                    line_vals.pop("sale_line_ids", None)
                vals["invoice_line_ids"].append((0, 0, line_vals))
            move = self.env["account.move"].with_company(order.company_id).with_context(
                default_move_type="out_refund"
            ).create(vals)
            if bucket == "posted":
                move.action_post()
            move.message_post(body=Markup(_("Credit note created from return wizard for %s.")) % order._get_html_link())
            for line, qty, _linked in items:
                log_line = log_line_by_sol[line.sale_line_id.id]
                log_line.credit_qty += qty
                log_line.credit_move_ids = [(4, move.id)]
            moves |= move
        return moves

    # -- cancellations ------------------------------------------------------
    def _process_cancellations(self, lines, log_line_by_sol):
        """Reduce the ordered quantity of undelivered goods.

        In v16 and v17, decreasing product_uom_qty on a confirmed line launches a
        negative procurement which is merged into the pending delivery moves
        (and cancels them when they reach zero) - we rely on that standard
        mechanism instead of editing pickings by hand.
        """
        for line in lines:
            sol = line.sale_line_id
            if self.picking_id:
                # Opened from a delivery: shrink THIS delivery's moves first, so the
                # negative procurement triggered by the SO write finds nothing left
                # to cut and cannot reduce another backorder instead.
                self._decrease_pending_moves(sol, line.quantity)
            new_qty = sol.product_uom_qty - line.quantity
            sol.with_context(sale_return_wizard=True).write({"product_uom_qty": new_qty})
            log_line_by_sol[sol.id].so_qty_reduced += line.quantity

    def _decrease_pending_moves(self, sol, qty):
        """Remove ``qty`` (SO line UoM) from the open moves of the wizard's delivery.

        Moves are cut newest first; a move cut to zero is cancelled. Untouched
        upstream moves (pick / pack) that only feed the cut move are reduced
        too, so the warehouse does not keep picking goods nobody will ship.
        Quantities already set as done on an open move are never removed.
        """
        remaining = qty
        rounding = sol.product_uom.rounding
        for move in sol._sret_pending_moves(self.picking_id).sorted("id", reverse=True):
            if float_compare(remaining, 0.0, precision_rounding=rounding) <= 0:
                break
            available = move.product_uom._compute_quantity(sol._sret_move_cancellable_qty(move), sol.product_uom)
            take = min(available, remaining)
            if float_compare(take, 0.0, precision_rounding=rounding) <= 0:
                continue
            self._decrease_move(move, sol.product_uom._compute_quantity(take, move.product_uom))
            remaining -= take
        if float_compare(remaining, 0.0, precision_rounding=rounding) > 0:
            raise UserError(_(
                "%(product)s: only %(qty)s is still open on %(picking)s.",
                product=sol.product_id.display_name, qty=qty - remaining, picking=self.picking_id.name,
            ))

    def _decrease_move(self, move, qty):
        """Lower one open move's demand by ``qty`` (move UoM), cancelling it at zero."""
        rounding = move.product_uom.rounding
        origins = move.move_orig_ids.filtered(
            lambda m: m.state not in ("done", "cancel") and m.move_dest_ids == move
        )
        # v17: read the processed quantity BEFORE unreserving (see below).
        processed = move._sret_processed_qty()
        move._do_unreserve()
        new_qty = move.product_uom_qty - qty
        if float_compare(new_qty, processed, precision_rounding=rounding) < 0:
            raise UserError(_("%s: cannot cancel quantities already processed.", move.product_id.display_name))
        if float_is_zero(new_qty, precision_rounding=rounding):
            move._action_cancel()
        else:
            move.product_uom_qty = new_qty
            move._action_assign()
            # v17 merged reserved and done quantities into ``quantity``; if the
            # unreserve above reset lines the warehouse had already picked,
            # restore them so processed quantities are never lost.
            if not float_is_zero(processed, precision_rounding=rounding) and float_compare(
                move.quantity, processed, precision_rounding=rounding
            ) < 0:
                move.quantity = processed
                move.picked = True
        for origin in origins:
            origin_qty = min(
                move.product_uom._compute_quantity(qty, origin.product_uom),
                max(origin.product_uom_qty - origin._sret_processed_qty(), 0.0),
            )
            if float_compare(origin_qty, 0.0, precision_rounding=origin.product_uom.rounding) > 0:
                self._decrease_move(origin, origin_qty)

    # -- returns ----------------------------------------------------------------
    def _get_return_location(self, picking):
        if self.return_location_id:
            return self.return_location_id
        location = picking.location_id
        return_type = picking.picking_type_id.return_picking_type_id
        if return_type.default_location_dest_id.return_location:
            location = return_type.default_location_dest_id
        return location

    def _allocate_return_qty(self, line):
        """Spread the requested return quantity over done deliveries, newest first.

        :return: list of (stock.move, qty in product UoM)
        """
        sol = line.sale_line_id
        product_uom = sol.product_id.uom_id
        remaining = sol.product_uom._compute_quantity(line.quantity, product_uom)
        allocation = []
        for move in sol._sret_returnable_moves(self.picking_id or None).sorted(lambda m: (m.date, m.id), reverse=True):
            if float_compare(remaining, 0.0, precision_rounding=product_uom.rounding) <= 0:
                break
            available = move.product_uom._compute_quantity(sol._sret_move_remaining_qty(move), product_uom)
            take = min(available, remaining)
            if float_compare(take, 0.0, precision_rounding=product_uom.rounding) > 0:
                allocation.append((move, take))
                remaining -= take
        if float_compare(remaining, 0.0, precision_rounding=product_uom.rounding) > 0:
            raise UserError(_(
                "Unable to find enough delivered quantity to return for %s.", sol.product_id.display_name
            ))
        return allocation

    def _create_return_pickings(self, lines, log_line_by_sol):
        """Create one return per original delivery through the standard
        stock.return.picking wizard (keeps move chains, valuation and the
        to_refund flag consistent with Odoo's own returns)."""
        per_picking = defaultdict(list)  # picking -> [(move, qty, log_line)]
        for line in lines:
            log_line = log_line_by_sol[line.sale_line_id.id]
            for move, qty in self._allocate_return_qty(line):
                per_picking[move.picking_id].append((move, qty, log_line))

        pickings = self.env["stock.picking"]
        for picking, items in per_picking.items():
            return_wiz = self.env["stock.return.picking"].with_context(
                active_id=picking.id, active_model="stock.picking", active_ids=picking.ids
            ).create({
                "picking_id": picking.id,
                "location_id": self._get_return_location(picking).id,
                "product_return_moves": [
                    (0, 0, {"product_id": move.product_id.id, "move_id": move.id, "quantity": qty, "to_refund": True})
                    for move, qty, _log in items
                ],
            })
            new_picking_id, _type_id = return_wiz._create_returns()
            new_picking = self.env["stock.picking"].browse(new_picking_id)
            log_by_move = {move.id: log_line for move, _qty, log_line in items}
            for new_move in new_picking.move_ids:
                log_line = log_by_move.get(new_move.origin_returned_move_id.id)
                if log_line:
                    new_move.sale_return_log_line_id = log_line.id
            new_picking.message_post(body=Markup(_("Return created from the return wizard of %s.")) % self.sale_order_id._get_html_link())
            pickings |= new_picking
        return pickings

    def _validate_return_pickings(self, pickings):
        for picking in pickings:
            if picking.move_ids.filtered(lambda m: m.has_tracking != "none"):
                picking.message_post(body=_("Contains lot/serial-tracked products: to be validated by the warehouse."))
                continue
            for move in picking.move_ids.filtered(lambda m: m.state not in ("done", "cancel")):
                move.quantity = move.product_uom_qty
                move.picked = True
            res = picking.with_context(skip_backorder=True, skip_immediate=True, skip_sms=True).button_validate()
            if res is not True:
                picking.message_post(body=_("Automatic validation needs extra input: please validate manually."))

    def _post_order_message(self, log):
        order = self.sale_order_id
        rows = Markup("").join(
            Markup("<li>%s: %s %s %s</li>") % (
                dict(RETURN_ACTIONS)[ll.action], ll.quantity, ll.product_uom_id.name, ll.product_id.display_name,
            )
            for ll in log.line_ids
        )
        body = Markup("<b>%s</b> %s<ul>%s</ul>") % (log._get_html_link(), log.reason or "", rows)
        order.message_post(body=body)
        if self.picking_id:
            self.picking_id.message_post(body=body)


class SaleReturnWizardLine(models.TransientModel):
    _name = "sale.return.wizard.line"
    _description = "Sale Order Return / Credit Wizard Line"
    _order = "sequence, id"

    wizard_id = fields.Many2one("sale.return.wizard", required=True, ondelete="cascade", index=True)
    sale_line_id = fields.Many2one("sale.order.line", required=True, readonly=True, ondelete="cascade")
    sequence = fields.Integer(related="sale_line_id.sequence", store=True)
    product_id = fields.Many2one(related="sale_line_id.product_id", store=True)
    default_code = fields.Char(related="product_id.default_code", string="SKU")
    name = fields.Text("Description", readonly=True)
    product_uom_id = fields.Many2one(related="sale_line_id.product_uom", string="UoM")
    currency_id = fields.Many2one(related="wizard_id.currency_id")
    price_reduce = fields.Float(compute="_compute_credit", digits="Product Price")
    is_visible = fields.Boolean(default=True, index=True)

    # Snapshot of the SO line (SO line UoM), refreshed again at execution.
    ordered_qty = fields.Float("Ordered", digits="Product Unit of Measure", readonly=True)
    delivered_qty = fields.Float("Delivered", digits="Product Unit of Measure", readonly=True)
    invoiced_qty = fields.Float("Invoiced", digits="Product Unit of Measure", readonly=True)
    creditable_qty = fields.Float("Creditable", digits="Product Unit of Measure", readonly=True)
    posted_creditable_qty = fields.Float(digits="Product Unit of Measure", readonly=True)
    returnable_qty = fields.Float("Returnable", digits="Product Unit of Measure", readonly=True)
    pending_qty = fields.Float("Undelivered", digits="Product Unit of Measure", readonly=True)

    action = fields.Selection(RETURN_ACTIONS, required=True, default="keep")
    quantity = fields.Float(digits="Product Unit of Measure")
    max_qty = fields.Float("Max", compute="_compute_max_qty", digits="Product Unit of Measure")
    credit_qty = fields.Float("To Credit", compute="_compute_credit", digits="Product Unit of Measure")
    credit_amount = fields.Monetary("Credit (untaxed)", compute="_compute_credit", currency_field="currency_id")

    def _get_max_qty(self, action=None):
        self.ensure_one()
        action = action or self.action
        if action == "return":
            limit = self.returnable_qty
        elif action == "credit":
            limit = self.creditable_qty
        elif action == "cancel":
            limit = self.pending_qty
        else:
            limit = 0.0
        return max(min(limit, self.ordered_qty), 0.0)

    @api.depends("action", "returnable_qty", "creditable_qty", "pending_qty", "ordered_qty")
    def _compute_max_qty(self):
        for line in self:
            line.max_qty = line._get_max_qty()

    @api.depends("action", "quantity", "creditable_qty", "invoiced_qty", "ordered_qty",
                 "sale_line_id.discount", "sale_line_id.price_unit")
    def _compute_credit(self):
        for line in self:
            qty = line.quantity if line.action != "keep" else 0.0
            if line.action == "return":
                credit = min(qty, line.creditable_qty)
            elif line.action == "credit":
                credit = qty
            elif line.action == "cancel":
                # invoiced beyond the new ordered qty must be credited back
                credit = min(max(line.invoiced_qty - (line.ordered_qty - qty), 0.0), qty, line.creditable_qty)
            else:
                credit = 0.0
            sol = line.sale_line_id
            price = sol.price_unit * (1 - (sol.discount or 0.0) / 100.0)
            line.price_reduce = price
            line.credit_qty = max(credit, 0.0)
            line.credit_amount = line.credit_qty * price

    # -- client-side guidance ---------------------------------------------
    @api.onchange("action")
    def _onchange_action(self):
        if self.action == "keep":
            self.quantity = 0.0
        else:
            max_qty = self._get_max_qty()
            if not self.quantity or self.quantity > max_qty:
                self.quantity = max_qty

    @api.onchange("quantity")
    def _onchange_quantity(self):
        if self.quantity and self.action == "keep":
            # Typing a quantity on a 'Keep' line: pick the most natural action.
            for action in ("return", "credit", "cancel"):
                if self._get_max_qty(action) > 0:
                    self.action = action
                    break
        max_qty = self._get_max_qty()
        if self.quantity < 0 or float_compare(self.quantity, max_qty, precision_rounding=self.product_uom_id.rounding or 0.001) > 0:
            wrong = self.quantity
            self.quantity = max(min(self.quantity, max_qty), 0.0)
            return {"warning": {
                "title": _("Invalid quantity"),
                "message": _(
                    "%(product)s: %(qty)s is outside the allowed range 0 - %(max)s for '%(action)s'.",
                    product=self.product_id.display_name, qty=wrong, max=max_qty,
                    action=dict(RETURN_ACTIONS)[self.action],
                ),
            }}

    # -- server-side guarantees -------------------------------------------
    @api.constrains("quantity", "action")
    def _check_quantity_constraint(self):
        self._check_quantities()

    def _check_quantities(self):
        errors = []
        for line in self:
            rounding = line.product_uom_id.rounding or 0.001
            name = line.product_id.display_name
            if float_compare(line.quantity, 0.0, precision_rounding=rounding) < 0:
                errors.append(_("%s: quantity cannot be negative.", name))
                continue
            if line.action == "keep":
                if not float_is_zero(line.quantity, precision_rounding=rounding):
                    errors.append(_("%s: a kept line cannot have a quantity.", name))
                continue
            if float_compare(line.quantity, line.ordered_qty, precision_rounding=rounding) > 0:
                errors.append(_("%(p)s: %(q)s exceeds the ordered quantity (%(o)s).",
                                p=name, q=line.quantity, o=line.ordered_qty))
                continue
            if line.action == "return" and float_compare(line.quantity, line.delivered_qty, precision_rounding=rounding) > 0:
                errors.append(_("%(p)s: cannot return %(q)s, only %(d)s delivered.",
                                p=name, q=line.quantity, d=line.delivered_qty))
                continue
            max_qty = line._get_max_qty()
            if float_compare(line.quantity, max_qty, precision_rounding=rounding) > 0:
                label = {"return": _("returnable"), "credit": _("creditable (invoiced)"),
                         "cancel": _("undelivered")}[line.action]
                errors.append(_("%(p)s: %(q)s exceeds the %(label)s quantity (%(m)s).",
                                p=name, q=line.quantity, label=label, m=max_qty))
        if errors:
            raise ValidationError("\n".join(errors))

    def _refresh_snapshot(self):
        for line in self:
            line.write(line.sale_line_id._sret_get_snapshot(line.wizard_id.picking_id))
