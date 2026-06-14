from odoo import fields, models  # pylint: disable=import-error,no-name-in-module


class MrpWorkorder(models.Model):
    _inherit = "mrp.workorder"

    wf_instruction = fields.Text(
        string="Instrucciones WF",
        copy=False,
    )
    wf_planned_duration_min = fields.Float(
        string="Duración planificada WF (min)",
        copy=False,
    )
    company_currency_id = fields.Many2one(
        related="company_id.currency_id",
        string="Moneda",
        readonly=True,
        store=True,
    )
    wf_planned_cost = fields.Monetary(
        string="Costo planificado WF",
        currency_field="company_currency_id",
        copy=False,
    )
