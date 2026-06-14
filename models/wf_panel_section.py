from odoo import _, fields, models
from odoo.exceptions import UserError


class WFPanelSection(models.Model):
    def action_create_panel_mo(self):
        self.ensure_one()
        wizard = self.env['panel.mrp.mo.creator.wizard'].create({})
        return wizard.action_create_panel_mo()
    _inherit = "wf.panel.section"

    production_id = fields.Many2one(
        "mrp.production",
        string="Orden de fabricación",
        copy=False,
        readonly=True,
    )

    manufactured_product_id = fields.Many2one(
        "product.product",
        string="Producto fabricado",
        copy=False,
        index=True,
        help="Producto terminado específico para esta sección del panel.",
    )

    manufactured_bom_id = fields.Many2one(
        "mrp.bom",
        string="Lista de materiales",
        copy=False,
        index=True,
        help="Lista de materiales asociada al producto fabricado de esta sección.",
    )

    def action_prepare_manufacturing_profile(self):
        self.ensure_one()
        aggregated, missing = self.project_id.ensure_manufacturing_profile(self)
        if not aggregated and not missing:
            raise UserError(_("No se detectaron componentes para generar la lista de materiales."))
        message = _("Producto y lista de materiales generados correctamente.")
        notif_type = "success"
        if missing:
            message = _(
                "Se generó la lista de materiales, pero faltan coincidencias:\n%s"
            ) % "\n".join(missing)
            notif_type = "warning"
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Configuración de fabricación"),
                "message": message,
                "type": notif_type,
                "sticky": bool(missing),
            },
        }
