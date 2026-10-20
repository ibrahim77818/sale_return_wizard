# -*- coding: utf-8 -*-
from odoo import Command
from odoo.exceptions import UserError, ValidationError
from odoo.tests import Form, tagged

from odoo.addons.sale.tests.common import TestSaleCommon


@tagged("post_install", "-at_install", "sale_return_wizard")
class TestSaleReturnWizard(TestSaleCommon):
    """Quantity guards and document routing of the Sale Return / Credit wizard.

    Order used everywhere (all prices untaxed):
        A  storable, invoice on delivery, 100.0
        B  storable, invoice on delivery,  50.0
        C  storable, invoice on ORDER,     20.0
        D  storable, invoice on delivery,  10.0
    """

    @classmethod
    def setUpClass(cls, chart_template_ref=None):
        super().setUpClass(chart_template_ref=chart_template_ref)
        cls.warehouse = cls.env["stock.warehouse"].search([("company_id", "=", cls.env.company.id)], limit=1)
        cls.stock_location = cls.warehouse.lot_stock_id

        def product(code, price, policy):
            return cls.env["product.product"].create({
                "name": "Product %s" % code,
                "default_code": "SKU-%s" % code,
                "barcode": "BAR-%s" % code,
                "detailed_type": "product",
                "invoice_policy": policy,
                "list_price": price,
                "taxes_id": [Command.clear()],
            })

        cls.prod_a = product("A", 100.0, "delivery")
        cls.prod_b = product("B", 50.0, "delivery")
        cls.prod_c = product("C", 20.0, "order")
        cls.prod_d = product("D", 10.0, "delivery")
        for prod in cls.prod_a | cls.prod_b | cls.prod_c | cls.prod_d:
            cls.env["stock.quant"]._update_available_quantity(prod, cls.stock_location, 1000.0)

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def _create_so(self, qty=10.0):
        so = self.env["sale.order"].create({
            "partner_id": self.partner_a.id,
            "warehouse_id": self.warehouse.id,
            "order_line": [
                Command.create({
                    "product_id": prod.id,
                    "product_uom_qty": qty,
                    "price_unit": prod.list_price,
                    "tax_id": [Command.clear()],
                })
                for prod in (self.prod_a, self.prod_b, self.prod_c, self.prod_d)
            ],
        })
        so.action_confirm()
        return so

    def _deliver(self, so, quantities):
        """Validate the open delivery with the given quantities (backorder for the rest)."""
        picking = so.picking_ids.filtered(
            lambda p: p.picking_type_code == "outgoing" and p.state not in ("done", "cancel")
        )
        self.assertEqual(len(picking), 1)
        picking.action_assign()
        for move in picking.move_ids:
            move.quantity_done = quantities.get(move.product_id, 0.0)
        picking.with_context(skip_backorder=True, skip_immediate=True).button_validate()
        return picking

    def _invoice(self, so, post=True):
        invoice = so._create_invoices()
        if post:
            invoice.action_post()
        return invoice

    def _wizard(self, so, **vals):
        action = so.action_open_sale_return_wizard()
        wizard = self.env["sale.return.wizard"].browse(action["res_id"])
        if vals:
            wizard.write(vals)
        return wizard

    @staticmethod
    def _line(wizard, product):
        return wizard.all_line_ids.filtered(lambda l: l.product_id == product)

    @staticmethod
    def _sol(so, product):
        return so.order_line.filtered(lambda l: l.product_id == product)

    def _process(self, wizard):
        wizard.action_confirm()
        return self.env["sale.return.log"].search(
            [("sale_order_id", "=", wizard.sale_order_id.id)], order="id desc", limit=1
        )

    # ------------------------------------------------------------------
    # 1. Quantity constraints
    # ------------------------------------------------------------------
    def test_01_quantity_constraints(self):
        so = self._create_so()
        self._deliver(so, {self.prod_a: 10, self.prod_b: 10, self.prod_c: 6, self.prod_d: 10})
        wizard = self._wizard(so)
        line_a, line_c = self._line(wizard, self.prod_a), self._line(wizard, self.prod_c)

        self.assertEqual(line_a.returnable_qty, 10.0)
        self.assertEqual(line_c.returnable_qty, 6.0)
        self.assertEqual(line_c.pending_qty, 4.0)
        self.assertEqual(line_a.creditable_qty, 0.0, "Nothing invoiced yet")

        with self.assertRaises(ValidationError, msg="more than ordered/delivered"):
            line_a.write({"action": "return", "quantity": 11.0})
        with self.assertRaises(ValidationError, msg="more than delivered"):
            line_c.write({"action": "return", "quantity": 7.0})
        with self.assertRaises(ValidationError, msg="credit without invoice"):
            line_a.write({"action": "credit", "quantity": 1.0})
        with self.assertRaises(ValidationError, msg="cancel a fully delivered line"):
            line_a.write({"action": "cancel", "quantity": 1.0})
        with self.assertRaises(ValidationError, msg="cancel more than undelivered"):
            line_c.write({"action": "cancel", "quantity": 5.0})
        with self.assertRaises(ValidationError, msg="keep with a quantity"):
            line_a.write({"action": "keep", "quantity": 2.0})
        with self.assertRaises(ValidationError, msg="negative quantity"):
            line_a.write({"action": "return", "quantity": -1.0})

        # boundaries are accepted
        line_a.write({"action": "return", "quantity": 10.0})
        line_c.write({"action": "cancel", "quantity": 4.0})

        # nothing selected -> refused
        wizard2 = self._wizard(so)
        with self.assertRaises(UserError):
            wizard2.action_confirm()

    def test_02_execution_rechecks_live_quantities(self):
        """A stale wizard cannot return more than what is still returnable."""
        so = self._create_so()
        self._deliver(so, {self.prod_a: 10, self.prod_b: 10, self.prod_c: 10, self.prod_d: 10})
        stale = self._wizard(so)
        self._line(stale, self.prod_a).write({"action": "return", "quantity": 8.0})

        fresh = self._wizard(so)
        self._line(fresh, self.prod_a).write({"action": "return", "quantity": 5.0})
        self._process(fresh)

        with self.assertRaises(ValidationError):
            stale.action_confirm()  # only 5 left to return

    def test_03_draft_order_refused(self):
        so = self.env["sale.order"].create({
            "partner_id": self.partner_a.id,
            "order_line": [Command.create({"product_id": self.prod_a.id, "product_uom_qty": 1})],
        })
        with self.assertRaises(UserError):
            so.action_open_sale_return_wizard()

    # ------------------------------------------------------------------
    # 2. Scenario A: delivered & invoiced
    # ------------------------------------------------------------------
    def test_10_scenario_a_delivered_invoiced(self):
        so = self._create_so()
        self._deliver(so, {self.prod_a: 10, self.prod_b: 10, self.prod_c: 10, self.prod_d: 10})
        self._invoice(so)

        wizard = self._wizard(so, reason="Damaged")
        self._line(wizard, self.prod_a).write({"action": "return", "quantity": 3.0})   # partial
        self._line(wizard, self.prod_b).write({"action": "return", "quantity": 10.0})  # full
        log = self._process(wizard)

        # one return picking, received
        self.assertEqual(len(log.picking_ids), 1)
        picking = log.picking_ids
        self.assertEqual(picking.state, "done")
        self.assertEqual(picking.picking_type_code, "incoming")
        moves = {m.product_id: m for m in picking.move_ids}
        self.assertEqual(moves[self.prod_a].quantity_done, 3.0)
        self.assertEqual(moves[self.prod_b].quantity_done, 10.0)
        self.assertTrue(all(m.to_refund for m in picking.move_ids))

        # one posted credit note with exact quantities / proportional amounts
        self.assertEqual(len(log.credit_note_ids), 1)
        refund = log.credit_note_ids
        self.assertEqual(refund.move_type, "out_refund")
        self.assertEqual(refund.state, "posted")
        self.assertEqual(refund.ref, "Damaged")
        rlines = {l.product_id: l for l in refund.invoice_line_ids}
        self.assertEqual(rlines[self.prod_a].quantity, 3.0)
        self.assertEqual(rlines[self.prod_b].quantity, 10.0)
        self.assertAlmostEqual(refund.amount_untaxed, 3 * 100.0 + 10 * 50.0)
        self.assertEqual(rlines[self.prod_a].sale_line_ids, self._sol(so, self.prod_a))

        # SO quantities stay consistent: nothing left to invoice or deliver
        sol_a, sol_b = self._sol(so, self.prod_a), self._sol(so, self.prod_b)
        self.assertEqual((sol_a.product_uom_qty, sol_a.qty_delivered, sol_a.qty_invoiced), (7.0, 7.0, 7.0))
        self.assertEqual((sol_b.product_uom_qty, sol_b.qty_delivered, sol_b.qty_invoiced), (0.0, 0.0, 0.0))
        self.assertEqual(sol_a.qty_to_invoice, 0.0)
        self.assertEqual(sol_b.qty_to_invoice, 0.0)
        self.assertEqual(so.invoice_status, "invoiced")

        # trace
        self.assertEqual(so.sale_return_log_count, 1)
        self.assertEqual(log.state, "done")
        self.assertEqual(log.line_ids.filtered(lambda l: l.product_id == self.prod_a).so_qty_reduced, 3.0)

    def test_11_partially_invoiced_return(self):
        """Delivered 10, invoiced 6: returning 8 credits 6 and un-delivers 8."""
        so = self._create_so()
        self._deliver(so, {self.prod_a: 6})
        self._invoice(so)  # A 6 (C 10 as it is invoiced on order)
        self._deliver(so, {self.prod_a: 4})

        wizard = self._wizard(so)
        line_a = self._line(wizard, self.prod_a)
        self.assertEqual((line_a.returnable_qty, line_a.creditable_qty), (10.0, 6.0))
        line_a.write({"action": "return", "quantity": 8.0})
        self.assertEqual(line_a.credit_qty, 6.0)
        log = self._process(wizard)

        refund = log.credit_note_ids
        self.assertEqual(refund.state, "posted")
        self.assertEqual(refund.invoice_line_ids.quantity, 6.0)
        sol_a = self._sol(so, self.prod_a)
        self.assertEqual(
            (sol_a.product_uom_qty, sol_a.qty_delivered, sol_a.qty_invoiced, sol_a.qty_to_invoice),
            (2.0, 2.0, 0.0, 2.0),
        )

    def test_12_draft_invoice_gives_draft_credit_note(self):
        so = self._create_so()
        self._deliver(so, {self.prod_a: 10, self.prod_b: 10, self.prod_c: 10, self.prod_d: 10})
        self._invoice(so, post=False)

        wizard = self._wizard(so)
        self._line(wizard, self.prod_a).write({"action": "return", "quantity": 3.0})
        log = self._process(wizard)

        self.assertEqual(len(log.credit_note_ids), 1)
        self.assertEqual(log.credit_note_ids.state, "draft")
        self.assertEqual(log.credit_note_ids.invoice_line_ids.quantity, 3.0)

    # ------------------------------------------------------------------
    # 3. Scenario B: delivered & NOT invoiced / partial delivery
    # ------------------------------------------------------------------
    def test_20_scenario_b_delivered_not_invoiced(self):
        so = self._create_so()
        self._deliver(so, {self.prod_a: 10, self.prod_b: 10, self.prod_c: 10, self.prod_d: 10})

        wizard = self._wizard(so)
        self._line(wizard, self.prod_a).write({"action": "return", "quantity": 4.0})
        log = self._process(wizard)

        self.assertFalse(log.credit_note_ids, "Nothing invoiced -> no credit note")
        self.assertEqual(log.picking_ids.state, "done")
        sol_a = self._sol(so, self.prod_a)
        self.assertEqual((sol_a.product_uom_qty, sol_a.qty_delivered, sol_a.qty_to_invoice), (6.0, 6.0, 6.0))
        # the next invoice only bills what the customer kept
        invoice = self._invoice(so)
        self.assertEqual(invoice.invoice_line_ids.filtered(lambda l: l.product_id == self.prod_a).quantity, 6.0)

    def test_21_cancel_undelivered_backorder(self):
        so = self._create_so()
        self._deliver(so, {self.prod_a: 10, self.prod_b: 10, self.prod_c: 6, self.prod_d: 10})
        backorder_move = so.picking_ids.move_ids.filtered(
            lambda m: m.product_id == self.prod_c and m.state not in ("done", "cancel")
        )
        self.assertEqual(backorder_move.product_uom_qty, 4.0)

        wizard = self._wizard(so)
        self._line(wizard, self.prod_c).write({"action": "cancel", "quantity": 4.0})
        log = self._process(wizard)

        sol_c = self._sol(so, self.prod_c)
        self.assertEqual(sol_c.product_uom_qty, 6.0)
        pending = so.picking_ids.move_ids.filtered(
            lambda m: m.product_id == self.prod_c and m.state not in ("done", "cancel")
        )
        self.assertFalse(sum(pending.mapped("product_uom_qty")), "Pending delivery must be gone")
        self.assertFalse(log.picking_ids)
        self.assertFalse(log.credit_note_ids, "C was not invoiced yet")

    def test_22_deferred_return_reduces_so_on_receipt(self):
        so = self._create_so()
        self._deliver(so, {self.prod_a: 10, self.prod_b: 10, self.prod_c: 10, self.prod_d: 10})
        wizard = self._wizard(so, auto_validate_return=False)
        self._line(wizard, self.prod_a).write({"action": "return", "quantity": 3.0})
        log = self._process(wizard)

        picking = log.picking_ids
        self.assertNotEqual(picking.state, "done")
        self.assertEqual(log.state, "waiting")
        sol_a = self._sol(so, self.prod_a)
        self.assertEqual(sol_a.product_uom_qty, 10.0, "Unchanged until goods are received")

        # warehouse receives later
        picking.move_ids.quantity_done = 3.0
        picking.with_context(skip_backorder=True, skip_immediate=True).button_validate()
        self.assertEqual((sol_a.product_uom_qty, sol_a.qty_delivered), (7.0, 7.0))
        self.assertEqual(log.state, "done")
        self.assertEqual(log.line_ids.qty_received, 3.0)

    # ------------------------------------------------------------------
    # 4. Mixed partial / full routing on one order
    # ------------------------------------------------------------------
    def test_30_mixed_routing(self):
        so = self._create_so()
        self._deliver(so, {self.prod_a: 10, self.prod_b: 10, self.prod_c: 6, self.prod_d: 10})
        self._invoice(so)  # A10 B10 D10 (delivery) + C10 (order policy, 4 not delivered)

        wizard = self._wizard(so)
        self._line(wizard, self.prod_a).write({"action": "return", "quantity": 2.0})
        self._line(wizard, self.prod_c).write({"action": "cancel", "quantity": 4.0})
        self._line(wizard, self.prod_d).write({"action": "credit", "quantity": 1.0})
        # B kept
        self.assertAlmostEqual(wizard.estimated_credit_amount, 2 * 100.0 + 4 * 20.0 + 1 * 10.0)
        log = self._process(wizard)

        # documents
        self.assertEqual(len(log.picking_ids), 1)
        self.assertEqual(log.picking_ids.move_ids.product_id, self.prod_a)
        self.assertEqual(len(log.credit_note_ids), 1)
        refund = log.credit_note_ids
        self.assertEqual(refund.state, "posted")
        qty = {l.product_id: l.quantity for l in refund.invoice_line_ids}
        self.assertEqual(qty, {self.prod_a: 2.0, self.prod_c: 4.0, self.prod_d: 1.0})
        self.assertAlmostEqual(refund.amount_untaxed, 290.0)

        sol = {p: self._sol(so, p) for p in (self.prod_a, self.prod_b, self.prod_c, self.prod_d)}
        # A: returned & credited
        self.assertEqual((sol[self.prod_a].product_uom_qty, sol[self.prod_a].qty_invoiced), (8.0, 8.0))
        # B: untouched
        self.assertEqual((sol[self.prod_b].product_uom_qty, sol[self.prod_b].qty_invoiced), (10.0, 10.0))
        # C: over-invoiced part credited, pending delivery removed
        self.assertEqual((sol[self.prod_c].product_uom_qty, sol[self.prod_c].qty_invoiced), (6.0, 6.0))
        self.assertEqual(sol[self.prod_c].qty_to_invoice, 0.0)
        # D: allowance, NOT linked -> not invoiceable again
        self.assertEqual((sol[self.prod_d].qty_invoiced, sol[self.prod_d].qty_to_invoice), (10.0, 0.0))
        self.assertFalse(refund.invoice_line_ids.filtered(lambda l: l.product_id == self.prod_d).sale_line_ids)
        for product in sol:
            self.assertEqual(sol[product].qty_to_invoice, 0.0, product.display_name)

        # allowance is remembered: only 9 left to credit on D
        wizard2 = self._wizard(so)
        self.assertEqual(self._line(wizard2, self.prod_d).creditable_qty, 9.0)
        with self.assertRaises(ValidationError):
            self._line(wizard2, self.prod_d).write({"action": "credit", "quantity": 10.0})

    def test_31_full_return_then_nothing_left(self):
        so = self._create_so(qty=5.0)
        self._deliver(so, {self.prod_a: 5, self.prod_b: 5, self.prod_c: 5, self.prod_d: 5})
        self._invoice(so)
        wizard = self._wizard(so)
        wizard.action_bulk_return()
        self.assertTrue(all(l.action == "return" and l.quantity == 5.0 for l in wizard.all_line_ids))
        log = self._process(wizard)
        self.assertAlmostEqual(log.credit_note_ids.amount_untaxed, 5 * (100 + 50 + 20 + 10))
        self.assertTrue(all(l.product_uom_qty == 0 and l.qty_invoiced == 0 for l in so.order_line))

        wizard2 = self._wizard(so)
        self.assertTrue(all(l.returnable_qty == 0 for l in wizard2.all_line_ids))

    # ------------------------------------------------------------------
    # 5. Search / filter on the popup
    # ------------------------------------------------------------------
    def test_40_filters_and_bulk(self):
        so = self._create_so()
        self._deliver(so, {self.prod_a: 10, self.prod_b: 10, self.prod_c: 6, self.prod_d: 10})
        wizard = self._wizard(so)
        self.assertEqual(len(wizard.line_ids), 4)

        wizard.filter_text = "sku-c"
        wizard.action_apply_filter()
        self.assertEqual(wizard.line_ids.product_id, self.prod_c)
        self.assertEqual(len(wizard.all_line_ids), 4, "Hidden lines are kept")

        wizard.filter_text = "BAR-A, Product B"  # barcode OR name
        wizard.action_apply_filter()
        self.assertEqual(wizard.line_ids.product_id, self.prod_a | self.prod_b)

        # bulk action only touches the visible lines
        wizard.action_bulk_return()
        self.assertEqual(self._line(wizard, self.prod_a).quantity, 10.0)
        self.assertEqual(self._line(wizard, self.prod_c).action, "keep")

        wizard.write({"filter_text": False, "filter_qty_field": "delivered_qty", "filter_qty_max": 6.0})
        wizard.action_apply_filter()
        self.assertEqual(wizard.line_ids.product_id, self.prod_c)

        wizard.write({"filter_qty_max": 0.0, "filter_only_selected": True})
        wizard.action_apply_filter()
        self.assertEqual(wizard.line_ids.product_id, self.prod_a | self.prod_b)

        wizard.action_reset_filter()
        self.assertEqual(len(wizard.line_ids), 4)
        self.assertEqual(self._line(wizard, self.prod_b).quantity, 10.0, "Edits survive filtering")

        # live filtering: a plain save of a filter field re-filters (what the popup JS does)
        wizard.write({"filter_qty_field": "invoiced_qty"})  # quantity type alone = "> 0"
        self.assertFalse(wizard.line_ids, "Nothing invoiced yet")
        wizard.write({"filter_qty_field": "pending_qty"})
        self.assertEqual(wizard.line_ids.product_id, self.prod_c)
        wizard.write({"filter_qty_field": False})
        self.assertEqual(len(wizard.line_ids), 4)

    def test_41_onchange_clamps_quantity(self):
        so = self._create_so()
        self._deliver(so, {self.prod_a: 10, self.prod_b: 10, self.prod_c: 6, self.prod_d: 10})
        wizard = self._wizard(so)
        line_c = self._line(wizard, self.prod_c)
        new_line = line_c.new(origin=line_c)
        new_line.action = "return"
        new_line._onchange_action()
        self.assertEqual(new_line.quantity, 6.0, "Defaults to the max returnable")
        new_line.quantity = 9.0
        res = new_line._onchange_quantity()
        self.assertIn("warning", res)
        self.assertEqual(new_line.quantity, 6.0)

    # ------------------------------------------------------------------
    # 6. Opened from a delivery (stock.picking button)
    # ------------------------------------------------------------------
    def _picking_wizard(self, picking, **vals):
        action = picking.action_open_sale_return_wizard()
        wizard = self.env["sale.return.wizard"].browse(action["res_id"])
        if vals:
            wizard.write(vals)
        return wizard

    def test_50_picking_scope_limits_returns_to_that_delivery(self):
        so = self._create_so()
        pick1 = self._deliver(so, {self.prod_a: 6, self.prod_b: 10, self.prod_c: 6, self.prod_d: 10})
        pick2 = self._deliver(so, {self.prod_a: 4})  # backorder: A4 done, C4 left open in pick3
        self.assertTrue(pick1.sale_return_wizard_allowed)

        wizard = self._picking_wizard(pick1)
        self.assertEqual(wizard.picking_id, pick1)
        self.assertEqual(self._line(wizard, self.prod_a).returnable_qty, 6.0, "Only what pick1 shipped")
        self.assertEqual(self._line(wizard, self.prod_c).pending_qty, 0.0, "pick1 has nothing open")
        with self.assertRaises(ValidationError):
            self._line(wizard, self.prod_a).write({"action": "return", "quantity": 7.0})
        with self.assertRaises(ValidationError):
            self._line(wizard, self.prod_c).write({"action": "cancel", "quantity": 1.0})

        self._line(wizard, self.prod_a).write({"action": "return", "quantity": 6.0})
        log = self._process(wizard)
        self.assertEqual(log.origin_picking_id, pick1)
        self.assertEqual(log.picking_ids.move_ids.origin_returned_move_id.picking_id, pick1)
        sol_a = self._sol(so, self.prod_a)
        self.assertEqual((sol_a.product_uom_qty, sol_a.qty_delivered), (4.0, 4.0))
        self.assertEqual(pick1.sale_return_log_count, 1)

        # second delivery only lists its own product, and still has its 4 to return
        wizard2 = self._picking_wizard(pick2)
        self.assertEqual(wizard2.all_line_ids.product_id, self.prod_a)
        self.assertEqual(self._line(wizard2, self.prod_a).returnable_qty, 4.0)
        # first delivery is fully returned for A now
        self.assertEqual(self._line(self._picking_wizard(pick1), self.prod_a).returnable_qty, 0.0)

    def test_51_picking_cancel_targets_that_backorder(self):
        so = self._create_so()
        self._deliver(so, {self.prod_a: 10, self.prod_b: 10, self.prod_c: 6, self.prod_d: 10})
        backorder = so.picking_ids.filtered(lambda p: p.state not in ("done", "cancel"))
        self.assertEqual(len(backorder), 1)

        wizard = self._picking_wizard(backorder)
        self.assertEqual(wizard.all_line_ids.product_id, self.prod_c)
        line_c = self._line(wizard, self.prod_c)
        self.assertEqual((line_c.pending_qty, line_c.returnable_qty), (4.0, 0.0))
        line_c.write({"action": "cancel", "quantity": 4.0})
        self._process(wizard)

        self.assertEqual(backorder.state, "cancel")
        self.assertEqual(self._sol(so, self.prod_c).product_uom_qty, 6.0)
        still_open = so.picking_ids.move_ids.filtered(
            lambda m: m.product_id == self.prod_c and m.state not in ("done", "cancel")
        )
        self.assertFalse(still_open, "No replacement delivery is procured")

    def test_52_picking_cancel_keeps_processed_quantities(self):
        so = self._create_so()
        self._deliver(so, {self.prod_a: 10, self.prod_b: 10, self.prod_c: 6, self.prod_d: 10})
        backorder = so.picking_ids.filtered(lambda p: p.state not in ("done", "cancel"))
        backorder.action_assign()
        backorder.move_ids.quantity_done = 1.0  # warehouse already picked 1

        wizard = self._picking_wizard(backorder)
        line_c = self._line(wizard, self.prod_c)
        self.assertEqual(line_c.pending_qty, 3.0)
        with self.assertRaises(ValidationError):
            line_c.write({"action": "cancel", "quantity": 4.0})
        line_c.write({"action": "cancel", "quantity": 3.0})
        self._process(wizard)

        move_c = backorder.move_ids.filtered(lambda m: m.state != "cancel")
        self.assertEqual((move_c.product_uom_qty, move_c.quantity_done), (1.0, 1.0))
        self.assertEqual(self._sol(so, self.prod_c).product_uom_qty, 7.0)

    def test_53_picking_refusals(self):
        so = self._create_so(qty=2.0)
        pick = self._deliver(so, {self.prod_a: 2, self.prod_b: 2, self.prod_c: 2, self.prod_d: 2})

        # a return is not a delivery
        wizard = self._picking_wizard(pick)
        wizard.action_bulk_return()
        log = self._process(wizard)
        return_picking = log.picking_ids
        self.assertFalse(return_picking.sale_return_wizard_allowed)
        with self.assertRaises(UserError):
            return_picking.action_open_sale_return_wizard()

        # nothing left on a fully returned, uninvoiced delivery
        with self.assertRaises(UserError):
            pick.action_open_sale_return_wizard()

        # not linked to a sale order
        internal = self.env["stock.picking"].create({
            "picking_type_id": self.warehouse.int_type_id.id,
            "location_id": self.warehouse.lot_stock_id.id,
            "location_dest_id": self.warehouse.lot_stock_id.id,
        })
        self.assertFalse(internal.sale_return_wizard_allowed)
        with self.assertRaises(UserError):
            internal.action_open_sale_return_wizard()

        # order locked after delivery
        so2 = self._create_so(qty=1.0)
        pick2 = self._deliver(so2, {self.prod_a: 1, self.prod_b: 1, self.prod_c: 1, self.prod_d: 1})
        so2.action_done()
        self.assertFalse(pick2.sale_return_wizard_allowed)
        with self.assertRaises(UserError):
            pick2.action_open_sale_return_wizard()

    def test_42_header_summary_follows_unsaved_edits(self):
        """The counters above the table update while lines are edited, before any save."""
        so = self._create_so()
        self._deliver(so, {self.prod_a: 10, self.prod_b: 10, self.prod_c: 10, self.prod_d: 10})
        self._invoice(so)
        wizard = self._wizard(so)
        self._line(wizard, self.prod_b).write({"action": "credit", "quantity": 2.0})  # saved: 100.0
        form = Form(wizard)
        index = wizard.line_ids.ids.index(self._line(wizard, self.prod_a).id)
        with form.line_ids.edit(index) as line:
            line.action = "return"
            line.quantity = 3.0
        self.assertEqual(form.selected_line_count, 2)
        self.assertAlmostEqual(form.estimated_credit_amount, 3 * 100.0 + 2 * 50.0)
