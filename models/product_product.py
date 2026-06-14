from odoo import fields, models


class ProductProduct(models.Model):
    _inherit = "product.product"

    wf_panel_manufactured_only = fields.Boolean(
        string="WF Panel Manufacturable",
        help="Indicates that the variant is only manufactured for WF panel operations and shouldn't be reused as a raw purchasable component.",
        default=False,
    )

    wf_panel_cut_consumable = fields.Boolean(
        string="WF Panel Cut Consumable",
        help="Flag automatically set on cut pieces generated for a single panel job; they can be archived once consumed.",
        default=False,
    )

    wf_panel_leftover_stock = fields.Boolean(
        string="WF Panel Leftover Stock",
        help="Marks leftovers kept as stockable lengths that remain available for future panels.",
        default=False,
    )
