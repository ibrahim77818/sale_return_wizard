/** @odoo-module **/
/**
 * Live filtering for the sale return / credit popup.
 *
 * As soon as one of the filter fields changes (toggle, selection, number, or
 * the search box on Enter / blur), the wizard record is saved. The server-side
 * write() recomputes which lines are visible, and the reload that follows the
 * save re-reads the lines one2many (its domain hides filtered-out lines).
 * Quantities typed on hidden lines are saved first, so nothing is lost.
 */
import { registry } from "@web/core/registry";
import { formView } from "@web/views/form/form_view";
import { FormController } from "@web/views/form/form_controller";
import { useEffect } from "@odoo/owl";

const FILTER_FIELDS = [
    "filter_text",
    "filter_qty_field",
    "filter_qty_min",
    "filter_qty_max",
    "filter_only_actionable",
    "filter_only_selected",
];

export class SaleReturnWizardFormController extends FormController {
    setup() {
        super.setup();
        this._filterKey = null;
        this._filtering = false;
        useEffect(
            (key) => {
                if (key === null) {
                    return; // record not loaded yet
                }
                if (this._filterKey === null || this._filtering) {
                    this._filterKey = key; // initial load, or our own reload
                    return;
                }
                if (key !== this._filterKey) {
                    this._filterKey = key;
                    this._applyLiveFilter();
                }
            },
            () => [this._currentFilterKey()]
        );
    }

    _currentFilterKey() {
        const record = this.model.root;
        if (!record || !record.data) {
            return null;
        }
        return JSON.stringify(FILTER_FIELDS.map((name) => record.data[name]));
    }

    async _applyLiveFilter() {
        if (this._filtering) {
            return;
        }
        this._filtering = true;
        try {
            await this.model.root.save({ stayInEdition: true });
        } finally {
            this._filtering = false;
            this._filterKey = this._currentFilterKey();
        }
    }
}

registry.category("views").add("sale_return_wizard_form", {
    ...formView,
    Controller: SaleReturnWizardFormController,
});
