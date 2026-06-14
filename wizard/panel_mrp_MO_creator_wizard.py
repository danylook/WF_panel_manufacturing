# -*- coding: utf-8 -*-
from odoo import models, api, fields, _

class PanelMrpMOCreatorWizard(models.TransientModel):
    _name = 'panel.mrp.mo.creator.wizard'
    _description = 'Panel MRP Creador de MO y BOM'

    result_text = fields.Text(string='Resultado', readonly=True)


    def get_section_and_project(self):
        active_model = self.env.context.get('active_model')
        active_ids = self.env.context.get('active_ids') or []
        section = None
        if active_model == 'wf.panel.section' and len(active_ids) == 1:
            section = self.env['wf.panel.section'].browse(active_ids[0])
        elif self.env.context.get('active_id'):
            section = self.env['wf.panel.section'].browse(self.env.context['active_id'])
        if not section or not section.exists():
            raise Exception('No se encontró la sección activa.')
        panel = section.project_id
        if not panel or not panel.exists():
            raise Exception('No se encontró el panel asociado.')
        project = panel if panel and panel.exists() else None
        if not project:
            project = self.env['project.project'].create({'name': section.name})
        return section, project

    def get_or_create_panel_product(self, nombre_panel):
        panel_product = self.env['product.product'].search([('name', '=', nombre_panel)], limit=1)
        panel_vals = {
            'name': nombre_panel,
            'type': 'consu',
            'sale_ok': False,
            'purchase_ok': False,
        }
        if not panel_product:
            print(f"[Panel] Producto NO existe, se crea: {nombre_panel}")
            print(f"[Panel] panel_vals: {panel_vals}")
            ProductTemplate = self.env["product.template"].sudo()
            product_template = ProductTemplate.create({
                "name": nombre_panel,
                "type": "consu",
                "sale_ok": False,
                "purchase_ok": False,
                "categ_id": self.env.ref("product.product_category_all").id,
                "uom_id": self.env.ref("uom.product_uom_unit").id,
                "uom_po_id": self.env.ref("uom.product_uom_unit").id,
            })
            panel_product = product_template.product_variant_id
        else:
            print(f"[Panel] Producto YA existe: {nombre_panel}")
            panel_product.write(panel_vals)
        # Limpieza forzada de campos en producto y template
        panel_product.sudo().write({'project_id': False, 'project_template_id': False})
        if panel_product.product_tmpl_id:
            panel_product.product_tmpl_id.sudo().write({'project_id': False, 'project_template_id': False})
        return panel_product

    def get_or_create_bom(self, panel_product):
        bom = self.env['mrp.bom'].search([
            ('product_tmpl_id', '=', panel_product.product_tmpl_id.id),
            ('type', '=', 'normal')
        ], limit=1)
        if bom:
            print(f"[BOM] Ya existe BOM para el template: {panel_product.product_tmpl_id.id}, BOM id: {bom.id}")
        else:
            bom = self.env['mrp.bom'].create({
                'product_tmpl_id': panel_product.product_tmpl_id.id,
                'type': 'normal',
                'product_qty': 1,
            })
            print(f"[BOM] BOM creada para el template: {panel_product.product_tmpl_id.id}, BOM id: {bom.id}")
        return bom

    def create_panel_pieces_and_bom_lines(self, piezas_dicc, bom, section, project):
        resultado = []
        for nombre, datos in piezas_dicc.items():
            pieza_nombre = datos.get('producto_largo_nombre', '') or nombre
            # Buscar producto base
            producto_base = self.env['product.product'].search([('name', '=', pieza_nombre)], limit=1)
            if not producto_base:
                raise Exception(f"No existe el producto base: {pieza_nombre}")
            # Construir domain con todos los atributos relevantes
            domain = [('product_tmpl_id', '=', producto_base.product_tmpl_id.id)]
            # Buscar valores de atributos Odoo
            atributos = {
                'producto_largo_valor': 'Length',
                'producto_ancho': 'Width',
            }
            attribute_value_ids = []
            for key, attr_name in atributos.items():
                valor = datos.get(key)
                if valor:
                    attribute = self.env['product.attribute'].search([('name', '=', attr_name)], limit=1)
                    if attribute:
                        value = self.env['product.attribute.value'].search([
                            ('attribute_id', '=', attribute.id),
                            ('name', '=', str(valor))
                        ], limit=1)
                        if value:
                            attribute_value_ids.append(value.id)
            if attribute_value_ids:
                domain.append(('attribute_value_ids', 'in', attribute_value_ids))
            pieza_product = self.env['product.product'].search(domain, limit=1)
            if not pieza_product:
                # Intentar con producto base + ' manufactura'
                manufactura_nombre = f"{pieza_nombre} manufactura"
                producto_base_manufactura = self.env['product.product'].search([('name', '=', manufactura_nombre)], limit=1)
                if producto_base_manufactura:
                    domain_manufactura = [('product_tmpl_id', '=', producto_base_manufactura.product_tmpl_id.id)]
                    for attr in [d for d in domain if d[0] != 'product_tmpl_id']:
                        domain_manufactura.append(attr)
                    pieza_product = self.env['product.product'].search(domain_manufactura, limit=1)
                    if pieza_product:
                        print(f"[VARIANTE] Se encontró variante en producto manufactura: {manufactura_nombre}")
                # Si aún no existe, crear variante bajo producto manufactura
                if not pieza_product:
                    base_tmpl_id = producto_base_manufactura.product_tmpl_id.id if producto_base_manufactura else producto_base.product_tmpl_id.id
                    variante_vals = {
                        'product_tmpl_id': base_tmpl_id,
                        'name': manufactura_nombre,
                        'sale_ok': False,
                        'purchase_ok': False,
                    }
                    for attr in [d for d in domain if d[0] != 'product_tmpl_id']:
                        variante_vals[attr[0]] = attr[2]
                    pieza_product = self.env['product.product'].create(variante_vals)
                    print(f"[VARIANTE] Se crea variante para {manufactura_nombre} con atributos: {str([d for d in domain if d[0] != 'product_tmpl_id'])}")
            pieza_product.sudo().write({'project_id': False, 'project_template_id': False})
            if pieza_product.product_tmpl_id:
                pieza_product.product_tmpl_id.sudo().write({'project_id': False, 'project_template_id': False})
            # Permitir agregar piezas iguales (líneas BOM duplicadas)
            self.env['mrp.bom.line'].create({
                'bom_id': bom.id,
                'product_id': pieza_product.id,
                'product_qty': 1,
            })
            resultado.append(_("Pieza agregada a BOM: %s (atributos: %s)") % (pieza_nombre, str([d for d in domain if d[0] != 'product_tmpl_id'])))
            resto_corte = datos.get('resto_corte', 0)
            try:
                resto_corte_f = float(resto_corte)
            except Exception:
                resto_corte_f = 0
            if resto_corte_f > 0:
                self.create_manufactura_and_recorte_products(pieza_nombre, project, resultado)
        return resultado

    def create_manufactura_and_recorte_products(self, pieza_nombre, project, resultado):
        manufactura_nombre = f"{pieza_nombre} manufactura"
        manufactura_product = self.env['product.product'].search([('name', '=', manufactura_nombre)], limit=1)
        manufactura_vals = {
            'name': manufactura_nombre,
            'type': 'consu',
            'sale_ok': False,
            'purchase_ok': False,
        }
        if not manufactura_product:
            print(f"[Manufactura] Producto NO existe, se crea: {manufactura_nombre}")
            manufactura_product = self.env['product.product'].create(manufactura_vals)
        else:
            print(f"[Manufactura] Producto YA existe: {manufactura_nombre}")
            manufactura_product.write(manufactura_vals)
        manufactura_product.sudo().write({'project_id': False, 'project_template_id': False})
        if manufactura_product.product_tmpl_id:
            manufactura_product.product_tmpl_id.sudo().write({'project_id': False, 'project_template_id': False})
        recorte_nombre = f"{pieza_nombre} recorte"
        recorte_product = self.env['product.product'].search([('name', '=', recorte_nombre)], limit=1)
        recorte_vals = {
            'name': recorte_nombre,
            'type': 'consu',
            'sale_ok': False,
            'purchase_ok': False,
        }
        if not recorte_product:
            print(f"[Recorte] Producto NO existe, se crea: {recorte_nombre}")
            recorte_product = self.env['product.product'].create(recorte_vals)
        else:
            print(f"[Recorte] Producto YA existe: {recorte_nombre}")
            recorte_product.write(recorte_vals)
        recorte_product.sudo().write({'project_id': False, 'project_template_id': False})
        if recorte_product.product_tmpl_id:
            recorte_product.product_tmpl_id.sudo().write({'project_id': False, 'project_template_id': False})
        resultado.append(_("WO corte creada para %s, manufactura: %s, recorte: %s") % (pieza_nombre, manufactura_nombre, recorte_nombre))

    def process_house_panels(self, house):
        resultado = []
        for panel in house.panel_ids:
            nombre_panel = panel.name
            panel_product = panel.product_id or self.env['product.product'].search([('name', '=', nombre_panel)], limit=1)
            if not panel_product:
                panel_product = self.env['product.product'].create({
                    'name': nombre_panel,
                    'type': 'consu',
                    'sale_ok': False,
                    'purchase_ok': False,
                })
                panel.product_id = panel_product.id
            bom = panel.bom_id or self.env['mrp.bom'].search([('product_tmpl_id', '=', panel_product.product_tmpl_id.id)], limit=1)
            if not bom:
                bom = self.env['mrp.bom'].create({
                    'product_tmpl_id': panel_product.product_tmpl_id.id,
                    'type': 'normal',
                    'product_qty': 1,
                })
                panel.bom_id = bom.id
            mo = panel.mo_id or self.env['mrp.production'].create({
                'product_id': panel_product.id,
                'bom_id': bom.id,
                'product_qty': 1,
            })
            panel.mo_id = mo.id
            resultado.append(_("MO creada para panel: %s") % nombre_panel)
            mo._create_workorders()
            for wo in mo.workorder_ids:
                wo.panel_id = panel.id
            resultado.append(_("WO creadas para panel: %s") % nombre_panel)
        return resultado

    def action_create_panel_mo(self):
        active_model = self.env.context.get('active_model')
        active_ids = self.env.context.get('active_ids') or []
        resultado = []
        if active_model == 'project.house' and active_ids:
            house = self.env['project.house'].browse(active_ids[0])
            resultado = self.process_house_panels(house)
        else:
            section, project = self.get_section_and_project()
            # Use the 18.0 matching architecture
            wizard = self.env['wf.panel.mrp.wizard'].with_context(
                active_model='wf.panel.section',
                active_ids=[section.id],
                active_id=section.id,
            ).create({'section_id': section.id})
            try:
                with self.env.cr.savepoint():
                    wizard.action_generate()
                resultado.append(_("Orden de fabricación generada para: %s") % section.display_name)
            except Exception as exc:
                resultado.append(_("Error generando MO para %s: %s") % (section.display_name, exc))
        self.result_text = '\n'.join(resultado)
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'panel.mrp.mo.creator.wizard',
            'view_mode': 'form',
            'res_id': self.id,
            'target': 'new',
        }
