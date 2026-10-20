# Sale Return & Credit Wizard (Odoo 16)

`sale_return_wizard` adds a **Return / Credit** button to confirmed Sale Orders. It opens a popup listing every order line, where each line can be kept, returned, credited or cancelled. On validation the module creates the right pickings and credit notes, and it records the whole operation in a return log.

## Architecture

| Layer | Model | Role |
|---|---|---|
| Wizard | `sale.return.wizard` (transient) | Options, server-side filters, bulk actions, execution engine |
| Wizard | `sale.return.wizard.line` (transient) | Quantity snapshot of each SO line, plus its action and quantity |
| Trace | `sale.return.log` / `sale.return.log.line` | Persistent record of every run, with links to its pickings and credit notes (sequence `SRET/YYYY/xxxxx`) |
| Hook | `stock.move._action_done` | Reduces the SO ordered quantity once the returned goods are actually received |
| Helpers | `sale.order.line._sret_*` | Returnable, creditable and posted quantities (all in the SO line UoM) |

### Why the wizard is created server-side
`action_open_sale_return_wizard()` creates the wizard and all of its lines, then reopens it with a real `res_id`. Because the record already exists, the **Filter** and bulk buttons can save the user's edits, toggle the stored `is_visible` flag and reopen the same wizard. `line_ids` carries `domain=[('is_visible','=',True)]` and is the list the popup shows. `all_line_ids` covers the same lines with no filter and is the one that gets processed. Hidden lines therefore keep their edited quantities, which matters when an order has hundreds of lines.

Filter options:
- Free text, matched against product name, SKU, barcode or line description. Separate terms with commas to match any of them.
- A quantity threshold (min/max) on the ordered, delivered, invoiced, returnable or undelivered quantity.
- "Actionable only".
- "Selected only".

## Line actions and limits

| Action | Max quantity | Documents |
|---|---|---|
| Keep | 0 | none |
| Return & Refund | min(delivered, still-returnable on done deliveries) | Return picking through the standard `stock.return.picking` (`to_refund=True`). A credit note for min(qty, invoiced), **linked** to the SO line. Ordered qty is reduced once the goods are received. |
| Credit Only | invoiced − earlier allowances | Credit note line **not linked** to the SO line, so the quantity cannot be re-invoiced. The allowance is tracked through the log. |
| Cancel Undelivered | ordered − delivered | Ordered qty is decreased, and Odoo's negative procurement shrinks or cancels the pending delivery. Any quantity that becomes over-invoiced (invoice-on-order products) is credited and linked. |

Validation happens at three levels:
1. **Client side**: `@api.onchange` clamps the quantity and shows a warning, and rows outside the allowed range turn red.
2. **`@api.constrains`** on `quantity` and `action`.
3. **At execution**: the snapshot is re-read from the live SO and every check runs again. This catches wizards that went stale while another user was working on the same order.

## Document routing

- **Delivered and invoiced (posted).** The goods come back on a return picking. A credit note is created and posted, and `reversed_entry_id` points to the original invoice when there is only one.
- **Delivered, not invoiced.** A return picking is created but no credit note. The delivered and ordered quantities drop, so the next invoice only bills what the customer kept.
- **Delivered, partially invoiced.** Only the invoiced part is credited. The rest only reduces the delivered quantity.
- **Quantity only on draft invoices.** That part goes to a separate **draft** credit note for review.
- **Partial delivery (backorder).** Use "Cancel Undelivered" to remove the remaining quantity.
- **Lot/serial-tracked products, or "Receive Returned Goods Now" unchecked.** The return stays open for the warehouse. The SO quantity is reduced only when the return is validated, through the `stock.move` hook.

## Security
- `sale_return_wizard.group_sale_return_user` grants access to the button. Sales Managers get it automatically.
- Creating return pickings also requires *Inventory / User*.
- Users in the Salesman, Sales Manager and Inventory User groups can read the log.

## Tests
```bash
odoo-bin -d test_db -i sale_return_wizard --test-tags /sale_return_wizard --stop-after-init
```
The tests run against a database that has a chart of accounts (they use `TestSaleCommon`, which builds on `AccountTestInvoicingCommon`). They cover:
- Quantity constraints
- Stale-wizard re-check
- Scenario A, including partially invoiced and draft-invoice cases
- Scenario B, including backorder cancellation and deferred receipt
- Mixed routing on a single order
- A full return
- Filters and bulk actions
- Onchange clamping

## From a delivery (since 16.0.1.2.0)
Customer deliveries of a confirmed SO also show a **Return / Credit** button. It opens the same wizard, scoped to that one delivery:

- **Lines listed:** only the SO lines with a move in this delivery.
- **Returnable:** only what this delivery shipped and has not returned yet.
- **Credit Only:** capped at what this delivery shipped.
- **Cancel Undelivered:** capped at this delivery's open demand, excluding quantities already set as done. The cut is applied to this delivery's own moves, plus the pick/pack moves that only feed them. Once those moves are cut, the SO quantity change finds nothing left to procure, so it cannot shrink another backorder or create a new delivery.

The button is hidden or refused for:
- transfers with no SO
- pick/pack steps, receipts, internal transfers and dropships
- returns
- cancelled transfers
- locked or unconfirmed orders
- deliveries with nothing left to process

Lines that belong to another SO, and kit products, are left out and named in an info banner. The return log records the delivery it came from, and the delivery has a **Return Logs** smart button.
