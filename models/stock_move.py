from odoo import fields, models


class StockMove(models.Model):
    _inherit = "stock.move"

    wf_panel_component_id = fields.Many2one(
        "wf.panel.component",
        string="WF Panel Component",
        copy=False,
        index=True,
    )
