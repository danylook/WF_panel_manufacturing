# -*- coding: utf-8 -*-
from odoo import models, fields, api

class ProjectHouse(models.Model):
    _name = 'project.house'
    _description = 'Proyecto de Casa'

    name = fields.Char(string='Nombre del Proyecto', required=True)
    panel_ids = fields.One2many('project.house.panel', 'house_id', string='Paneles')

class ProjectHousePanel(models.Model):
    _name = 'project.house.panel'
    _description = 'Panel de Casa'

    name = fields.Char(string='Nombre del Panel', required=True)
    house_id = fields.Many2one('project.house', string='Proyecto')
    product_id = fields.Many2one('product.product', string='Producto Base')
    bom_id = fields.Many2one('mrp.bom', string='BOM')
    mo_id = fields.Many2one('mrp.production', string='Manufacturing Order')
    wo_ids = fields.One2many('mrp.workorder', 'panel_id', string='Work Orders')

# Puedes extender mrp.workorder para vincularlo al panel
class MrpWorkorder(models.Model):
    _inherit = 'mrp.workorder'
    panel_id = fields.Many2one('project.house.panel', string='Panel de Casa')
