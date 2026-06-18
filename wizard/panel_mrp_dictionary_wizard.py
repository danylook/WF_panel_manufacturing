# -*- coding: utf-8 -*-
from odoo import _, fields, models


class PanelMrpDictionaryWizardMfg(models.TransientModel):
    """Extends the importer's cut-dictionary wizard with a legacy text display
    used by the 'Diccionario de piezas' button in the manufacturing addon."""
    _inherit = 'panel.mrp.dictionary.wizard'

    dictionary_text = fields.Text(string='Diccionario de piezas (texto)', readonly=True)

    def action_panel_mrp_dictionary_wizard(self):
        """Open the wizard and populate the text dictionary using keyword search."""
        section = self._get_active_section()
        if not section or not section.exists():
            raise Exception('No se encontró la sección activa.')

        lines = self._build_dictionary_lines(section)
        self.dictionary_text = '\n'.join(lines)

        return {
            'type': 'ir.actions.act_window',
            'res_model': 'panel.mrp.dictionary.wizard',
            'view_mode': 'form',
            'res_id': self.id,
            'target': 'new',
        }

    def _get_active_section(self):
        ctx = self.env.context
        active_model = ctx.get('active_model')
        active_ids = ctx.get('active_ids') or []
        if active_model == 'wf.panel.section' and len(active_ids) == 1:
            return self.env['wf.panel.section'].browse(active_ids[0])
        active_id = ctx.get('active_id')
        if active_id:
            return self.env['wf.panel.section'].browse(active_id)
        return None

    def _build_dictionary_lines(self, section):
        """Build text lines for the dictionary using keyword-based product search."""
        wizard = self.env['wf.panel.mrp.wizard'].with_context(
            active_model='wf.panel.section',
            active_ids=[section.id],
        ).create({'section_id': section.id})

        lines = [
            "{} | {} | {} | {} | {}".format(
                _("Pieza"), _("L x W x D"), _("Producto encontrado"), _("Largo producto"), _("Estado")
            )
        ]
        try:
            for component in section.component_ids:
                target_length = getattr(component, 'data_length', 0.0) or 0.0
                data_w = getattr(component, 'data_width', 0.0) or 0.0
                data_d = getattr(component, 'data_depth', 0.0) or 0.0
                variant, reason = wizard._match_component_to_variant(component)
                if variant:
                    stock_len = wizard._parse_product_name_length_inches(variant.name or '') or 0.0
                    status = "OK — corte {:.3g}in de {:.3g}in".format(target_length, stock_len)
                    prod_name = variant.display_name or variant.name or ''
                else:
                    status = reason or _("No encontrado")
                    prod_name = ''
                    stock_len = 0.0
                lines.append("{} | {:.3g}x{:.3g}x{:.3g} | {} | {:.3g} | {}".format(
                    component.data_id or component.display_name or '',
                    target_length, data_w, data_d,
                    prod_name, stock_len, status,
                ))
        finally:
            wizard.unlink()

        return lines
