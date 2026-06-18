# pylint: disable=pointless-statement
{
    "name": "WF Panel Manufacturing",
    "version": "18.0.5.0.0",
    "license": "AGPL-3",
    "summary": "Generar ordenes de fabricacion para paneles importados",
    "category": "Manufacturing",
    "depends": ["WF_panel_importer", "mrp", "product_variant_measure", "model_viewer_widget"],
    "data": ["security/ir.model.access.csv", "views/panel_manufacturing_views.xml", "views/mrp_workorder_views.xml", "views/res_config_settings_views.xml", "views/product_product_views.xml", "views/product_template_views.xml", "views/panel_mrp_dictionary_wizard_views.xml", "views/panel_mrp_mo_creator_button_views.xml"],
    "installable": True,
    "application": False
}
