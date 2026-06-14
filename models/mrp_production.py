from collections import defaultdict

from odoo import _, fields, models
from odoo.tools import float_round


class MrpProduction(models.Model):
    _inherit = "mrp.production"

    panel_section_id = fields.Many2one(
        "wf.panel.section",
        string="Sección WF",
        index=True,
        copy=False,
    )
    wf_is_assembly_mo = fields.Boolean(
        string="Es MO de ensamblaje WF",
        default=False,
        copy=False,
    )
    wf_cut_production_id = fields.Many2one(
        "mrp.production",
        string="MO de corte WF",
        ondelete="set null",
        copy=False,
    )

    def _link_bom(self, bom):
        self.ensure_one()
        product_qty = self.product_qty
        uom = self.product_uom_id
        moves_to_unlink = self.env["stock.move"]
        workorders_to_unlink = self.env["mrp.workorder"]
        if self.state == "draft" and self.bom_id == bom:
            self.bom_id = False
        if self.state in ["cancel", "done", "draft"]:
            if self.state == "draft":
                moves_to_unlink = self.move_raw_ids
                workorders_to_unlink = self.workorder_ids
            self.bom_id = bom
            moves_to_unlink.unlink()
            workorders_to_unlink.unlink()
            if self.state == "draft":
                self.write({"product_qty": product_qty, "product_uom_id": uom.id})
            return

        def operation_key_values(record):
            return tuple(record[key] for key in ("company_id", "name", "workcenter_id"))

        def filter_by_attributes(record, product=self.product_id):
            product_attribute_ids = product.product_template_attribute_value_ids.ids
            return (
                not record.bom_product_template_attribute_value_ids
                or any(att_val.id in product_attribute_ids for att_val in record.bom_product_template_attribute_value_ids)
            )

        ratio = self._get_ratio_between_mo_and_bom_quantities(bom)
        _dummy, bom_lines = bom.explode(self.product_id, bom.product_qty)
        bom_lines_by_id = defaultdict(lambda: [None, 0])
        for line, exploded_values in bom_lines:
            if filter_by_attributes(line, exploded_values["product"]):
                key = (line.id, line.product_id.id)
                bom_lines_by_id[key][0] = line
                bom_lines_by_id[key][1] += exploded_values["qty"] / exploded_values["original_qty"]
        bom_byproducts_by_id = {
            byproduct.id: byproduct for byproduct in bom.byproduct_ids.filtered(filter_by_attributes)
        }
        operations_by_id = {
            operation.id: operation for operation in bom.operation_ids.filtered(filter_by_attributes)
        }

        for workorder in self.workorder_ids:
            operation = operations_by_id.pop(workorder.operation_id.id, False)
            if not operation:
                for operation_id in operations_by_id:
                    _operation = operations_by_id[operation_id]
                    if operation_key_values(_operation) == operation_key_values(workorder):
                        operation = operations_by_id.pop(operation_id)
                        break
            if operation and workorder.operation_id != operation:
                workorder.operation_id = operation
            elif operation and workorder.operation_id == operation:
                if workorder.workcenter_id != operation.workcenter_id:
                    workorder.workcenter_id = operation.workcenter_id
                if workorder.name != operation.name:
                    workorder.name = operation.name
            elif workorder.operation_id and workorder.operation_id not in operations_by_id:
                workorders_to_unlink |= workorder

        workorders_values = []
        for operation in operations_by_id.values():
            workorder_vals = {
                "name": operation.name,
                "operation_id": operation.id,
                "product_uom_id": self.product_uom_id.id,
                "production_id": self.id,
                "state": "pending",
                "workcenter_id": operation.workcenter_id.id,
            }
            workorders_values.append(workorder_vals)
        self.workorder_ids += self.env["mrp.workorder"].create(workorders_values)

        for move_raw in self.move_raw_ids:
            if move_raw.wf_panel_component_id:
                if self._wf_panel_align_component_move(move_raw, bom_lines_by_id, ratio):
                    continue
            bom_line, bom_qty = bom_lines_by_id.pop((move_raw.bom_line_id.id, move_raw.product_id.id), (False, None))
            if not bom_line:
                for _bom_line, _bom_qty in bom_lines_by_id.values():
                    if move_raw.product_id == _bom_line.product_id:
                        bom_line, bom_qty = bom_lines_by_id.pop((_bom_line.id, move_raw.product_id.id))
                        if bom_line:
                            break
            move_raw_qty = bom_line and move_raw.product_uom._compute_quantity(
                move_raw.product_uom_qty * ratio, bom_line.product_uom_id
            )
            if bom_line and (
                not move_raw.bom_line_id
                or move_raw.bom_line_id.bom_id != bom
                or move_raw.operation_id != bom_line.operation_id
                or bom_line.product_qty != move_raw_qty
            ):
                move_raw.bom_line_id = bom_line
                move_raw.product_id = bom_line.product_id
                move_raw.product_uom_qty = bom_qty / ratio
                move_raw.product_uom = bom_line.product_uom_id
                if move_raw.operation_id != bom_line.operation_id:
                    move_raw.operation_id = bom_line.operation_id
                    move_raw.workorder_id = self.workorder_ids.filtered(
                        lambda wo: wo.operation_id == move_raw.operation_id
                    )
            elif not bom_line:
                moves_to_unlink |= move_raw

        raw_moves_values = []
        for bom_line, bom_qty in bom_lines_by_id.values():
            raw_move_vals = self._get_move_raw_values(
                bom_line.product_id,
                bom_qty / ratio,
                bom_line.product_uom_id,
                bom_line=bom_line,
            )
            raw_moves_values.append(raw_move_vals)
        self.env["stock.move"].create(raw_moves_values)

        for move_byproduct in self.move_byproduct_ids:
            bom_byproduct = bom_byproducts_by_id.pop(move_byproduct.byproduct_id.id, False)
            if not bom_byproduct:
                for _bom_byproduct in bom_byproducts_by_id.values():
                    if move_byproduct.product_id == _bom_byproduct.product_id:
                        bom_byproduct = bom_byproducts_by_id.pop(_bom_byproduct.id)
                        break
            move_byproduct_qty = bom_byproduct and move_byproduct.product_uom._compute_quantity(
                move_byproduct.product_uom_qty * ratio, bom_byproduct.product_uom_id
            )
            if bom_byproduct and (
                not move_byproduct.byproduct_id
                or bom_byproduct.product_id != move_byproduct.product_id
                or bom_byproduct.product_qty != move_byproduct_qty
            ):
                move_byproduct.byproduct_id = bom_byproduct
                move_byproduct.cost_share = bom_byproduct.cost_share
                move_byproduct.product_uom_qty = bom_byproduct.product_qty / ratio
                move_byproduct.product_uom = bom_byproduct.product_uom_id
            elif not bom_byproduct:
                moves_to_unlink |= move_byproduct

        byproduct_values = []
        for bom_byproduct in bom_byproducts_by_id.values():
            qty = bom_byproduct.product_qty / ratio
            move_byproduct_vals = self._get_move_finished_values(
                bom_byproduct.product_id.id,
                qty,
                bom_byproduct.product_uom_id.id,
                bom_byproduct.operation_id.id,
                bom_byproduct.id,
                bom_byproduct.cost_share,
            )
            byproduct_values.append(move_byproduct_vals)
        self.move_finished_ids += self.env["stock.move"].create(byproduct_values)

        if self.warehouse_id.manufacture_steps in ("pbm", "pbm_sam"):
            moves_to_unlink.product_uom_qty = 0
        moves_to_unlink._action_cancel()
        moves_to_unlink.unlink()
        workorders_to_unlink.unlink()
        self.bom_id = bom

    def _wf_panel_align_component_move(self, move_raw, bom_lines_by_id, ratio):
        if not bom_lines_by_id:
            return False
        for key, (bom_line, bom_qty) in list(bom_lines_by_id.items()):
            if not bom_line or bom_line.product_id != move_raw.product_id:
                continue
            converted_qty = move_raw.product_uom._compute_quantity(
                move_raw.product_uom_qty * ratio,
                bom_line.product_uom_id,
            )
            rounding = bom_line.product_uom_id.rounding or 0.0001
            remaining = float_round(bom_qty - converted_qty, precision_rounding=rounding)
            move_raw.bom_line_id = bom_line
            if not move_raw.operation_id and bom_line.operation_id:
                move_raw.operation_id = bom_line.operation_id
                move_raw.workorder_id = self.workorder_ids.filtered(
                    lambda wo: wo.operation_id == move_raw.operation_id
                )[:1]
            if remaining <= float_round(0.0, precision_rounding=rounding):
                bom_lines_by_id.pop(key, None)
            else:
                bom_lines_by_id[key][1] = remaining
            return True
        return False

    def write(self, vals):
        result = super().write(vals)
        if vals.get("state") == "done":
            for production in self:
                if production.panel_section_id and not production.wf_is_assembly_mo:
                    existing = self.env["mrp.production"].search(
                        [
                            ("panel_section_id", "=", production.panel_section_id.id),
                            ("wf_is_assembly_mo", "=", True),
                        ],
                        limit=1,
                    )
                    if not existing:
                        production._wf_create_assembly_mo()
        return result

    def _wf_build_piece_position_table(self, section):
        """Return a plain-text table of piece positions (X/Y top-left) for the assembly WO."""
        components = section.component_ids.sorted(lambda c: (c.sequence, c.id))
        if not components:
            return ""
        header = _("Posiciones de piezas — %s") % section.display_name
        col_w = [40, 10, 10, 10, 10, 10]
        titles = [_("Pieza"), _("X"), _("Y"), _("Largo"), _("Ancho"), _("Prof")]
        sep = "  ".join("-" * w for w in col_w)
        hdr_row = "  ".join(t.ljust(col_w[i]) for i, t in enumerate(titles))
        lines = [header, sep, hdr_row, sep]
        for comp in components:
            row = [
                (comp.data_id or "")[:col_w[0]].ljust(col_w[0]),
                f"{comp.x:.3f}".rjust(col_w[1]),
                f"{comp.y:.3f}".rjust(col_w[2]),
                f"{comp.data_length:.3f}".rjust(col_w[3]),
                f"{comp.data_width:.3f}".rjust(col_w[4]),
                f"{comp.data_depth:.3f}".rjust(col_w[5]),
            ]
            lines.append("  ".join(row))
        return "\n".join(lines)

    def _wf_create_assembly_mo(self):
        """Auto-create an Assembly MO when this cut MO is marked Done."""
        self.ensure_one()
        section = self.panel_section_id
        if not section:
            return None
        Workcenter = self.env["mrp.workcenter"].sudo()
        assembly_wc = Workcenter.search([("name", "=", "Assembly Center")], limit=1)
        if not assembly_wc:
            assembly_wc = Workcenter.create({"name": "Assembly Center"})
        assembly_mo = self.env["mrp.production"].sudo().with_context(mail_notrack=True).create({
            "product_id": self.product_id.id,
            "product_qty": self.product_qty,
            "product_uom_id": self.product_uom_id.id,
            "picking_type_id": self.picking_type_id.id,
            "panel_section_id": section.id,
            "wf_is_assembly_mo": True,
            "wf_cut_production_id": self.id,
            "origin": self.name,
            "project_id": False,
        })
        piece_table = self._wf_build_piece_position_table(section)
        self.env["mrp.workorder"].sudo().create({
            "name": _("Ensamblaje \u2014 %s") % section.display_name,
            "production_id": assembly_mo.id,
            "workcenter_id": assembly_wc.id,
            "qty_production": assembly_mo.product_qty,
            "state": "ready",
            "product_uom_id": assembly_mo.product_uom_id.id if assembly_mo.product_uom_id else False,
            "wf_instruction": _("Ensamblar el panel %s.") % section.display_name + ("\n\n" + piece_table if piece_table else ""),
        })
        assembly_mo.action_confirm()
        assembly_mo.message_post(
            body=_("MO de ensamblaje creada autom\u00e1ticamente desde %s.") % self.name,
            subtype_xmlid="mail.mt_note",
        )
        self.message_post(
            body=_("MO de ensamblaje creada: %s") % assembly_mo.name,
            subtype_xmlid="mail.mt_note",
        )
        return assembly_mo
