from odoo import fields, models  # pylint: disable=import-error,no-name-in-module


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    wf_cut_length_minutes_per_piece = fields.Float(
        string="Minutos por corte de largo (WF)",
        config_parameter="wf_panel_manufacturing.cut_length_minutes_per_piece",
        help="Tiempo estimado en minutos para realizar el corte de largo de una pieza.",
    )
    wf_cut_length_setup_minutes = fields.Float(
        string="Minutos de preparación corte de largo (WF)",
        config_parameter="wf_panel_manufacturing.cut_length_setup_minutes",
        help="Tiempo adicional de preparación en minutos para el corte de largo por lote.",
    )
    wf_cut_width_minutes_per_piece = fields.Float(
        string="Minutos por corte de ancho (WF)",
        config_parameter="wf_panel_manufacturing.cut_width_minutes_per_piece",
        help="Tiempo estimado en minutos para ajustar el ancho de una pieza.",
    )
    wf_cut_width_setup_minutes = fields.Float(
        string="Minutos de preparación corte de ancho (WF)",
        config_parameter="wf_panel_manufacturing.cut_width_setup_minutes",
        help="Tiempo adicional de preparación en minutos para el corte de ancho por lote.",
    )
