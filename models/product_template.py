from odoo import fields, models


class WfProductStockLength(models.Model):
    _name = 'wf.product.stock.length'
    _description = 'WF Panel Stock Length'
    _order = 'length_in'

    product_tmpl_id = fields.Many2one(
        'product.template',
        string='Product',
        required=True,
        ondelete='cascade',
        index=True,
    )
    length_in = fields.Float(string='Length (in)', required=True, digits=(10, 3))

    _sql_constraints = [
        ('unique_tmpl_length', 'UNIQUE(product_tmpl_id, length_in)',
         'Each length must be unique per product.'),
    ]


class ProductTemplateWf(models.Model):
    _inherit = 'product.template'

    wf_stock_length_ids = fields.One2many(
        'wf.product.stock.length',
        'product_tmpl_id',
        string='Available Stock Lengths (WF Panel)',
    )
