import difflib
import logging
import re
from fractions import Fraction

from markupsafe import Markup
from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools import float_round

from .cut_optimizer import optimize_with_leftover_chain, MIN_LEFTOVER_INCHES


_logger = logging.getLogger(__name__)
_CUT_USAGE_CACHE = {}
_COMPONENT_VARIANT_CACHE = {}
_ASSEMBLY_VARIANT_CACHE = {}
_CUT_CONSUMABLE_VARIANTS = {}
_INTERMEDIATE_VARIANT_CACHE = {}
_WIDTH_CUT_COMPONENTS = {}
_COMPONENT_STOCK_LENGTH_CACHE = {}  # component.id → stock_length_in (float)
_CORE_MIN_MATCH = 10


class WFPanelManufacturingWizard(models.TransientModel):
    def _get_variant_width(self, variant, attribute):
        # Extract the numeric width magnitude for a given variant.
        if not variant or not attribute:
            return None
        ptav = variant.product_template_attribute_value_ids.filtered(
            lambda record: record.attribute_id == attribute
        )[:1]
        if not ptav:
            return None
        return self._extract_magnitude(ptav.product_attribute_value_id)
    _name = "wf.panel.mrp.wizard"
    _description = "Generar orden de fabricación para panel WF"

    _color_reset = "\033[0m"
    _color_codes = {
        "info": "\033[36m",      # cyan
        "success": "\033[32m",   # green
        "warning": "\033[33m",   # yellow
        "error": "\033[31m",     # red
        "detail": "\033[35m",    # magenta
    }

    section_id = fields.Many2one(
        "wf.panel.section",
        string="Panel",
        required=True,
    )
    auto_assign = fields.Boolean(
        string="Reservar materiales",
        help="Si se marca, la orden reservará stock inmediatamente tras crear los movimientos de materiales.",
    )
    auto_plan = fields.Boolean(
        string="Planificar operaciones",
        help="Programa las órdenes de trabajo tras confirmar la orden de fabricación.",
    )
    result_production_id = fields.Many2one(
        "mrp.production",
        string="Orden creada",
        readonly=True,
    )
    missing_component_info = fields.Text(
        string="Componentes sin coincidencia",
        readonly=True,
    )

    _component_attribute_map = (
        ("data_length", ("Length", "Largo")),
        ("data_width", ("Width", "Ancho")),
        ("data_depth", ("Thickness", "Espesor")),
    )
    _component_code_fields = ("data_id",)
    _base_length_uom_xmlid = "uom.product_uom_inch"
    _value_tolerance = 0.1
    _cuttable_fields = {"data_length", "data_width"}
    _component_stop_tokens = frozenset()

    @api.model
    def default_get(self, fields_list):
        # Prefill wizard defaults based on active panel section context.
        defaults = super().default_get(fields_list)
        if "section_id" in fields_list and not defaults.get("section_id"):
            active_model = self.env.context.get("active_model")
            active_ids = self.env.context.get("active_ids") or []
            if active_model == "wf.panel.section" and len(active_ids) == 1:
                defaults["section_id"] = active_ids[0]
        return defaults

    def action_generate(self):
        # Drive the complete MO creation flow for the selected panel section.
        self.ensure_one()
        section = self.section_id
        if not section:
            raise UserError(_("Seleccione un panel."))
        if section.production_id:
            raise UserError(
                _("El panel %s ya tiene una orden de fabricación asociada.") % section.display_name
            )
        panel = section.project_id
        if not panel:
            raise UserError(_("El panel no está vinculado a un proyecto."))

        final_product_label = self._build_final_product_label(panel, section)
        cut_notes_holder = []
        ctx = dict(self.env.context or {})
        ctx["_cut_notes_holder"] = cut_notes_holder
        ctx["_wf_panel_mrp_wizard_id"] = self.id
        aggregated, missing = panel.with_context(ctx).ensure_manufacturing_profile(section)
        # Capture matching data immediately — before any downstream code can alter the cache
        _component_map = dict(self._get_component_variant_map())
        _stock_length_map = dict(self._get_component_stock_length_map())
        _logger.info("WF cut list capture: wizard=%s components=%d stock_lens=%d", self.id, len(_component_map), len(_stock_length_map))
        print("[WF DEBUG] cut list capture: wizard=%s components=%d stock_lens=%d" % (self.id, len(_component_map), len(_stock_length_map)))

        bom = section.manufactured_bom_id or panel.bom_id
        if not bom:
            raise UserError(
                _("No se pudo generar la lista de materiales para la sección %s.") % section.display_name
            )
        # Crear un producto único para el panel si no existe
        product_template = None
        product = section.manufactured_product_id
        if not product:
            ProductTemplate = self.env["product.template"].sudo()
            panel_name = self._build_final_product_label(panel, section)
            product_template = ProductTemplate.create({
                "name": panel_name,
                "type": "consu",
                "sale_ok": False,
                "purchase_ok": False,
                "categ_id": self.env.ref("product.product_category_all").id,
                "uom_id": self.env.ref("uom.product_uom_unit").id,
                "uom_po_id": self.env.ref("uom.product_uom_unit").id,
            })
            section.manufactured_product_id = product_template.id
            product = product_template
        # Si no existe el producto, error
        if not product:
            raise UserError(
                _("La sección %s no tiene un producto fabricado asociado.") % section.display_name
            )

        if not aggregated:
            detail = "\n".join(missing)
            raise UserError(
                _("No se encontraron productos coincidentes para los componentes del panel.\n%s") % detail
                if detail
                else _("No se encontraron productos coincidentes para los componentes del panel.")
            )

        has_description_field = "product_description_mrp" in self.env["mrp.production"]._fields

        # Clear stale project_id on the BOM — project_mrp copies bom.project_id to
        # mrp.production via a stored compute, causing FK violations if that project
        # no longer exists.
        if bom.project_id:
            bom.sudo().write({"project_id": False})

        production_vals = {
            "product_id": product.product_variant_id.id,
            "product_qty": 1.0,
            "product_uom_id": product.uom_id.id,
            "bom_id": bom.id,
            "origin": f"{panel.name or panel.project or panel.id}/{section.name}",
            "panel_section_id": section.id,
            "project_id": False,
        }
        if has_description_field:
            production_vals["product_description_mrp"] = final_product_label
        production = self.env["mrp.production"].create(production_vals)

        production.action_confirm()
        if has_description_field:
            self._apply_final_product_label(production, final_product_label)
        if production.move_raw_ids:
            production.move_raw_ids.unlink()
            # After unlink, clear any cached or referenced move records
            production.move_raw_ids = self.env["stock.move"].browse()
            # Also clear cut_notes_holder to avoid referencing deleted moves
            cut_notes_holder = []

        # ── Optimización de cortes y creación de variantes ──────────────────────
        moves_to_create, leftover_vals = self._prepare_raw_moves_optimized(
            production, _component_map, _stock_length_map
        )
        moves = self.env["stock.move"]
        workorder_meta = None
        if moves_to_create:
            moves = self.env["stock.move"].create(moves_to_create)

            # ── Sincronizar BOM lines con los raw moves optimizados ──
            # Las BOM lines se crearon antes de la optimización (ensure_manufacturing_profile)
            # y tienen 7 líneas de 2x4. Debemos reemplazarlas con 5 líneas (una por plan).
            if bom and moves:
                old_lines = bom.bom_line_ids
                if old_lines:
                    old_lines.unlink()
                for move in moves:
                    if move.raw_material_production_id.id != production.id:
                        continue
                    bom_line_vals = {
                        "bom_id": bom.id,
                        "product_id": move.product_id.id,
                        "product_qty": move.product_uom_qty,
                        "product_uom_id": move.product_uom.id,
                        "sequence": move.sequence or 10,
                    }
                    self.env["mrp.bom.line"].sudo().create(bom_line_vals)

            # workorder_meta debe estar definido antes de usarse
            if 'workorder_meta' in locals():
                if workorder_meta:
                    # Asociar movimientos de corte de largo
                    cut_workorder = workorder_meta.get("cut_workorder")
                    width_cut_workorder = workorder_meta.get("width_cut_workorder")
                    assembly_workorder = workorder_meta.get("assembly_workorder")
                    width_components = set(workorder_meta.get("width_cut_components") or [])
                    single_components = set(workorder_meta.get("single_cut_components") or [])
                    # Asignar workorder_id a los movimientos según el tipo de corte
                    for move in moves:
                        comp_id = getattr(move, "wf_panel_component_id", False)
                        if comp_id:
                            if comp_id.id in width_components and width_cut_workorder:
                                move.write({"workorder_id": width_cut_workorder.id})
                            elif comp_id.id in single_components and cut_workorder:
                                move.write({"workorder_id": cut_workorder.id})
                            elif assembly_workorder:
                                move.write({"workorder_id": assembly_workorder.id})
                    # Asociar operación a las líneas del BOM
                    bom_lines = bom.bom_line_ids
                    for line in bom_lines:
                        comp_id = getattr(line, "wf_panel_component_id", False)
                        if comp_id:
                            if comp_id.id in width_components and width_cut_workorder and width_cut_workorder.operation_id:
                                line.write({"operation_id": width_cut_workorder.operation_id.id})
                            elif comp_id.id in single_components and cut_workorder and cut_workorder.operation_id:
                                line.write({"operation_id": cut_workorder.operation_id.id})
                            elif assembly_workorder and assembly_workorder.operation_id:
                                line.write({"operation_id": assembly_workorder.operation_id.id})
        if moves:
            moves._action_confirm()
            # Restore cut descriptions — _action_confirm() overwrites name with the MO reference
            for move in moves:
                desc = move.description_picking
                if desc and desc != move.name:
                    move.write({"name": desc})
            if self.auto_assign:
                moves._action_assign()

        # Crear movimientos de subproducto para restos aprovechables
        leftover_moves = self.env["stock.move"]
        if leftover_vals:
            leftover_moves = self.env["stock.move"].create(leftover_vals)
            leftover_moves._action_confirm()
            leftover_moves_message = self._lang_choice(
                "WF panel restos aprovechables creados → {ids}",
                "WF panel reusable leftovers created → {ids}",
            ).format(ids=leftover_moves.ids)
            self._print_colored(leftover_moves_message, tone="detail")

        # También crear leftovers legacy desde cut_notes_holder
        legacy_leftovers = self._create_leftover_moves(production, cut_notes_holder)
        if legacy_leftovers:
            legacy_message = self._lang_choice(
                "WF panel subproductos legacy creados → {ids}",
                "WF panel legacy byproducts created → {ids}",
            ).format(ids=legacy_leftovers.ids)
            self._print_colored(legacy_message, tone="detail")

        if cut_notes_holder:
            note_message = self._lang_choice(
                "WF panel notas de corte recopiladas ({count}) → {notes}",
                "WF panel cut notes gathered ({count}) → {notes}",
            ).format(count=len(cut_notes_holder), notes=cut_notes_holder)
            self._print_colored(note_message, tone="detail")
        else:
            empty_message = self._lang_choice(
                "WF panel notas de corte recopiladas → []",
                "WF panel cut notes gathered → []",
            )
            self._print_colored(empty_message, tone="detail")

        width_cut_components = set(self._get_width_cut_components() or [])
        single_cut_components = set()
        for entry in cut_notes_holder or []:
            if not isinstance(entry, dict):
                continue
            if not entry.get("is_cut_instruction"):
                continue
            component_id = entry.get("component_id")
            if not component_id:
                continue
            if component_id in width_cut_components:
                continue
            single_cut_components.add(component_id)
        if not single_cut_components:
            variant_map = self._get_component_variant_map()
            for component_id in variant_map.keys():
                if component_id in width_cut_components:
                    continue
                single_cut_components.add(component_id)
        workorder_meta = self._create_default_workorders(
            production,
            section,
            cut_notes_holder,
            width_components=width_cut_components,
            single_components=single_cut_components,
        )
        self._prepare_cut_piece_transitions(production, workorder_meta, cut_notes_holder)

        if self.auto_plan:
            production.button_plan()

        self._assign_cut_components_to_workorder(production, workorder_meta, cut_notes_holder)
        self._assign_assembly_components_to_workorder(production, workorder_meta)
        self._sync_workorder_instructions(production, workorder_meta)

        section.production_id = production
        self.result_production_id = production
        combined_notes = []
        if missing:
            combined_notes.extend(missing)
        if cut_notes_holder:
            combined_notes.extend(
                entry["note"] if isinstance(entry, dict) else str(entry)
                for entry in cut_notes_holder
                if entry
            )
        if combined_notes:
            self.missing_component_info = "\n".join(combined_notes)

        self._post_cut_list_to_mo(production, _component_map, _stock_length_map)

        action = self.env.ref("mrp.mrp_production_action").read()[0]
        action.update({
            "res_id": production.id,
            "view_mode": "form",
            "views": [(False, "form")],
        })
        self._archive_consumed_cut_variants()
        self._clear_cut_usage_cache()
        self._clear_component_variant_map()
        self._clear_component_assembly_variant_map()
        self._clear_component_intermediate_variant_map()
        self._clear_width_cut_components()
        return action

    def unlink(self):
        # Clean cached matching data when the wizard records go away.
        for wizard in self:
            try:
                wizard._clear_cut_usage_cache()
                wizard._clear_component_variant_map()
                wizard._clear_component_assembly_variant_map()
                wizard._clear_cut_consumable_variants()
                wizard._clear_component_intermediate_variant_map()
                wizard._clear_width_cut_components()
            except Exception:
                continue
        return super().unlink()

    @staticmethod
    def _compute_stage_minutes(component_count, minutes_per_piece, setup_minutes):
        # Estimate stage duration in minutes with per-piece and setup contributions.
        if not component_count:
            return 0.0
        minutes = 0.0
        if minutes_per_piece:
            minutes = max(component_count * minutes_per_piece, 0.0)
        if setup_minutes:
            minutes += max(setup_minutes, 0.0)
        return minutes

    def _get_config_float(self, key, default=0.0):
        # Retrieve a float configuration parameter with safe conversion.
        Param = self.env["ir.config_parameter"].sudo()
        raw_value = Param.get_param(key, default)
        try:
            return float(raw_value)
        except (TypeError, ValueError):
            return default

    def _get_cut_timing_config(self):
        # Load configured timing values for cutting stages.
        return {
            "length_per_piece": self._get_config_float(
                "wf_panel_manufacturing.cut_length_minutes_per_piece",
                default=0.0,
            ),
            "length_setup": self._get_config_float(
                "wf_panel_manufacturing.cut_length_setup_minutes",
                default=0.0,
            ),
            "width_per_piece": self._get_config_float(
                "wf_panel_manufacturing.cut_width_minutes_per_piece",
                default=0.0,
            ),
            "width_setup": self._get_config_float(
                "wf_panel_manufacturing.cut_width_setup_minutes",
                default=0.0,
            ),
        }

    @staticmethod
    def _estimate_workorder_cost(minutes, workcenter):
        # Convert planned minutes into a monetary estimate using the workcenter rate.
        if not minutes or not workcenter:
            return 0.0
        hourly_cost = getattr(workcenter, "costs_hour", 0.0) or 0.0
        if not hourly_cost:
            return 0.0
        return (minutes / 60.0) * hourly_cost

    def _create_default_workorders(self, production, section, cut_notes, width_components=None, single_components=None):
        # Create base cut and assembly workorders with separate stages for one-side and two-side cuts.
        Workorder = self.env["mrp.workorder"].sudo()
        Workcenter = self.env["mrp.workcenter"].sudo()
        cut_wc = Workcenter.search([("name", "=", "Cut Center")], limit=1)
        if not cut_wc:
            cut_wc = Workcenter.create({"name": "Cut Center"})
        assembly_wc = Workcenter.search([("name", "=", "Assembly Center")], limit=1)
        if not assembly_wc:
            assembly_wc = Workcenter.create({"name": "Assembly Center"})
        workorders_data = []
        bin_anchor_map = {}
        note_entries = cut_notes or []
        width_components = set(width_components or [])
        single_components = set(single_components or [])

        general_note_lines = []
        component_notes = {}
        for entry in note_entries:
            if isinstance(entry, dict):
                note_text = entry.get("note")
                if not note_text:
                    continue
                component_id = entry.get("component_id")
                if component_id:
                    component_notes.setdefault(component_id, []).append(note_text)
                else:
                    general_note_lines.append(note_text)
            elif entry:
                general_note_lines.append(str(entry))

        def _collect_notes(component_ids):
            lines = list(general_note_lines)
            for comp_id in component_ids:
                lines.extend(component_notes.get(comp_id, []))
            return [line for line in lines if line]

        def _build_instruction(component_ids, default_line):
            lines = _collect_notes(component_ids)
            combined = "\n".join(lines)
            if combined:
                return f"{default_line}\n{combined}" if default_line else combined
            return default_line

        cut_instruction = False
        width_instruction = False
        assembly_instruction = False
        assembly_one_instruction = False
        assembly_two_instruction = False

        cut_note_lines = []
        for entry in note_entries:
            if isinstance(entry, dict):
                note_text = entry.get("note")
                if note_text:
                    cut_note_lines.append(note_text)
            elif entry:
                cut_note_lines.append(str(entry))
        cut_note_text = "\n".join(cut_note_lines)

        requires_width_cut = bool(width_components)
        length_components = set(width_components or set()) | set(single_components or set())
        length_component_count = len(length_components)
        width_component_count = len(width_components)
        cut_timing_config = self._get_cut_timing_config()
        length_minutes = self._compute_stage_minutes(
            length_component_count,
            cut_timing_config.get("length_per_piece", 0.0),
            cut_timing_config.get("length_setup", 0.0),
        )
        width_minutes = self._compute_stage_minutes(
            width_component_count,
            cut_timing_config.get("width_per_piece", 0.0),
            cut_timing_config.get("width_setup", 0.0),
        ) if requires_width_cut else 0.0
        length_minutes = float_round(length_minutes, precision_digits=2) if length_minutes else 0.0
        width_minutes = float_round(width_minutes, precision_digits=2) if width_minutes else 0.0
        length_stage_key = "cut_one_side" if requires_width_cut else "cut_length"
        length_cost = float_round(
            self._estimate_workorder_cost(length_minutes, cut_wc),
            precision_digits=2,
        ) if length_minutes and cut_wc else 0.0
        width_cost = float_round(
            self._estimate_workorder_cost(width_minutes, cut_wc),
            precision_digits=2,
        ) if width_minutes and cut_wc else 0.0
        stage_timing_payload = {
            length_stage_key: {
                "minutes": length_minutes,
                "cost": length_cost,
            }
        }
        if requires_width_cut:
            stage_timing_payload["cut_two_sides"] = {
                "minutes": width_minutes,
                "cost": width_cost,
            }
        self._print_colored(
            self._lang_choice(
                "WF panel tiempos corte planificados → largo:{length} min ancho:{width} min",
                "WF panel planned cut timings → length:{length} min width:{width} min",
            ).format(
                length=stage_timing_payload.get(length_stage_key, {}).get("minutes", 0.0),
                width=stage_timing_payload.get("cut_two_sides", {}).get("minutes", 0.0),
            ),
            tone="detail",
        )
        if cut_wc and (length_cost or width_cost):
            self._print_colored(
                self._lang_choice(
                    "WF panel costo corte planificado → largo:{length_cost} ancho:{width_cost}",
                    "WF panel planned cutting cost → length:{length_cost} width:{width_cost}",
                ).format(
                    length_cost=stage_timing_payload.get(length_stage_key, {}).get("cost", 0.0),
                    width_cost=stage_timing_payload.get("cut_two_sides", {}).get("cost", 0.0),
                ),
                tone="detail",
            )

        if cut_wc:
            product_uom_id = production.product_uom_id.id if production.product_uom_id else False
            # Create one work order per stock stick (bin from bin packing)
            _comp_map = dict(self._get_component_variant_map())
            _stock_map = self._get_component_stock_length_map()
            BinComp = self.env["wf.panel.component"].sudo()
            BinProd = self.env["product.product"].sudo()
            bin_index = 0
            if _comp_map:
                bin_comps = BinComp.browse(_comp_map.keys()).sorted(key=lambda c: (c.sequence, c.id))
                bin_groups = {}
                for bc in bin_comps:
                    v_id = _comp_map.get(bc.id)
                    if not v_id:
                        continue
                    sl = _stock_map.get(bc.id, 0.0)
                    cl = bc.data_length or 0.0
                    bin_groups.setdefault((v_id, sl), []).append((bc, cl))
                for (v_id, stock_len), items in bin_groups.items():
                    bv = BinProd.browse(v_id)
                    if not bv.exists():
                        continue
                    bins = self._bin_pack_cuts(items, stock_len)
                    per_bin_min = float_round(
                        length_minutes / max(len(bins), 1), precision_digits=2
                    ) if length_minutes else 0.0
                    for bin_cuts in bins:
                        bin_index += 1
                        anchor_comp = bin_cuts[0][0]
                        cut_lines = []
                        for comp, _cl in bin_cuts:
                            dims = []
                            if comp.data_length and comp.data_length > 0:
                                dims.append(f'L={comp.data_length:.3f}"')
                            if comp.data_width and comp.data_width > 0:
                                dims.append(f'W={comp.data_width:.3f}"')
                            if comp.data_depth and comp.data_depth > 0:
                                dims.append(f'D={comp.data_depth:.3f}"')
                            piece_label = comp.data_id or comp.display_name or ""
                            cut_lines.append(
                                f"  \u2022 {piece_label}: {', '.join(dims)}" if dims else f"  \u2022 {piece_label}"
                            )
                        if stock_len:
                            used = sum(cl for _, cl in bin_cuts)
                            waste = stock_len - used
                            bin_instruction = (
                                f'1. Tomar: {bv.display_name} [{stock_len:.0f}"]\n'
                                f"2. Cortar:\n" + "\n".join(cut_lines) +
                                f'\n3. Desperdicio: {waste:.3f}"'
                            )
                            wo_name = f'Pieza #{bin_index}: {bv.display_name} [{stock_len:.0f}"]'
                        else:
                            bin_instruction = (
                                f"1. Tomar: {bv.display_name}\n2. Cortar:\n" +
                                "\n".join(cut_lines)
                            )
                            wo_name = f"Pieza #{bin_index}: {bv.display_name}"
                        if cut_note_text:
                            bin_instruction = f"{bin_instruction}\n{cut_note_text}"
                        if not cut_instruction:
                            cut_instruction = bin_instruction
                        stage_key = f"cut_bin_{bin_index}"
                        bin_anchor_map[anchor_comp.id] = stage_key
                        bin_wo_vals = {
                            "name": wo_name,
                            "production_id": production.id,
                            "workcenter_id": cut_wc.id,
                            "qty_production": production.product_qty,
                            "state": "ready",
                            "product_uom_id": product_uom_id,
                            "duration_expected": per_bin_min,
                            "wf_planned_duration_min": per_bin_min,
                            "wf_planned_cost": float_round(
                                self._estimate_workorder_cost(per_bin_min, cut_wc),
                                precision_digits=2,
                            ),
                        }
                        workorders_data.append({
                            "create_vals": bin_wo_vals,
                            "instruction": bin_instruction,
                            "workcenter_label": cut_wc.display_name or cut_wc.name or "Cut Center",
                            "stage": stage_key,
                            "anchor_comp_id": anchor_comp.id,
                        })
            if not workorders_data:
                # Fallback: no bins computed, create one generic cutting WO
                cut_instruction = cut_note_text or _("Realizar los cortes seg\u00fan especificaci\u00f3n.")
                workorders_data.append({
                    "create_vals": {
                        "name": _("Corte de materiales"),
                        "production_id": production.id,
                        "workcenter_id": cut_wc.id,
                        "qty_production": production.product_qty,
                        "state": "ready",
                        "product_uom_id": product_uom_id,
                        "duration_expected": length_minutes,
                        "wf_planned_duration_min": length_minutes,
                        "wf_planned_cost": length_cost,
                    },
                    "instruction": cut_instruction,
                    "workcenter_label": cut_wc.display_name or cut_wc.name or "Cut Center",
                    "stage": "cut_length",
                })

        product_uom_id = production.product_uom_id.id if production.product_uom_id else False
        assembly_label = section.display_name if section else production.name
        assembly_instruction = _("Ensamblar el panel %s.") % assembly_label
        if section:
            assembly_instruction += "\n\n" + self._wf_build_piece_position_table(section)
        if cut_note_text:
            assembly_instruction = f"{assembly_instruction}\n{_('Notas:')}\n{cut_note_text}"
        workorders_data.append({
            "create_vals": {
                "name": _("Ensamblaje \u2014 %s") % assembly_label,
                "production_id": production.id,
                "workcenter_id": assembly_wc.id,
                "qty_production": production.product_qty,
                "state": "ready",
                "product_uom_id": product_uom_id,
            },
            "instruction": assembly_instruction,
            "workcenter_label": assembly_wc.display_name or assembly_wc.name or "Assembly Center",
            "stage": "assembly",
        })

        created_workorders = self.env["mrp.workorder"].browse()
        stage_map = {}
        if workorders_data:
            self._print_colored(
                f"WF panel workorders payload → {[data['create_vals'] for data in workorders_data]}",
                tone="detail",
            )
            sanitized = [
                {k: v for k, v in data["create_vals"].items() if k != "sequence"}
                for data in workorders_data
            ]
            created_workorders = Workorder.create(sanitized)
            for workorder, data in zip(created_workorders, workorders_data):
                instruction = data.get("instruction")
                if instruction:
                    if "wf_instruction" in workorder._fields:
                        workorder.wf_instruction = instruction
                    self._record_workorder_instruction(workorder, instruction)
                stage = data.get("stage")
                if stage:
                    stage_map[stage] = workorder
                # Link each bin WO to its corresponding raw material move
                anchor_comp_id = data.get("anchor_comp_id")
                if anchor_comp_id:
                    matching_moves = production.move_raw_ids.filtered(
                        lambda m: m.wf_panel_component_id.id == anchor_comp_id
                    )
                    if matching_moves:
                        matching_moves[:1].write({"workorder_id": workorder.id})
            summary_count = len(cut_notes or [])
            self._print_colored(
                self._lang_choice(
                    "WF panel resumen de notas de corte → {count} registros",
                    "WF panel cut notes summary → {count} records",
                ).format(count=summary_count),
                tone="detail",
            )

        assembly_workorder = stage_map.get("assembly")
        bin_workorders = [stage_map[k] for k in sorted(stage_map) if k.startswith("cut_bin_")]
        return {
            "workorders": created_workorders if workorders_data else self.env["mrp.workorder"],
            "cut_wc_id": cut_wc.id if cut_wc else False,
            "width_cut_wc_id": False,
            "assembly_wc_id": assembly_wc.id if assembly_wc else False,
            "cut_instruction": cut_instruction,
            "width_cut_instruction": False,
            "assembly_instruction": assembly_instruction,
            "assembly_one_instruction": False,
            "assembly_two_instruction": False,
            "cut_workorder": False,
            "width_cut_workorder": False,
            "assembly_workorder": assembly_workorder,
            "assembly_one_workorder": False,
            "assembly_two_workorder": False,
            "bin_workorders": bin_workorders,
            "bin_anchor_map": bin_anchor_map,
            "width_cut_components": width_components,
            "single_cut_components": single_components,
            "cut_length_minutes": stage_timing_payload.get(length_stage_key, {}).get("minutes", 0.0),
            "cut_length_cost": stage_timing_payload.get(length_stage_key, {}).get("cost", 0.0),
            "cut_width_minutes": 0.0,
            "cut_width_cost": 0.0,
            "cut_length_component_count": length_component_count,
            "cut_width_component_count": width_component_count,
        }

    def _build_final_product_label(self, panel, section):
        # Compose the finished product label combining panel and section names.
        panel_name = getattr(panel, "display_name", False) or getattr(panel, "name", False) or getattr(panel, "project", False) or str(panel.id)
        section_name = getattr(section, "display_name", False) or getattr(section, "name", False) or str(section.id)
        return f"{panel_name} - {section_name}"

    def _apply_final_product_label(self, production, label):
        # Push the computed label onto the MO and its finished moves.
        if not production or not label:
            return
        if "product_description_mrp" in production._fields:
            if production.product_description_mrp != label:
                production.product_description_mrp = label
        finished_moves = production.move_finished_ids
        if finished_moves:
            finished_moves.write({
                "name": label,
                "description_picking": label,
            })

    def _create_leftover_moves(self, production, cut_notes):
        # Generate stock moves for reusable leftovers detected during cutting.
        Move = self.env["stock.move"]
        Product = self.env["product.product"].sudo()
        if not production or not cut_notes:
            return Move.browse()
        grouped = {}
        for entry in cut_notes:
            if not isinstance(entry, dict):
                continue
            if not entry.get("is_leftover"):
                continue
            variant_id = entry.get("variant_id")
            if not variant_id:
                continue
            bucket = grouped.setdefault(variant_id, {"qty": 0.0, "entries": []})
            bucket["qty"] += 1.0
            bucket["entries"].append(entry)
        if not grouped:
            return Move.browse()
        move_vals_list = []
        for variant_id, data in grouped.items():
            variant = Product.browse(variant_id)
            if not variant or not variant.exists():
                continue
            quantity = data["qty"]
            label = self._format_leftover_label(variant, data["entries"])
            move_vals = production._get_move_finished_values(
                variant.id,
                quantity,
                variant.uom_id.id,
                False,
                False,
                0,
            )
            move_vals.update({
                "name": label,
                "description_picking": label,
            })
            move_vals_list.append(move_vals)
        if not move_vals_list:
            return Move.browse()
        leftovers = Move.create(move_vals_list)
        leftovers._action_confirm()
        return leftovers

    def _format_leftover_label(self, variant, entries):
        # Build a human-friendly label for a leftover piece move.
        base = variant.display_name or variant.name or str(variant.id)
        length_entry = next((entry for entry in entries if entry.get("leftover_length")), None)
        if length_entry:
            formatted_length = self._format_measure(length_entry["leftover_length"])
            return _("Sobrante - %s (%s)") % (base, formatted_length)
        return _("Sobrante - %s") % base

    @staticmethod
    def _bin_pack_cuts(items, stock_length):
        """First-Fit Decreasing bin packing.

        items: list of (component, required_length_in)
        stock_length: available stick length in inches (0 = unlimited/generic)

        Returns list of bins, each bin is a list of (component, cut_length).
        """
        if not stock_length:
            # No stock length defined — one bin per component (no optimisation possible)
            return [[(comp, length)] for comp, length in items]

        # Sort descending by required length for FFD
        sorted_items = sorted(items, key=lambda x: x[1], reverse=True)
        bins = []  # each bin: (remaining_capacity, [(component, cut_length)])
        for comp, cut_len in sorted_items:
            placed = False
            for i, (remaining, cuts) in enumerate(bins):
                if remaining >= cut_len - 1e-6:
                    cuts.append((comp, cut_len))
                    bins[i] = (remaining - cut_len, cuts)
                    placed = True
                    break
            if not placed:
                bins.append((stock_length - cut_len, [(comp, cut_len)]))
        return [cuts for _, cuts in bins]

    def _prepare_raw_moves(self, production, aggregated, component_map=None, stock_length_map=None):
        # Build raw material move values using cut-optimised bin packing.
        self.ensure_one()
        moves = []
        if component_map is None:
            component_map = dict(self._get_component_variant_map())
        if stock_length_map is None:
            stock_length_map = self._get_component_stock_length_map()

        if component_map:
            Component = self.env["wf.panel.component"].sudo()
            Product = self.env["product.product"].sudo()
            components = Component.browse(component_map.keys()).sorted(key=lambda c: (c.sequence, c.id))

            # Group components by (variant_id, stock_length) for bin packing
            groups = {}  # (variant_id, stock_length) → [(component, cut_length)]
            for component in components:
                variant_id = component_map.get(component.id)
                if not variant_id:
                    continue
                stock_len = stock_length_map.get(component.id, 0.0)
                cut_len = component.data_length or 0.0
                key = (variant_id, stock_len)
                groups.setdefault(key, []).append((component, cut_len))

            seq = 10
            for (variant_id, stock_len), items in groups.items():
                variant = Product.browse(variant_id)
                if not variant or not variant.exists():
                    continue

                bins = self._bin_pack_cuts(items, stock_len)
                self._print_colored(
                    f"WF panel bin-pack: {variant.display_name} stock={stock_len}\" → "
                    f"{len(items)} cortes en {len(bins)} piezas",
                    tone="info",
                )

                for bin_cuts in bins:
                    # Build a combined description for this stick
                    cut_descs = []
                    for comp, cut_len in bin_cuts:
                        parts = []
                        if comp.data_length and comp.data_length > 0:
                            parts.append(f"L={comp.data_length:.3f}\"")
                        if comp.data_width and comp.data_width > 0:
                            parts.append(f"W={comp.data_width:.3f}\"")
                        if comp.data_depth and comp.data_depth > 0:
                            parts.append(f"D={comp.data_depth:.3f}\"")
                        piece_label = comp.data_id or ""
                        dims = ", ".join(parts)
                        cut_descs.append(f"{piece_label} ({dims})" if piece_label else dims)

                    if stock_len:
                        used = sum(cl for _, cl in bin_cuts)
                        waste = stock_len - used
                        cuts_str = " | ".join(cut_descs)
                        description = (
                            f"{variant.display_name} [{stock_len:.0f}\"] → "
                            f"Cortar: {cuts_str} | Desperdicio: {waste:.3f}\""
                        )
                    else:
                        cuts_str = " | ".join(cut_descs)
                        description = f"{variant.display_name} → Cortar: {cuts_str}"

                    # Use the first component in the bin as the anchor (for workorder linking)
                    anchor_comp = bin_cuts[0][0]
                    base_vals = production._get_move_raw_values(variant, 1.0, variant.uom_id)
                    base_vals.update({
                        "name": description,
                        "description_picking": description,
                        "wf_panel_component_id": anchor_comp.id,
                        "sequence": seq,
                    })
                    moves.append(base_vals)
                    seq += 1

            self._print_colored(
                f"WF panel raw moves prepared (bin-packed) → {len(moves)}",
                tone="detail",
            )

        if moves:
            return moves

        # Fallback: aggregated (no component map)
        for variant, quantity in aggregated.items():
            base_vals = production._get_move_raw_values(variant, quantity, variant.uom_id)
            label = variant.display_name
            base_vals.update({
                "name": label,
                "description_picking": label,
            })
            moves.append(base_vals)
        self._print_colored(
            f"WF panel raw moves prepared (aggregated fallback) → {len(moves)}",
            tone="detail",
        )
        return moves

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

    def _post_cut_list_to_mo(self, production, component_map=None, stock_length_map=None):
        """Post a formatted cut list as a chatter note on the MO."""
        if component_map is None:
            component_map = dict(self._get_component_variant_map())
        if stock_length_map is None:
            stock_length_map = self._get_component_stock_length_map()
        _logger.info("WF cut list: wizard=%s component_map size=%d stock_length_map size=%d", self.id, len(component_map), len(stock_length_map))
        print("[WF DEBUG] _post_cut_list_to_mo: wizard=%s component_map=%d stock_length_map=%d" % (self.id, len(component_map), len(stock_length_map)))
        if not component_map:
            _logger.warning("WF cut list: component_map is empty, no cut list will be posted")
            print("[WF DEBUG] component_map is EMPTY — skipping cut list")
            return

        Component = self.env["wf.panel.component"].sudo()
        Product = self.env["product.product"].sudo()
        components = Component.browse(component_map.keys()).sorted(key=lambda c: (c.sequence, c.id))

        # Group components by (variant, stock_length) same as bin packing
        groups = {}
        for component in components:
            variant_id = component_map.get(component.id)
            if not variant_id:
                continue
            stock_len = stock_length_map.get(component.id, 0.0)
            cut_len = component.data_length or 0.0
            key = (variant_id, stock_len)
            groups.setdefault(key, []).append((component, cut_len))

        lines_html = []
        total_sticks = 0
        for (variant_id, stock_len), items in groups.items():
            variant = Product.browse(variant_id)
            if not variant.exists():
                continue
            bins = self._bin_pack_cuts(items, stock_len)
            total_sticks += len(bins)
            for i, bin_cuts in enumerate(bins, 1):
                used = sum(cl for _, cl in bin_cuts)
                waste = (stock_len - used) if stock_len else 0.0
                stick_label = (
                    f"<b>{variant.display_name}</b> — {stock_len:.0f}&quot; stock"
                    if stock_len else
                    f"<b>{variant.display_name}</b>"
                )
                cuts_rows = "".join(
                    f"<tr><td style='padding:2px 8px'>{comp.data_id or comp.display_name}</td>"
                    f"<td style='padding:2px 8px'>{cut_l:.3f}&quot;</td>"
                    f"<td style='padding:2px 8px'>{comp.data_width:.3f}&quot;</td>"
                    f"<td style='padding:2px 8px'>{comp.data_depth:.3f}&quot;</td></tr>"
                    for comp, cut_l in bin_cuts
                )
                waste_row = (
                    f"<tr style='color:#999'><td colspan='2' style='padding:2px 8px'>"
                    f"Desperdicio</td><td colspan='2' style='padding:2px 8px'>{waste:.3f}&quot;</td></tr>"
                    if stock_len else ""
                )
                lines_html.append(
                    f"<p style='margin:8px 0 2px'>{stick_label} — Pieza #{i}</p>"
                    f"<table style='border-collapse:collapse;font-size:12px'>"
                    f"<thead><tr style='background:#eee'>"
                    f"<th style='padding:2px 8px;text-align:left'>ID</th>"
                    f"<th style='padding:2px 8px;text-align:left'>Largo</th>"
                    f"<th style='padding:2px 8px;text-align:left'>Ancho</th>"
                    f"<th style='padding:2px 8px;text-align:left'>Esp.</th>"
                    f"</tr></thead><tbody>{cuts_rows}{waste_row}</tbody></table>"
                )

        if not lines_html:
            return

        section_name = production.panel_section_id.display_name if production.panel_section_id else production.name
        header = (
            f"<h4 style='margin-bottom:4px'>✂ Lista de cortes — {section_name}</h4>"
            f"<p style='color:#555;margin-top:0'>{total_sticks} pieza(s) de stock requerida(s)</p>"
        )
        body = header + "".join(lines_html)
        production.message_post(body=Markup(body), subtype_xmlid="mail.mt_note")

    def _record_workorder_instruction(self, workorder, instruction):
        # Log and post instructions tied to a specific workorder.
        if not workorder:
            return
        workcenter_label = workorder.workcenter_id.display_name or workorder.name or _("Operación")
        instruction_text = f"{workcenter_label}: {instruction}"
        self._print_colored(
            f"WF panel workorder note → {instruction_text}",
            tone="detail",
        )
        production = workorder.production_id
        if not production or not hasattr(production, "message_post"):
            return
        try:
            production.message_post(
                body=f"<p>{instruction_text}</p>",
                subtype_xmlid="mail.mt_note",
            )
        except Exception as exc:
            _logger.warning(
                "WF panel workorder note post failed (%s): %s",
                workorder.id,
                exc,
            )

    def _assign_cut_components_to_workorder(self, production, workorder_meta, cut_notes):
        # Link raw moves that require cutting to the appropriate cut workorder.
        if not production:
            return
        cut_workorder = (workorder_meta or {}).get("cut_workorder") if workorder_meta else False
        width_cut_workorder = (workorder_meta or {}).get("width_cut_workorder") if workorder_meta else False
        cut_workorder = cut_workorder.exists() if cut_workorder else False
        width_cut_workorder = width_cut_workorder.exists() if width_cut_workorder else False
        width_components = set((workorder_meta or {}).get("width_cut_components") or [])
        single_components = set((workorder_meta or {}).get("single_cut_components") or [])
        if not cut_workorder and not width_cut_workorder:
            return

        component_variants = {}
        for entry in cut_notes or []:
            if not isinstance(entry, dict):
                continue
            if not entry.get("is_cut_instruction"):
                continue
            component_id = entry.get("component_id")
            variant_id = entry.get("variant_id")
            if component_id and variant_id:
                component_variants.setdefault(component_id, set()).add(variant_id)

        component_variant_map = dict(self._get_component_variant_map())

        def _collect_moves(component_ids):
            if not component_ids:
                return production.move_raw_ids.browse()
            moves = production.move_raw_ids.filtered(
                lambda move: move.wf_panel_component_id and move.wf_panel_component_id.id in component_ids
            )
            moves = moves.filtered(lambda move: not move.move_orig_ids)
            if moves:
                return moves
            variant_candidates = set()
            for comp_id in component_ids:
                variant_candidates.update(component_variants.get(comp_id, set()))
                base_variant_id = component_variant_map.get(comp_id)
                if base_variant_id:
                    variant_candidates.add(base_variant_id)
            if not variant_candidates:
                return production.move_raw_ids.browse()
            moves = production.move_raw_ids.filtered(lambda move: move.product_id.id in variant_candidates)
            return moves.filtered(lambda move: not move.move_orig_ids)

        assigned_move_ids = set()

        def _assign(workorder, component_ids, label):
            if not workorder or not component_ids:
                return
            moves = _collect_moves(component_ids)
            if not moves:
                self._print_colored(
                    self._lang_choice(
                        f"WF panel {label} assignment → sin movimientos coincidentes",
                        f"WF panel {label} assignment → no matching moves",
                    ),
                    tone="warning",
                )
                return
            # Skip moves already assigned to a work order (e.g. per-bin WOs)
            available_moves = moves.filtered(
                lambda move: move.id not in assigned_move_ids and not move.workorder_id
            )
            if not available_moves:
                return
            moves_to_update = available_moves.filtered(lambda move: not move.workorder_id or move.workorder_id.id != workorder.id)
            if not moves_to_update:
                return
            assignment_vals = {"workorder_id": workorder.id}
            if workorder.operation_id:
                assignment_vals["operation_id"] = workorder.operation_id.id
            moves_to_update.write(assignment_vals)
            assigned_move_ids.update(moves_to_update.ids)
            self._print_colored(
                self._lang_choice(
                    f"WF panel {label} assignment → {{count}} movimientos vinculados al WO #{{wo}}",
                    f"WF panel {label} assignment → {{count}} moves linked to WO #{{wo}}",
                ).format(count=len(moves_to_update), wo=workorder.id),
                tone="success",
            )
            debug_payload = moves_to_update.read(["id", "workorder_id", "product_id", "product_uom_qty", "wf_panel_component_id"])
            self._print_colored(
                self._lang_choice(
                    f"WF panel {label} assignment details → {{payload}}",
                    f"WF panel {label} assignment details → {{payload}}",
                ).format(payload=debug_payload),
                tone="detail",
            )

        # Assign width components first so they do not leak into the one-side stage.
        _assign(width_cut_workorder, width_components, "two-side cut" if self._get_lang_prefix() != "es" else "corte dos caras")
        _assign(cut_workorder, single_components, "one-side cut" if self._get_lang_prefix() != "es" else "corte una cara")

    def _assign_assembly_components_to_workorder(self, production, workorder_meta):
        # Attach material moves to the appropriate assembly workorders.
        if not production:
            return
        workorder_meta = workorder_meta or {}
        assembly_one = workorder_meta.get("assembly_one_workorder")
        assembly_two = workorder_meta.get("assembly_two_workorder")
        assembly_general = workorder_meta.get("assembly_workorder")
        assembly_one = assembly_one.exists() if assembly_one else False
        assembly_two = assembly_two.exists() if assembly_two else False
        assembly_general = assembly_general.exists() if assembly_general else False
        if not any((assembly_one, assembly_two, assembly_general)):
            return

        width_components = set(workorder_meta.get("width_cut_components") or [])
        single_components = set(workorder_meta.get("single_cut_components") or [])

        cut_workorder = workorder_meta.get("cut_workorder")
        width_cut_workorder = workorder_meta.get("width_cut_workorder")
        cut_workorder = cut_workorder.exists() if cut_workorder else False
        width_cut_workorder = width_cut_workorder.exists() if width_cut_workorder else False

        protected_moves = self.env["stock.move"]
        if cut_workorder:
            protected_moves |= production.move_raw_ids.filtered(
                lambda move: move.workorder_id and move.workorder_id.id == cut_workorder.id
            )
        if width_cut_workorder:
            protected_moves |= production.move_raw_ids.filtered(
                lambda move: move.workorder_id and move.workorder_id.id == width_cut_workorder.id
            )

        candidate_moves = production.move_raw_ids - protected_moves
        component_variant_map = dict(self._get_component_variant_map())
        assembly_variant_map = dict(self._get_component_assembly_variant_map())
        component_variants = {}
        for comp_id, variant_id in component_variant_map.items():
            if variant_id:
                component_variants.setdefault(comp_id, set()).add(variant_id)
        for comp_id, variant_id in assembly_variant_map.items():
            if variant_id:
                component_variants.setdefault(comp_id, set()).add(variant_id)

        assigned_move_ids = set()

        def _collect_moves(component_ids):
            if not component_ids:
                return candidate_moves.filtered(lambda move: not move.move_orig_ids)
            moves = candidate_moves.filtered(
                lambda move: move.wf_panel_component_id and move.wf_panel_component_id.id in component_ids
            )
            moves = moves.filtered(lambda move: not move.move_orig_ids)
            if moves:
                return moves
            variant_ids = set()
            for comp_id in component_ids:
                variant_ids.update(component_variants.get(comp_id, set()))
            if not variant_ids:
                return candidate_moves.filtered(lambda move: not move.move_orig_ids)
            moves = candidate_moves.filtered(lambda move: move.product_id.id in variant_ids)
            return moves.filtered(lambda move: not move.move_orig_ids)

        def _assign(workorder, component_ids, label_es, label_en):
            if not workorder or not component_ids:
                return
            moves = _collect_moves(component_ids)
            if not moves:
                self._print_colored(
                    self._lang_choice(
                        f"WF panel {label_es} → sin movimientos coincidentes",
                        f"WF panel {label_en} → no matching moves",
                    ),
                    tone="warning",
                )
                return
            available_moves = moves.filtered(lambda move: move.id not in assigned_move_ids)
            if not available_moves:
                return
            moves_to_update = available_moves.filtered(
                lambda move: not move.workorder_id or move.workorder_id.id != workorder.id
            )
            if not moves_to_update:
                assigned_move_ids.update(available_moves.ids)
                return
            assignment_vals = {"workorder_id": workorder.id}
            if workorder.operation_id:
                assignment_vals["operation_id"] = workorder.operation_id.id
            moves_to_update.write(assignment_vals)
            assigned_move_ids.update(moves_to_update.ids)
            self._print_colored(
                self._lang_choice(
                    f"WF panel {label_es} → {{count}} movimientos vinculados al WO #{{wo}}",
                    f"WF panel {label_en} → {{count}} moves linked to WO #{{wo}}",
                ).format(count=len(moves_to_update), wo=workorder.id),
                tone="success",
            )
            detail_payload = moves_to_update.read([
                "id",
                "workorder_id",
                "product_id",
                "product_uom_qty",
                "wf_panel_component_id",
            ])
            self._print_colored(
                self._lang_choice(
                    f"WF panel {label_es} → detalles {{payload}}",
                    f"WF panel {label_en} → details {{payload}}",
                ).format(payload=detail_payload),
                tone="detail",
            )

        _assign(assembly_one, single_components, "ensamblaje una cara", "assembly one-side")
        _assign(assembly_two, width_components, "ensamblaje dos caras", "assembly two-sides")

        fallback_workorder = assembly_general or assembly_two or assembly_one
        leftover_moves = candidate_moves.filtered(lambda move: move.id not in assigned_move_ids)
        if fallback_workorder and leftover_moves:
            moves_to_update = leftover_moves.filtered(
                lambda move: not move.workorder_id or move.workorder_id.id != fallback_workorder.id
            )
            if moves_to_update:
                assignment_vals = {"workorder_id": fallback_workorder.id}
                if fallback_workorder.operation_id:
                    assignment_vals["operation_id"] = fallback_workorder.operation_id.id
                moves_to_update.write(assignment_vals)
                assigned_move_ids.update(moves_to_update.ids)
                self._print_colored(
                    self._lang_choice(
                        "WF panel ensamblaje general → {count} movimientos vinculados al WO #{wo}",
                        "WF panel general assembly → {count} moves linked to WO #{wo}",
                    ).format(count=len(moves_to_update), wo=fallback_workorder.id),
                    tone="success",
                )
                detail_payload = moves_to_update.read([
                    "id",
                    "workorder_id",
                    "product_id",
                    "product_uom_qty",
                    "wf_panel_component_id",
                ])
                self._print_colored(
                    self._lang_choice(
                        "WF panel ensamblaje general → detalles {payload}",
                        "WF panel general assembly → details {payload}",
                    ).format(payload=detail_payload),
                    tone="detail",
                )
        if not assigned_move_ids:
            self._print_colored(
                self._lang_choice(
                    "WF panel ensamblaje → sin movimientos asignados",
                    "WF panel assembly → no moves assigned",
                ),
                tone="warning",
            )

    def _sync_workorder_instructions(self, production, workorder_meta):
        # Ensure stored instructions are pushed onto the relevant workorders.
        if not production or not production.workorder_ids:
            return
        if "wf_instruction" not in production.workorder_ids._fields:
            return
        if not workorder_meta:
            return
        instruction_payloads = []
        mapping = [
            ("cut_workorder", "cut_instruction"),
            ("width_cut_workorder", "width_cut_instruction"),
            ("assembly_workorder", "assembly_instruction"),
        ]
        for wo_key, instruction_key in mapping:
            workorder = workorder_meta.get(wo_key)
            instruction = workorder_meta.get(instruction_key)
            if not workorder or not instruction:
                continue
            workorder = workorder.exists()
            if not workorder:
                continue
            instruction_payloads.append((workorder, instruction))
        for workorder, instruction in instruction_payloads:
            workorder.write({"wf_instruction": instruction})

    def _prepare_cut_piece_transitions(self, production, workorder_meta, cut_notes):
        # Create intermediate finished/raw moves for cut pieces feeding assembly.
        assembly_map = dict(self._get_component_assembly_variant_map())
        if not production or not assembly_map:
            return
        cut_components = {
            entry.get("component_id")
            for entry in (cut_notes or [])
            if isinstance(entry, dict)
            and entry.get("is_cut_instruction")
            and entry.get("component_id")
        }
        target_component_ids = [cid for cid in assembly_map.keys() if cid in cut_components]
        if not target_component_ids:
            return
        component_map = dict(self._get_component_variant_map())
        intermediate_map = dict(self._get_component_intermediate_variant_map())
        width_components = set()
        if workorder_meta and workorder_meta.get("width_cut_components"):
            width_components.update(workorder_meta.get("width_cut_components") or [])
        else:
            width_components.update(self._get_width_cut_components() or [])
        Workorder = self.env["mrp.workorder"].sudo()
        cut_workorder = (workorder_meta or {}).get("cut_workorder") if workorder_meta else Workorder.browse()
        width_cut_workorder = (workorder_meta or {}).get("width_cut_workorder") if workorder_meta else Workorder.browse()
        assembly_workorder = (workorder_meta or {}).get("assembly_workorder") if workorder_meta else Workorder.browse()
        cut_workorder = cut_workorder.exists() if cut_workorder else Workorder.browse()
        width_cut_workorder = width_cut_workorder.exists() if width_cut_workorder else Workorder.browse()
        assembly_workorder = assembly_workorder.exists() if assembly_workorder else Workorder.browse()
        if not assembly_workorder:
            return
        if not cut_workorder and not width_cut_workorder:
            return
        Component = self.env["wf.panel.component"].sudo()
        Product = self.env["product.product"].sudo()
        Move = self.env["stock.move"].sudo()
        component_records = Component.browse(target_component_ids).exists()
        if not component_records:
            return
        new_finished_moves = Move.browse()
        new_raw_moves = Move.browse()
        for component in component_records.sorted(key=lambda comp: (comp.sequence, comp.id)):
            base_variant_id = component_map.get(component.id)
            assembly_variant_id = assembly_map.get(component.id)
            if not assembly_variant_id:
                continue
            width_required = component.id in width_components
            length_workorder = width_cut_workorder if width_required else cut_workorder
            if width_required and not width_cut_workorder:
                self._print_colored(
                    self._lang_choice(
                        f"WF panel: componente #{component.id} requiere corte de ancho pero no existe WO dedicado.",
                        f"WF panel: component #{component.id} requires width cut but no dedicated WO exists.",
                    ),
                    tone="warning",
                )
                width_required = False
                length_workorder = cut_workorder
            if not length_workorder:
                continue
            if assembly_variant_id == base_variant_id and not width_required:
                continue
            assembly_variant = Product.browse(assembly_variant_id)
            if not assembly_variant or not assembly_variant.exists():
                continue
            intermediate_variant = Product.browse(intermediate_map.get(component.id)) if width_required else Product.browse()
            has_intermediate = bool(intermediate_variant and intermediate_variant.exists() and intermediate_variant.id != assembly_variant.id)
            if width_required and not has_intermediate:
                intermediate_variant = Product.browse()
            qty = 1.0
            length_variant = intermediate_variant if has_intermediate else assembly_variant
            if not length_variant or not length_variant.exists():
                length_variant = assembly_variant

            # First stage finished move (length cut)
            length_label = length_variant.display_name or length_variant.name or str(length_variant.id)
            if component.data_id:
                length_label = f"{length_label} [{component.data_id}]"
            length_operation_id = False
            if length_workorder and length_workorder.operation_id:
                length_operation_id = length_workorder.operation_id.id
            length_finished_vals = production._get_move_finished_values(
                length_variant.id,
                qty,
                length_variant.uom_id.id,
                length_operation_id,
                False,
                0,
            )
            length_finished_vals.update({
                "name": length_label,
                "description_picking": length_label,
                "wf_panel_component_id": component.id,
            })
            length_finished_vals["workorder_id"] = length_workorder.id
            if length_workorder.operation_id:
                length_finished_vals["operation_id"] = length_workorder.operation_id.id
            length_finished_move = Move.create(length_finished_vals)
            new_finished_moves |= length_finished_move

            last_finished_move = length_finished_move

            if width_required and width_cut_workorder:
                width_label = assembly_variant.display_name or assembly_variant.name or str(assembly_variant.id)
                if component.data_id:
                    width_label = f"{width_label} [{component.data_id}]"
                # Raw move for width cut stage consuming intermediate piece
                width_raw_vals = production._get_move_raw_values(
                    length_variant,
                    qty,
                    length_variant.uom_id,
                )
                width_raw_vals.update({
                    "name": width_label,
                    "description_picking": width_label,
                    "wf_panel_component_id": component.id,
                    "sequence": component.sequence or width_raw_vals.get("sequence", 10),
                    "workorder_id": width_cut_workorder.id,
                })
                if width_cut_workorder and width_cut_workorder.operation_id:
                    width_raw_vals["operation_id"] = width_cut_workorder.operation_id.id
                width_raw_move = Move.create(width_raw_vals)
                new_raw_moves |= width_raw_move
                width_raw_move.write({"move_orig_ids": [(4, length_finished_move.id)]})
                length_finished_move.write({"move_dest_ids": [(4, width_raw_move.id)]})

                # Finished move produced by width cut stage (final piece)
                width_finished_vals = production._get_move_finished_values(
                    assembly_variant.id,
                    qty,
                    assembly_variant.uom_id.id,
                    width_cut_workorder.operation_id.id if width_cut_workorder.operation_id else False,
                    False,
                    0,
                )
                width_finished_vals.update({
                    "name": width_label,
                    "description_picking": width_label,
                    "wf_panel_component_id": component.id,
                    "workorder_id": width_cut_workorder.id,
                })
                if width_cut_workorder.operation_id:
                    width_finished_vals["operation_id"] = width_cut_workorder.operation_id.id
                width_finished_move = Move.create(width_finished_vals)
                new_finished_moves |= width_finished_move
                width_finished_move.write({"move_orig_ids": [(4, width_raw_move.id)]})
                width_raw_move.write({"move_dest_ids": [(4, width_finished_move.id)]})
                last_finished_move = width_finished_move

            # Assembly consumption of final piece
            assembly_label = assembly_variant.display_name or assembly_variant.name or str(assembly_variant.id)
            if component.data_id:
                assembly_label = f"{assembly_label} [{component.data_id}]"
            assembly_raw_vals = production._get_move_raw_values(
                assembly_variant,
                qty,
                assembly_variant.uom_id,
            )
            assembly_raw_vals.update({
                "name": assembly_label,
                "description_picking": assembly_label,
                "wf_panel_component_id": component.id,
                "sequence": component.sequence or assembly_raw_vals.get("sequence", 10),
            })
            assembly_raw_vals["workorder_id"] = assembly_workorder.id
            if assembly_workorder.operation_id:
                assembly_raw_vals["operation_id"] = assembly_workorder.operation_id.id
            assembly_raw_move = Move.create(assembly_raw_vals)
            new_raw_moves |= assembly_raw_move
            assembly_raw_move.write({"move_orig_ids": [(4, last_finished_move.id)]})
            last_finished_move.write({"move_dest_ids": [(4, assembly_raw_move.id)]})

        if new_finished_moves:
            new_finished_moves._action_confirm()
        if new_raw_moves:
            new_raw_moves._action_confirm()
            if self.auto_assign:
                new_raw_moves._action_assign()
        if new_raw_moves or new_finished_moves:
            self._print_colored(
                self._lang_choice(
                    "WF panel piezas cortadas generadas → raw:{raw} finished:{finished}",
                    "WF panel cut pieces generated → raw:{raw} finished:{finished}",
                ).format(raw=new_raw_moves.ids, finished=new_finished_moves.ids),
                tone="detail",
            )

    # ------------------------------------------------------------------
    # Matching helpers
    # ------------------------------------------------------------------

    # Role/structural words to ignore when extracting search keywords from data_id.
    _ROLE_WORDS = frozenset({
        'stud', 'plate', 'jack', 'king', 'top', 'bottom', 'vtp', 'cripple',
        'header', 'flat', 'corner', 'trimmer', 'double', 'triple', 'single',
        'full', 'half', 'filler', 'blocking', 'rim',
        'sill', 'nailer', 'backer', 'post', 'beam', 'ledger',
    })

    def _keywords_from_component(self, component):
        # Extract meaningful search tokens from a component data_id like D_Stud_2x6_SPF_No_2_1.
        raw = getattr(component, "data_id", None) or ""
        keywords = []
        for tok in re.split(r"[_\s]+", raw):
            t = tok.strip()
            if not t:
                continue
            if t.lower() in self._ROLE_WORDS:
                continue
            if len(t) <= 2:
                continue
            if t.isdigit():
                continue
            keywords.append(t)
        return keywords

    @staticmethod
    def _parse_product_name_length_inches(name):
        # Extract a length value in inches from a product name.
        # Handles: (144 in), (12 ft), 144 in, 12 ft, 12'
        # A unit suffix is required — bare trailing numbers like "No 2" are NOT treated as lengths.
        if not name:
            return None
        # Parenthesized form: (144 in) or (12 ft) or (12')
        m = re.search(r"\(\s*(\d+(?:\.\d+)?)\s*(in|inch(?:es)?|ft|feet|')\s*\)", name, re.IGNORECASE)
        if m:
            val = float(m.group(1))
            return val * 12 if m.group(2).lower() in ("ft", "feet", "'") else val
        # End-of-string form: 144 in, 12 ft, 12'
        m = re.search(r"(\d+(?:\.\d+)?)\s*(in|inch(?:es)?|ft|feet|')\s*$", name, re.IGNORECASE)
        if m:
            val = float(m.group(1))
            return val * 12 if m.group(2).lower() in ("ft", "feet", "'") else val
        return None

    def _match_component_to_variant(self, component):
        # Find a stock product matching component keywords with length >= required.
        # Searches by product name keywords AND default_code for better matching.
        target_length = getattr(component, "data_length", 0.0) or 0.0
        keywords = self._keywords_from_component(component)
        if not keywords:
            return False, _("No se pudo extraer nombre del componente %s") % (component.data_id or "?")

        # Build search domain: product must be consu and name must contain ALL keywords.
        domain = [("type", "=", "consu")]
        for kw in keywords:
            domain.append(("name", "ilike", kw))
        products = self.env["product.product"].search(domain, limit=300)

        if not products and len(keywords) > 2:
            # Retry with first 2 keywords only (less restrictive)
            domain2 = [("type", "=", "consu")]
            for kw in keywords[:2]:
                domain2.append(("name", "ilike", kw))
            products = self.env["product.product"].search(domain2, limit=300)

        # ── Fallback: buscar por default_code ──────────────────────────────
        if not products:
            # Extraer default_code candidato del data_id del componente
            raw = getattr(component, "data_id", None) or ""
            code_candidates = set()
            parts = raw.split("_")
            for i in range(len(parts)):
                for j in range(i + 1, len(parts) + 1):
                    candidate = "_".join(parts[i:j])
                    if 5 <= len(candidate) <= 40:
                        code_candidates.add(candidate)
            # Ordenar por longitud descendente (más específico primero)
            for candidate in sorted(code_candidates, key=len, reverse=True)[:5]:
                dc_products = self.env["product.product"].search([
                    ("type", "=", "consu"),
                    ("default_code", "ilike", candidate),
                ], limit=50)
                if dc_products:
                    products = dc_products
                    self._print_colored(
                        f"WF panel fallback default_code: '{candidate}' → {len(products)} productos",
                        tone="info",
                    )
                    break

        # ── Fallback 2: buscar por default_code con normalización ──────────
        if not products:
            # Normalizar: quitar caracteres no alfanuméricos y comparar
            raw = getattr(component, "data_id", None) or ""
            # Extraer tokens que parezcan códigos (contengan números y letras)
            normalized = re.sub(r'[^a-zA-Z0-9_]', '_', raw)
            # Probar substrings que contengan números
            for token in re.findall(r'[A-Za-z]+_\d+[A-Za-z0-9_]*', normalized):
                dc_products = self.env["product.product"].search([
                    ("type", "=", "consu"),
                    ("default_code", "ilike", f"%{token}%"),
                ], limit=50)
                if dc_products:
                    products = dc_products
                    self._print_colored(
                        f"WF panel fallback normalized: '{token}' → {len(products)} productos",
                        tone="info",
                    )
                    break

        # ── Fallback 3: buscar por nombre con menos keywords ──────────────
        if not products:
            # Intentar con 1 sola keyword
            for kw in keywords:
                kw_products = self.env["product.product"].search([
                    ("type", "=", "consu"),
                    ("name", "ilike", kw),
                ], limit=50)
                if kw_products:
                    products = kw_products
                    self._print_colored(
                        f"WF panel fallback single keyword: '{kw}' → {len(products)} productos",
                        tone="info",
                    )
                    break

        if not products:
            return False, _("Producto no registrado o no incluido en el nombre de la pieza")

        self._print_colored(
            f"WF panel search: '{component.data_id}' → keywords={keywords}, candidatos={len(products)}",
            tone="info",
        )

        if target_length <= 0:
            # No length requirement — return any match
            self._remember_component_stock_length(component, 0.0)
            self._remember_component_variant(component, products[0])
            return products[0], None

        # Filter by length >= target using defined stock lengths or product name
        sufficient = []
        generic = []
        best_found = 0.0
        for prod in products:
            tmpl = prod.product_tmpl_id
            stock_lengths = tmpl.wf_stock_length_ids
            if stock_lengths:
                # Use defined stock lengths — find shortest >= target
                for sl in stock_lengths.sorted('length_in'):
                    if sl.length_in > best_found:
                        best_found = sl.length_in
                    if sl.length_in + self._value_tolerance >= target_length:
                        sufficient.append((sl.length_in, prod))
                        break
            else:
                # Fall back to parsing length from product name
                length = self._parse_product_name_length_inches(prod.name or "")
                if length is None:
                    # No length info at all — generic raw stock
                    generic.append(prod)
                    continue
                if length > best_found:
                    best_found = length
                if length + self._value_tolerance >= target_length:
                    sufficient.append((length, prod))

        if sufficient:
            # Pick shortest sufficient (least waste)
            sufficient.sort(key=lambda x: x[0])
            _best_len, best = sufficient[0]
            self._remember_component_stock_length(component, _best_len)
            self._remember_component_variant(component, best)
            self._print_colored(
                f"WF panel selección → {best.display_name} (largo={_best_len:.3f} >= {target_length:.3f})",
                tone="success",
            )
            return best, None

        if generic:
            # Generic product (no length info at all) — raw stock to be cut to size
            best = generic[0]
            self._remember_component_stock_length(component, 0.0)
            self._remember_component_variant(component, best)
            self._print_colored(
                f"WF panel selección genérica → {best.display_name} (sin largo, objetivo={target_length:.3f})",
                tone="success",
            )
            return best, None

        reason = self._lang_choice(
            "Largo insuficiente: mejor candidato {l:.3f} < objetivo {t:.3f}",
            "Length insufficient: best candidate {l:.3f} < target {t:.3f}",
        ).format(l=best_found, t=target_length)
        return False, reason

    def _match_component_with_template(self, component, template):
        # Try to match a component against variants of a specific template.
        template = template.sudo()
        reasons = []
        required_values_groups = []
        for field_name, attribute_names in self._component_attribute_map:
            measure = getattr(component, field_name, None)
            if not measure or measure <= 0:
                continue
            attribute = self._find_attribute(attribute_names, auto_create=True)
            if not attribute:
                name_text = ", ".join(attribute_names) if isinstance(attribute_names, (list, tuple)) else attribute_names
                reasons.append(_("Falta el atributo (%s)") % name_text)
                return False, ", ".join(reasons)
            values = self._find_attribute_values(
                attribute,
                measure,
                template=template,
                create_missing=True,
                field_name=field_name,
                component=component,
            )
            if not values:
                attr_label = attribute.display_name or attribute.name
                reasons.append(_("Sin valor compatible para %s (%.3f)") % (attr_label, measure))
                return False, ", ".join(reasons)
            required_values_groups.append(values)
        # Antes de crear variantes, verificar si ya existe una con los combination_indices requeridos
        usable_variants = self._filter_variant_candidates(template.product_variant_ids)
        if required_values_groups:
            for variant in usable_variants:
                variant_value_ids = variant.product_template_attribute_value_ids.product_attribute_value_id
                if all(variant_value_ids & value_group for value_group in required_values_groups):
                    self._finalize_variant_selection(variant, component)
                    return variant, None
        # Si no existe, entonces crear variantes
        template._create_variant_ids()
        usable_variants = self._filter_variant_candidates(template.product_variant_ids)
        if required_values_groups:
            if not usable_variants:
                return False, _("Sin variante utilizable para %s") % template.display_name
            for variant in usable_variants:
                variant_value_ids = variant.product_template_attribute_value_ids.product_attribute_value_id
                if all(variant_value_ids & value_group for value_group in required_values_groups):
                    self._finalize_variant_selection(variant, component)
                    return variant, None
            return False, _("Sin variante generada para %s") % template.display_name
        default_variant = usable_variants[:1]
        if default_variant:
            default_variant.ensure_one()
            self._finalize_variant_selection(default_variant, component)
            return default_variant, None
        return False, _("Sin variante utilizable para %s") % template.display_name

    def _match_component_generic(self, component, candidate_variants=None):
        # Perform name- and attribute-based matching outside a chosen template.
        Product = self.env["product.product"]
        candidate_pool = self._filter_variant_candidates(candidate_variants)
        candidates = candidate_pool or Product.browse()
        component_core_key = self._prepare_component_core_key(component)
        # Try to locate the variant by its name before falling back to attribute matching.
        name_hints = self._extract_component_name_hints(component)
        if name_hints:
            hints_preview = ", ".join(name_hints[:10])
            self._print_colored(
                self._lang_choice(
                    "WF panel search: referencias de nombre consideradas → {preview}",
                    "WF panel search: name references considered → {preview}",
                ).format(preview=hints_preview),
                tone="detail",
            )
        base_name = self._prepare_code_field(getattr(component, "data_id", "") or "")
        last_reason = None
        name_match_attempted = bool(name_hints)
        found_variants_by_name = False
        for index, name_hint in enumerate(name_hints):
            if not name_hint:
                continue
            if index == 0:
                self._print_colored(
                    self._lang_choice(
                        "WF panel search: buscando variante por nombre exacto '{hint}'",
                        "WF panel search: searching variant by exact name '{hint}'",
                    ).format(hint=name_hint),
                    tone="info",
                )
            domain = [("name", "=", name_hint)]
            if candidates:
                domain.append(("id", "in", candidates.ids))
            variants = Product.search(domain, limit=10)
            variants = self._filter_variant_candidates(variants)
            if not variants:
                self._print_colored(
                    self._lang_choice(
                        "WF panel search: sin variantes con nombre '{hint}'",
                        "WF panel search: no variants with name '{hint}'",
                    ).format(hint=name_hint),
                    tone="warning",
                )
                continue
            found_variants_by_name = True
            name_match_attempted = True
            self._log_candidate_variants(variants, source=f"nombre exacto '{name_hint}'", component=component)
            for variant in variants:
                if component_core_key and len(component_core_key) >= _CORE_MIN_MATCH:
                    variant_key = self._prepare_product_core_key(variant.display_name or variant.name or "")
                    if not self._core_strings_match(component_core_key, variant_key):
                        continue
                matches, reason = self._variant_matches_component(variant, component)
                if matches:
                    self._finalize_variant_selection(variant, component)
                    return variant, None
                last_reason = reason
                self._print_colored(
                    self._lang_choice(
                        "WF panel search: variante descartada al validar atributos → #{vid} ({detail})",
                        "WF panel search: variant rejected during attribute validation → #{vid} ({detail})",
                    ).format(vid=variant.id, detail=reason or self._lang_choice("sin detalle", "no detail")),
                    tone="warning",
                )
        similarity_limit = 3
        similarity_cutoff = 0.4
        if base_name:
            similarity_matches = self._search_variants_by_similarity(
                base_name,
                candidate_variants=candidates,
                limit=similarity_limit,
                cutoff=similarity_cutoff,
            )
            name_match_attempted = True
            filtered_matches = [
                (variant, score)
                for variant, score in similarity_matches
                if not variant.wf_panel_manufactured_only
            ]
            if filtered_matches:
                preview = ", ".join(
                    f"#{variant.id}:{score:.2f}" for variant, score in filtered_matches[:similarity_limit]
                )
                self._print_colored(
                    self._lang_choice(
                        "WF panel search: coincidencias por similitud difflib (cutoff={cutoff}) → {preview}",
                        "WF panel search: difflib similarity matches (cutoff={cutoff}) → {preview}",
                    ).format(cutoff=similarity_cutoff, preview=preview),
                    tone="detail",
                )
                variant_ids = [variant.id for variant, _score in filtered_matches]
                variant_records = self.env["product.product"].browse(variant_ids)
                variant_records = self._filter_variant_candidates(variant_records)
                if variant_records:
                    found_variants_by_name = True
                self._log_candidate_variants(
                    variant_records,
                    source="difflib",
                    component=component,
                )
                for variant, score in filtered_matches:
                    if component_core_key and len(component_core_key) >= _CORE_MIN_MATCH:
                        variant_key = self._prepare_product_core_key(variant.display_name or variant.name or "")
                        if not self._core_strings_match(component_core_key, variant_key):
                            continue
                    matches, reason = self._variant_matches_component(variant, component)
                    if matches:
                        self._print_colored(
                            self._lang_choice(
                                "WF panel search: variante aceptada por similitud difflib ({score:.2f}) → #{vid}",
                                "WF panel search: variant accepted by difflib similarity ({score:.2f}) → #{vid}",
                            ).format(score=score, vid=variant.id),
                            tone="success",
                        )
                        self._finalize_variant_selection(variant, component)
                        return variant, None
                    last_reason = reason
                    self._print_colored(
                        self._lang_choice(
                            "WF panel search: variante descartada tras similitud difflib ({score:.2f}) → #{vid} ({detail})",
                            "WF panel search: variant rejected after difflib similarity ({score:.2f}) → #{vid} ({detail})",
                        ).format(score=score, vid=variant.id, detail=reason or self._lang_choice("sin detalle", "no detail")),
                        tone="warning",
                    )
            elif similarity_matches:
                self._print_colored(
                    self._lang_choice(
                        "WF panel search: coincidencias por similitud difflib descartadas por flag manufacturable",
                        "WF panel search: difflib similarity matches discarded by manufactured-only flag",
                    ),
                    tone="warning",
                )
            else:
                self._print_colored(
                    self._lang_choice(
                        "WF panel search: sin coincidencias por similitud difflib (cutoff={cutoff})",
                        "WF panel search: no difflib similarity matches (cutoff={cutoff})",
                    ).format(cutoff=similarity_cutoff),
                    tone="warning",
                )
        terms = self._extract_component_terms(component)
        if terms:
            terms_preview = ", ".join(terms[:10])
            self._print_colored(
                self._lang_choice(
                    "WF panel search: términos 'ilike' considerados → {preview}",
                    "WF panel search: 'ilike' terms considered → {preview}",
                ).format(preview=terms_preview),
                tone="detail",
            )
        for idx, term in enumerate(terms):
            if idx == 0:
                self._print_colored(
                    self._lang_choice(
                        "WF panel search: buscando variante por nombre ilike '{term}'",
                        "WF panel search: searching variant by ilike term '{term}'",
                    ).format(term=term),
                    tone="info",
                )
            domain = [("name", "ilike", term)]
            if candidates:
                domain.append(("id", "in", candidates.ids))
            variants = Product.search(domain, limit=10)
            variants = self._filter_variant_candidates(variants)
            if not variants:
                self._print_colored(
                    self._lang_choice(
                        "WF panel search: sin variantes con término ilike '{term}'",
                        "WF panel search: no variants with ilike term '{term}'",
                    ).format(term=term),
                    tone="warning",
                )
                continue
            found_variants_by_name = True
            name_match_attempted = True
            self._log_candidate_variants(variants, source=f"nombre ilike '{term}'", component=component)
            for variant in variants:
                if component_core_key and len(component_core_key) >= _CORE_MIN_MATCH:
                    variant_key = self._prepare_product_core_key(variant.display_name or variant.name or "")
                    if not self._core_strings_match(component_core_key, variant_key):
                        continue
                matches, reason = self._variant_matches_component(variant, component)
                if matches:
                    self._finalize_variant_selection(variant, component)
                    return variant, None
                last_reason = reason
                self._print_colored(
                    self._lang_choice(
                        "WF panel search: variante descartada al validar atributos → #{vid} ({detail})",
                        "WF panel search: variant rejected during attribute validation → #{vid} ({detail})",
                    ).format(vid=variant.id, detail=reason or self._lang_choice("sin detalle", "no detail")),
                    tone="warning",
                )
        if name_match_attempted and not found_variants_by_name:
            return False, _("Sin productos encontrados por nombre")
        if name_match_attempted:
            return False, last_reason or _("Sin coincidencia tras validar productos por nombre")
        reasons = []
        for field_name, attribute_names in self._component_attribute_map:
            measure = getattr(component, field_name, None)
            if not measure:
                continue
            attribute = self._find_attribute(attribute_names, auto_create=True)
            if not attribute:
                name_text = ", ".join(attribute_names) if isinstance(attribute_names, (list, tuple)) else attribute_names
                reasons.append(_("Falta el atributo (%s)") % name_text)
                return False, ", ".join(reasons)
            self._print_colored(
                f"WF panel search: evaluando atributo {attribute.display_name} con medida {measure}",
                tone="detail",
            )
            values = self._find_attribute_values(
                attribute,
                measure,
                field_name=field_name,
                component=component,
            )
            if not values:
                attr_label = attribute.display_name or attribute.name
                formatted_measure = self._format_measure_for_attribute(attribute, measure)
                reasons.append(_("Sin valor compatible para %s (%s)") % (attr_label, formatted_measure))
                return False, ", ".join(reasons)
            domain = [("product_template_attribute_value_ids.product_attribute_value_id", "in", values.ids)]
            if candidates:
                domain.append(("id", "in", candidates.ids))
            candidates = Product.search(domain)
            candidates = self._filter_variant_candidates(candidates)
            if not candidates:
                attr_label = attribute.display_name or attribute.name
                value_labels = ", ".join(values.mapped("name")) or self._format_measure_for_attribute(attribute, measure)
                reasons.append(_("Sin variante con %s = %s") % (attr_label, value_labels))
                return False, ", ".join(reasons)
            candidate_ids = ", ".join(str(variant.id) for variant in candidates)
            self._print_colored(
                f"WF panel search: variantes compatibles tras atributo {attribute.display_name} → [{candidate_ids}]",
                tone="detail",
            )
            candidate_names = ", ".join(
                f"#{variant.id}:{variant.display_name}"[:80] for variant in candidates[:10]
            )
            self._print_colored(
                f"WF panel search: detalles de variantes → {candidate_names or '—'}",
                tone="detail",
            )
        if candidates:
            best = candidates[0]
            self._finalize_variant_selection(best, component)
            return best, None
        return False, _("Sin coincidencia")

    def _finalize_variant_selection(self, variant, component):
        # Remember the chosen variant and prepare related cut data.
        if not variant or not component:
            return
        assembly_variant = variant
        intermediate_variant = False
        width_cut_required = False
        try:
            assembly_variant, intermediate_variant, width_cut_required = self._ensure_cut_piece_variant(variant, component)
            assembly_variant = assembly_variant or variant
            self._handle_cut_leftover_variant(variant, component)
        except Exception as exc:
            _logger.exception("WF panel leftover variant error: %s", exc)
            self._print_colored(
                f"WF panel leftover: {exc}",
                tone="error",
            )
        if width_cut_required and intermediate_variant:
            self._remember_component_intermediate_variant(component, intermediate_variant)
        self._remember_component_width_cut(component, width_cut_required)
        self._remember_component_variant(component, variant)
        _logger.info("WF cut list: remembered component %s → variant %s (wizard %s)", component.id, variant.id if variant else None, self.id)
        self._remember_component_assembly_variant(component, assembly_variant or variant)
        self._flush_cut_usage(component, variant)

    def _handle_cut_leftover_variant(self, variant, component):
        # Evaluate leftover lengths and ensure reusable variants/subproducts.
        length_mapping = next(
            (mapping for mapping in self._component_attribute_map if mapping[0] == "data_length"),
            None,
        )
        if not length_mapping:
            return
        attribute_names = length_mapping[1]
        target_measure = getattr(component, "data_length", None)
        if not target_measure or target_measure <= 0:
            return
        attribute = self._find_attribute(attribute_names)
        if not attribute:
            return
        variant_ptav = variant.product_template_attribute_value_ids.filtered(
            lambda ptav: ptav.attribute_id == attribute
        )[:1]
        if not variant_ptav:
            return
        value = variant_ptav.product_attribute_value_id
        magnitude = self._extract_magnitude(value)
        if magnitude is None:
            return
        base_uom = self._get_base_length_uom()
        required_measure = target_measure
        if value.uom_id and base_uom and value.uom_id.category_id == base_uom.category_id:
            required_measure = base_uom._compute_quantity(target_measure, value.uom_id)
            required_measure = self._apply_rounding(required_measure, value.uom_id)
        leftover = magnitude - required_measure
        if leftover <= self._value_tolerance:
            return
        leftover = max(0.0, leftover)
        if value.uom_id and base_uom and value.uom_id.category_id == base_uom.category_id:
            leftover_base = value.uom_id._compute_quantity(leftover, base_uom)
        else:
            leftover_base = leftover
        if base_uom:
            leftover_base = self._apply_rounding(leftover_base, base_uom)
        if leftover_base <= self._value_tolerance:
            return
        leftover_variant = self._ensure_leftover_variant(variant, attribute, leftover_base)
        if leftover_variant:
            display_name = getattr(leftover_variant, "display_name", False) or str(leftover_variant)
            leftover_id = getattr(leftover_variant, "id", False) or "—"
            note = _(
                "Sobrante reutilizable: %s (#%s) con largo %.3f"
            ) % (
                display_name,
                leftover_id,
                leftover_base,
            )
            entry = {
                "note": note,
                "variant_id": leftover_variant.id if leftover_variant else False,
                "variant_display": display_name,
                "component_id": getattr(component, "id", False),
                "is_leftover": True,
                "leftover_length": leftover_base,
            }
            holder = self.env.context.get("_cut_notes_holder")
            if isinstance(holder, list):
                holder.append(entry)
            self._print_colored(note, tone="detail")

    def _ensure_leftover_variant(self, base_variant, attribute, leftover_measure):
        # Guarantee a product variant exists for the leftover length.
        template = base_variant.product_tmpl_id.sudo()
        leftover_values = self._find_attribute_values(
            attribute,
            leftover_measure,
            template=template,
            create_missing=True,
            field_name="data_length",
            component=None,
        )
        leftover_value = False
        if leftover_values:
            exact_values = leftover_values.filtered(lambda val: self._is_measure_match(val, leftover_measure))
            if exact_values:
                leftover_value = exact_values[0]
        if not leftover_value:
            leftover_value = self._ensure_exact_attribute_value(attribute, leftover_measure, template)
        if not leftover_value:
            return False
        template._create_variant_ids()
        target_map = {}
        for ptav in base_variant.product_template_attribute_value_ids:
            attr_id = ptav.attribute_id.id
            if attr_id == attribute.id:
                target_map[attr_id] = leftover_value.id
            else:
                target_map[attr_id] = ptav.product_attribute_value_id.id
        for candidate in template.product_variant_ids:
            cand_map = {
                ptav.attribute_id.id: ptav.product_attribute_value_id.id
                for ptav in candidate.product_template_attribute_value_ids
            }
            if len(cand_map) != len(target_map):
                continue
            if all(cand_map.get(attr_id) == value_id for attr_id, value_id in target_map.items()):
                self._tag_variant_as_leftover_stock(candidate)
                return candidate
        ptav_ids = []
        Ptav = self.env["product.template.attribute.value"].sudo()
        for attr_id, value_id in target_map.items():
            ptav = Ptav.search([
                ("product_tmpl_id", "=", template.id),
                ("attribute_id", "=", attr_id),
                ("product_attribute_value_id", "=", value_id),
            ], limit=1)
            if ptav:
                ptav_ids.append(ptav.id)
        if not ptav_ids:
            return False
        new_variant = self.env["product.product"].sudo().create({
            "product_tmpl_id": template.id,
            "product_template_attribute_value_ids": [(6, 0, ptav_ids)],
        })
        self._print_colored(
            _(
                "Creada variante remanente #%s (%s) con largo %.3f"
            )
            % (new_variant.id, new_variant.display_name, leftover_measure),
            tone="success",
        )
        self._tag_variant_as_leftover_stock(new_variant)
        return new_variant

    def _ensure_ptav(self, template, attribute, value):
        # Make sure a PTAV exists for the given template/attribute/value trio.
        if not template or not attribute or not value:
            return False
        AttributeLine = self.env["product.template.attribute.line"].sudo()
        Ptav = self.env["product.template.attribute.value"].sudo()
        line = template.attribute_line_ids.filtered(lambda rec: rec.attribute_id == attribute)[:1]
        if not line:
            line = AttributeLine.create({
                "product_tmpl_id": template.id,
                "attribute_id": attribute.id,
                "value_ids": [(6, 0, [value.id])],
            })
        elif value not in line.value_ids:
            line.write({"value_ids": [(4, value.id)]})
        ptav = Ptav.search([
            ("product_tmpl_id", "=", template.id),
            ("attribute_id", "=", attribute.id),
            ("product_attribute_value_id", "=", value.id),
        ], limit=1)
        if not ptav:
            ptav = Ptav.create({
                "product_tmpl_id": template.id,
                "attribute_id": attribute.id,
                "product_attribute_value_id": value.id,
            })
        return ptav.id

    def _ensure_variant_from_value_map(self, template, value_map, base_variant, log_label=None):
        # Locate or create a variant that matches the provided attribute value map.
        Product = self.env["product.product"].sudo()
        template = template.sudo()
        template._create_variant_ids()
        target_map = {
            attr_id: value.id
            for attr_id, value in value_map.items()
            if value
        }
        # Check for existing variant with same combination_indices and product_tmpl_id
        for candidate in template.product_variant_ids:
            cand_map = {
                ptav.attribute_id.id: ptav.product_attribute_value_id.id
                for ptav in candidate.product_template_attribute_value_ids
            }
            if all(cand_map.get(attr_id) == value_id for attr_id, value_id in target_map.items()):
                self._tag_variant_as_cut_consumable(candidate, base_variant)
                return candidate
        ptav_ids = []
        for attr_id, value in value_map.items():
            if not value:
                continue
            attribute = value.attribute_id
            ptav_id = self._ensure_ptav(template, attribute, value)
            if ptav_id:
                ptav_ids.append(ptav_id)
        if not ptav_ids:
            return False
        # Double-check for existing variant before creating
        existing_variant = Product.search([
            ("product_tmpl_id", "=", template.id),
            ("product_template_attribute_value_ids", "in", ptav_ids)
        ], limit=1)
        if existing_variant:
            self._tag_variant_as_cut_consumable(existing_variant, base_variant)
            return existing_variant
        new_variant = Product.create({
            "product_tmpl_id": template.id,
            "product_template_attribute_value_ids": [(6, 0, ptav_ids)],
        })
        if log_label:
            self._print_colored(
                f"WF panel: creada variante {log_label} → #{new_variant.id} {new_variant.display_name}",
                tone="success",
            )
        self._tag_variant_as_cut_consumable(new_variant, base_variant)
        return new_variant

    def _ensure_cut_piece_variant(self, base_variant, component):
        # Determine length/width cut requirements and produce the needed variants.
        if not base_variant or not component:
            return base_variant, False, False

        template = base_variant.product_tmpl_id.sudo()
        if not template:
            return base_variant, False, False

        length_names = next((mapping[1] for mapping in self._component_attribute_map if mapping[0] == "data_length"), [])
        width_names = next((mapping[1] for mapping in self._component_attribute_map if mapping[0] == "data_width"), [])
        length_attribute = self._find_attribute(length_names) if length_names else False
        width_attribute = self._find_attribute(width_names) if width_names else False

        target_length = getattr(component, "data_length", None)
        target_width = getattr(component, "data_width", None)

        base_value_map = {
            ptav.attribute_id.id: ptav.product_attribute_value_id
            for ptav in base_variant.product_template_attribute_value_ids
        }
        target_value_map = dict(base_value_map)
        intermediate_value_map = dict(base_value_map)

        base_length_value = base_value_map.get(length_attribute.id) if length_attribute else False
        base_width_value = base_value_map.get(width_attribute.id) if width_attribute else False
        new_length_value = base_length_value
        new_width_value = base_width_value
        length_changed = False
        width_changed = False

        if length_attribute and target_length and target_length > 0:
            if not (base_length_value and self._is_measure_match(base_length_value, target_length)):
                candidate_values = self._find_attribute_values(
                    length_attribute,
                    target_length,
                    template=template,
                    create_missing=True,
                    field_name="data_length",
                    component=component,
                )
                piece_value = candidate_values[:1] if candidate_values else self.env["product.attribute.value"].browse()
                piece_value = piece_value and piece_value[0] or False
                if piece_value and not self._is_measure_match(piece_value, target_length):
                    piece_value = self._ensure_exact_attribute_value(length_attribute, target_length, template)
                if not piece_value:
                    piece_value = base_length_value
                if piece_value:
                    new_length_value = piece_value
            if new_length_value:
                target_value_map[length_attribute.id] = new_length_value
                intermediate_value_map[length_attribute.id] = new_length_value
                if base_length_value and new_length_value.id != base_length_value.id:
                    length_changed = True

        if width_attribute and target_width and target_width > 0:
            if base_width_value and self._is_measure_match(base_width_value, target_width):
                new_width_value = base_width_value
            else:
                width_values = self._find_attribute_values(
                    width_attribute,
                    target_width,
                    template=template,
                    create_missing=True,
                    field_name="data_width",
                    component=component,
                )
                candidate = width_values[:1] if width_values else self.env["product.attribute.value"].browse()
                candidate = candidate and candidate[0] or False
                if candidate and not self._is_measure_match(candidate, target_width):
                    candidate = self._ensure_exact_attribute_value(width_attribute, target_width, template)
                if candidate:
                    new_width_value = candidate
            if new_width_value:
                target_value_map[width_attribute.id] = new_width_value
                if not base_width_value or new_width_value.id != base_width_value.id:
                    width_changed = True
                intermediate_value_map[width_attribute.id] = base_width_value if base_width_value and width_changed else new_width_value

        base_id_map = {attr_id: value.id for attr_id, value in base_value_map.items() if value}
        target_id_map = {attr_id: value.id for attr_id, value in target_value_map.items() if value}
        intermediate_id_map = {attr_id: value.id for attr_id, value in intermediate_value_map.items() if value}

        final_variant = base_variant
        intermediate_variant = False
        width_required = width_changed

        if target_id_map != base_id_map:
            final_variant = self._ensure_variant_from_value_map(
                template,
                target_value_map,
                base_variant,
                log_label=self._lang_choice("final de corte", "final cut"),
            ) or base_variant

        intermediate_needed = width_required and intermediate_id_map != target_id_map
        if intermediate_needed:
            intermediate_variant = self._ensure_variant_from_value_map(
                template,
                intermediate_value_map,
                base_variant,
                log_label=self._lang_choice("intermedio de corte", "cut intermediate"),
            ) or base_variant
            if intermediate_variant and intermediate_variant.id != base_variant.id:
                self._mark_variant_manufacturable_only(intermediate_variant)

        if final_variant and final_variant != base_variant and length_changed:
            self._print_colored(
                self._lang_choice(
                    f"WF panel: variante final ajustada a largo {target_length}",
                    f"WF panel: final variant adjusted to length {target_length}",
                ),
                tone="detail",
            )
        if width_required:
            self._print_colored(
                self._lang_choice(
                    f"WF panel: pieza requiere corte de ancho → componente #{component.id}",
                    f"WF panel: piece requires width cut → component #{component.id}",
                ),
                tone="detail",
            )

        return final_variant, intermediate_variant, width_required

    def _ensure_exact_attribute_value(self, attribute, measure, template):
        # Find or create an attribute value matching the requested measure.
        base_uom = self._get_base_length_uom()
        values = attribute.value_ids
        if template:
            line = template.attribute_line_ids.filtered(lambda l: l.attribute_id == attribute)[:1]
            if line and line.value_ids:
                values = line.value_ids
        for value in values:
            if self._is_measure_match(value, measure):
                return value
        allow_variants = getattr(attribute, "create_variant", "always") != "no_variant"
        if not allow_variants or not template:
            return False
        return self._create_attribute_value(attribute, measure, template, base_uom)

    def _search_variants_by_similarity(self, base_text, candidate_variants=None, limit=3, cutoff=0.4):
        # Locate variants by fuzzy name similarity to the component text.
        Product = self.env["product.product"]
        normalized = re.sub(r"\s+", " ", base_text or "").strip()
        if not normalized:
            return []
        candidate_pool = self._filter_variant_candidates(candidate_variants)
        if not candidate_pool:
            candidate_pool = Product.browse()
        if not candidate_pool:
            segments = self._generate_similarity_segments(normalized, min_letters=3)
            if not segments:
                segments = [normalized]
            seen_ids = set()
            for segment in segments[:10]:
                domain = [("name", "ilike", segment)]
                found = Product.search(domain, limit=25)
                found = self._filter_variant_candidates(found)
                if not found:
                    continue
                new_variants = found.filtered(lambda variant: variant.id not in seen_ids)
                if not new_variants:
                    continue
                seen_ids.update(new_variants.ids)
                candidate_pool |= new_variants
        if not candidate_pool:
            return []
        name_map = {}
        choices = []
        for variant in candidate_pool:
            label = (variant.display_name or variant.name or "").strip()
            if not label:
                continue
            key = label.lower()
            choices.append(key)
            name_map.setdefault(key, []).append((variant, label))
        if not choices:
            return []
        needle = normalized.lower()
        matched_keys = difflib.get_close_matches(needle, choices, n=limit, cutoff=cutoff)
        if not matched_keys:
            return []
        results = []
        for key in matched_keys:
            entries = name_map.get(key, [])
            for variant, label in entries:
                ratio = difflib.SequenceMatcher(None, needle, key).ratio()
                results.append((variant, ratio))
        results.sort(key=lambda item: (-item[1], (item[0].display_name or item[0].name or "").lower()))
        primary = []
        fallback = []
        seen_variant_ids = set()
        for variant, ratio in results:
            if variant.id in seen_variant_ids:
                continue
            seen_variant_ids.add(variant.id)
            bucket = primary if not variant.wf_panel_manufactured_only else fallback
            bucket.append((variant, ratio))
        unique = primary[:limit]
        if len(unique) < limit and fallback:
            unique.extend(fallback[: max(0, limit - len(unique))])
        return unique

    def _find_attribute_values(
        self,
        attribute,
        measure,
        template=None,
        create_missing=False,
        field_name=None,
        component=None,
    ):
        base_uom = self._get_base_length_uom()
        value_pool = attribute.value_ids
        if template:
            line = template.attribute_line_ids.filtered(lambda l: l.attribute_id == attribute)[:1]
            if line and line.value_ids:
                value_pool = line.value_ids
        matched = attribute.value_ids.browse()
        for value in value_pool:
            magnitude = self._extract_magnitude(value)
            if magnitude is None:
                continue
            converted_measure = measure
            if value.uom_id and base_uom and value.uom_id.category_id == base_uom.category_id:
                converted_measure = base_uom._compute_quantity(measure, value.uom_id)
                converted_measure = self._apply_rounding(converted_measure, value.uom_id)
            if abs(converted_measure - magnitude) <= self._value_tolerance:
                matched |= value
        if matched:
            return matched
        allow_variants = getattr(attribute, "create_variant", "always") != "no_variant"
        if field_name in self._cuttable_fields:
            cut_value = self._find_cuttable_value(
                attribute,
                measure,
                base_uom,
                template=template,
                component=component,
                field_name=field_name,
            )
            if cut_value:
                if create_missing and template and allow_variants:
                    cut_value = self._ensure_value_on_template(attribute, cut_value, template)
                if component:
                    self._record_cut_usage(component, attribute, measure, cut_value)
                return cut_value
        if create_missing and template and allow_variants:
            new_value = self._create_attribute_value(attribute, measure, template, base_uom)
            if new_value:
                return new_value
        return matched

    def _find_cuttable_value(
        self,
        attribute,
        measure,
        base_uom,
        template=None,
        component=None,
        field_name=None,
    ):
        # Choose the smallest attribute value that can cover the desired measure.
        values = attribute.value_ids
        if template:
            line = template.attribute_line_ids.filtered(lambda l: l.attribute_id == attribute)[:1]
            if line and line.value_ids:
                values = line.value_ids
        if not values:
            return values
        candidates = []
        for value in values:
            magnitude = self._extract_magnitude(value)
            if magnitude is None:
                continue
            compare_measure = measure
            if value.uom_id and base_uom and value.uom_id.category_id == base_uom.category_id:
                compare_measure = base_uom._compute_quantity(measure, value.uom_id)
                compare_measure = self._apply_rounding(compare_measure, value.uom_id)
            delta = magnitude - compare_measure
            if delta + self._value_tolerance < 0:
                continue
            adjusted_delta = max(0.0, delta)
            candidates.append((adjusted_delta, magnitude, value))
        if not candidates:
            return attribute.value_ids.browse()
        candidates.sort(key=lambda item: (item[0], item[1]))
        for delta, _magnitude, value in candidates:
            has_variant = self._candidate_has_variant(template, attribute, value, component, field_name)
            if component:
                chosen_label = value.name
                target = self._format_measure_for_attribute(attribute, measure)
                tone = "detail" if has_variant else "warning"
                message = self._format_cut_candidate_message(
                    candidate_label=chosen_label,
                    delta_value=self._format_measure(delta),
                    has_variant=has_variant,
                    target_label=target,
                )
                self._print_colored(message, tone=tone)
            if has_variant:
                return value
        # Fall back to the smallest candidate even if no variant currently uses it
        return candidates[0][2]

    def _candidate_has_variant(self, template, attribute, value, component, field_name):
        # Check if a given attribute value combination maps to an existing variant.
        Product = self.env["product.product"].sudo()
        domain = [
            ("product_template_attribute_value_ids.product_attribute_value_id", "=", value.id),
        ]
        template_condition = None
        if template:
            template_condition = ("product_tmpl_id", "=", template.id)
            domain.append(template_condition)
        for other_field, attribute_names in self._component_attribute_map:
            if other_field == field_name:
                continue
            other_measure = getattr(component, other_field, None) if component else None
            if not other_measure:
                continue
            other_attribute = self._find_attribute(attribute_names)
            if not other_attribute:
                continue
            if getattr(other_attribute, "create_variant", "always") == "no_variant":
                continue
            matches = self._match_template_values(other_attribute, other_measure, template)
            if not matches:
                return False
            domain.append(("product_template_attribute_value_ids.product_attribute_value_id", "in", matches.ids))
        variant = Product.search(domain, limit=1)
        variant = self._filter_variant_candidates(variant)
        if variant:
            variant = variant.filtered(lambda rec: not rec.wf_panel_cut_consumable)
        if not variant and template_condition:
            global_domain = [clause for clause in domain if clause != template_condition]
            variant = Product.search(global_domain, limit=1)
            variant = self._filter_variant_candidates(variant)
            if variant:
                variant = variant.filtered(lambda rec: not rec.wf_panel_cut_consumable)
            if variant:
                self._print_colored(
                    f"WF panel cutting: variante encontrada fuera del template {template.display_name if template else ''} → {variant.display_name} (#{variant.id})",
                    tone="detail",
                )
        return bool(variant)

    def _match_template_values(self, attribute, measure, template):
        # Retrieve attribute values on the template that match the target measure.
        base_uom = self._get_base_length_uom()
        values = attribute.value_ids
        if template:
            line = template.attribute_line_ids.filtered(lambda l: l.attribute_id == attribute)[:1]
            if line and line.value_ids:
                values = line.value_ids
        matches = attribute.value_ids.browse()
        for value in values:
            magnitude = self._extract_magnitude(value)
            if magnitude is None:
                continue
            converted_measure = measure
            if value.uom_id and base_uom and value.uom_id.category_id == base_uom.category_id:
                converted_measure = base_uom._compute_quantity(measure, value.uom_id)
                converted_measure = self._apply_rounding(converted_measure, value.uom_id)
            if abs(converted_measure - magnitude) <= self._value_tolerance:
                matches |= value
        return matches

    def _record_cut_usage(self, component, attribute, measure, value):
        component_label = component.display_name or component.data_id or component.id
        attr_label = attribute.display_name or attribute.name
        formatted_measure = self._format_measure_for_attribute(attribute, measure)
        chosen_label = value.name
        note = self._format_cut_usage_note(
            component_label=component_label,
            attribute_label=attr_label,
            value_label=chosen_label,
            measure_label=formatted_measure,
        )
        _logger.info(note)
        self._print_colored(note, tone="warning")
        component_key = component.id if component else id(component)
        pending = self._get_pending_cut_usage()
        bucket = pending.setdefault(component_key, {})
        value_key = value.id if value else attribute.id if attribute else component_key
        bucket[value_key] = {
            "note": note,
            "attribute_id": attribute.id if attribute else False,
            "value_id": value.id if value else False,
            "component_id": component_key,
            "is_cut_instruction": True,
        }

    def _get_lang_prefix(self):
        """Return the two-letter language prefix for the current context/user."""
        lang = self.env.context.get("lang") or self.env.user.lang or "en_US"
        return lang.split("_", 1)[0].lower()

    def _lang_choice(self, es_text, en_text):
        """Choose between Spanish and English text according to the active language."""
        return es_text if self._get_lang_prefix() == "es" else en_text

    def _format_cut_candidate_message(self, candidate_label, delta_value, has_variant, target_label):
        lang = self._get_lang_prefix()
        if lang == "es":
            status = "variante OK" if has_variant else "sin variante"
            template = (
                "WF panel corte: candidato {candidate} (delta={delta}) → {status} para objetivo {target}"
            )
        else:
            status = "variant OK" if has_variant else "no variant"
            template = (
                "WF panel cutting: candidate {candidate} (delta={delta}) → {status} for target {target}"
            )
        return template.format(
            candidate=candidate_label,
            delta=delta_value,
            status=status,
            target=target_label,
        )

    def _format_cut_usage_note(self, component_label, attribute_label, value_label, measure_label):
        lang = self._get_lang_prefix()
        if lang == "es":
            template = "Cortar {component}: usar {attribute} {value} para objetivo {measure}"
        else:
            template = "Cut {component}: use {attribute} {value} for target {measure}"
        return template.format(
            component=component_label,
            attribute=attribute_label,
            value=value_label,
            measure=measure_label,
        )

    def _get_pending_cut_usage(self):
        # Access the per-wizard cache tracking pending cut notes.
        self.ensure_one()
        cache_key = (self.ids and self.ids[0]) or id(self)
        return _CUT_USAGE_CACHE.setdefault(cache_key, {})

    def _flush_cut_usage(self, component, variant):
        # Move pending cut entries into the shared notes with resolved variant info.
        if not component:
            return
        self.ensure_one()
        cache_key = (self.ids and self.ids[0]) or id(self)
        pending = _CUT_USAGE_CACHE.get(cache_key)
        if not pending:
            return
        component_key = component.id if component else id(component)
        entries_map = pending.pop(component_key, {})
        if not entries_map:
            return
        holder = self.env.context.get("_cut_notes_holder")
        for entry in entries_map.values():
            entry["variant_id"] = variant.id if variant else False
            entry["variant_display"] = variant.display_name if variant else False
            entry["component_id"] = component.id
            if isinstance(holder, list):
                holder.append(entry)
        if not pending:
            _CUT_USAGE_CACHE.pop(cache_key, None)

    def _reset_pending_cut_usage(self, component):
        # Clear cached cut usage entries for the given component.
        if not component:
            return
        component_key = component.id if component else id(component)
        pending = self._get_pending_cut_usage()
        pending[component_key] = {}

    def _remember_component_stock_length(self, component, stock_length):
        # Store the chosen stock length (inches) for a component for cut optimisation.
        if not component or stock_length is None:
            return
        self.ensure_one()
        cache_key = (self.ids and self.ids[0]) or id(self)
        bucket = _COMPONENT_STOCK_LENGTH_CACHE.setdefault(cache_key, {})
        bucket[component.id] = stock_length

    def _get_component_stock_length_map(self):
        self.ensure_one()
        cache_key = (self.ids and self.ids[0]) or id(self)
        return _COMPONENT_STOCK_LENGTH_CACHE.get(cache_key, {})

    def _remember_component_variant(self, component, variant):
        # Store the chosen raw variant id for later move creation.
        if not component or not variant:
            return
        self.ensure_one()
        cache_key = (self.ids and self.ids[0]) or id(self)
        bucket = _COMPONENT_VARIANT_CACHE.setdefault(cache_key, {})
        bucket[component.id] = variant.id

    def _get_component_variant_map(self):
        # Retrieve the cache of raw component→variant ids.
        self.ensure_one()
        cache_key = (self.ids and self.ids[0]) or id(self)
        return _COMPONENT_VARIANT_CACHE.setdefault(cache_key, {})

    def _remember_component_intermediate_variant(self, component, variant):
        # Track the intermediate variant produced after the first cut stage.
        if not component or not variant:
            return
        self.ensure_one()
        cache_key = (self.ids and self.ids[0]) or id(self)
        bucket = _INTERMEDIATE_VARIANT_CACHE.setdefault(cache_key, {})
        bucket[component.id] = variant.id

    def _get_component_intermediate_variant_map(self):
        # Retrieve cached intermediate variants for components requiring multi-stage cuts.
        self.ensure_one()
        cache_key = (self.ids and self.ids[0]) or id(self)
        return _INTERMEDIATE_VARIANT_CACHE.setdefault(cache_key, {})

    def _remember_component_assembly_variant(self, component, variant):
        # Cache the post-cut variant id that assembly should consume.
        if not component or not variant:
            return
        self.ensure_one()
        cache_key = (self.ids and self.ids[0]) or id(self)
        bucket = _ASSEMBLY_VARIANT_CACHE.setdefault(cache_key, {})
        bucket[component.id] = variant.id

    def _get_component_assembly_variant_map(self):
        # Retrieve the cached assembly variant mapping for components.
        self.ensure_one()
        cache_key = (self.ids and self.ids[0]) or id(self)
        return _ASSEMBLY_VARIANT_CACHE.setdefault(cache_key, {})

    def _remember_component_width_cut(self, component, required):
        # Store whether a component requires a secondary width cut.
        if not component:
            return
        self.ensure_one()
        cache_key = (self.ids and self.ids[0]) or id(self)
        bucket = _WIDTH_CUT_COMPONENTS.setdefault(cache_key, set())
        if required:
            bucket.add(component.id)
        elif component.id in bucket:
            bucket.discard(component.id)

    def _get_width_cut_components(self):
        # Return the set of component IDs that require a width cut.
        self.ensure_one()
        cache_key = (self.ids and self.ids[0]) or id(self)
        return _WIDTH_CUT_COMPONENTS.setdefault(cache_key, set())

    def _get_cut_consumable_variant_bucket(self):
        # Access the set of cut consumable variants tied to this wizard.
        self.ensure_one()
        cache_key = (self.ids and self.ids[0]) or id(self)
        return _CUT_CONSUMABLE_VARIANTS.setdefault(cache_key, set())

    def _remember_cut_consumable_variant(self, variant):
        # Track cut variants that should be archived after consumption.
        if not variant:
            return
        bucket = self._get_cut_consumable_variant_bucket()
        bucket.add(variant.id)

    def _clear_cut_consumable_variants(self):
        # Reset stored consumable variant ids for this wizard instance.
        self.ensure_one()
        cache_key = (self.ids and self.ids[0]) or id(self)
        _CUT_CONSUMABLE_VARIANTS.pop(cache_key, None)

    def _clear_cut_usage_cache(self):
        # Drop any pending cut instructions stored for this wizard.
        self.ensure_one()
        cache_key = (self.ids and self.ids[0]) or id(self)
        _CUT_USAGE_CACHE.pop(cache_key, None)

    def _clear_component_variant_map(self):
        # Clear stored raw variant matches for this wizard instance.
        self.ensure_one()
        cache_key = (self.ids and self.ids[0]) or id(self)
        _COMPONENT_VARIANT_CACHE.pop(cache_key, None)
        _COMPONENT_STOCK_LENGTH_CACHE.pop(cache_key, None)

    def _clear_component_assembly_variant_map(self):
        # Reset assembly variant cache for this wizard instance.
        self.ensure_one()
        cache_key = (self.ids and self.ids[0]) or id(self)
        _ASSEMBLY_VARIANT_CACHE.pop(cache_key, None)

    def _clear_component_intermediate_variant_map(self):
        # Reset intermediate variant cache for this wizard instance.
        self.ensure_one()
        cache_key = (self.ids and self.ids[0]) or id(self)
        _INTERMEDIATE_VARIANT_CACHE.pop(cache_key, None)

    def _clear_width_cut_components(self):
        # Clear stored width cut flags for this wizard instance.
        self.ensure_one()
        cache_key = (self.ids and self.ids[0]) or id(self)
        _WIDTH_CUT_COMPONENTS.pop(cache_key, None)

    # ── Optimización de cortes y creación de variantes ──────────────────────────

    def _optimize_and_create_cut_variants(self, component_map, stock_length_map):
        """
        Analiza los cortes necesarios, optimiza el agrupamiento (bin packing)
        y crea variantes de producto para cada pieza de stock y sus restos.

        Flujo:
        1. Agrupa componentes por (variante_base, stock_length)
        2. Calcula cuántas piezas de stock se necesitan
        3. Ejecuta optimización con encadenamiento de restos
        4. Crea variantes de producto para cada plan de corte
        5. Crea variantes de resto (leftover >= 50cm) como stock disponible
        6. Descarta restos < 50cm como desperdicio

        Returns:
            dict: {
                'cut_plans': [{...}],
                'total_waste': float,
                'total_leftover': float,
                'efficiency': float,
            }
        """
        self.ensure_one()
        Component = self.env["wf.panel.component"].sudo()
        Product = self.env["product.product"].sudo()

        if not component_map:
            return {
                'cut_plans': [],
                'total_waste': 0.0,
                'total_leftover': 0.0,
                'efficiency': 1.0,
            }

        components = Component.browse(component_map.keys()).sorted(
            key=lambda c: (c.sequence, c.id)
        )
        _logger.info("WF optimizer: components=%d, component_map keys=%s",
                     len(components), list(component_map.keys())[:5])

        # Agrupar por (variant_id, stock_length)
        groups = {}
        for component in components:
            variant_id = component_map.get(component.id)
            if not variant_id:
                _logger.info("WF optimizer: component %d has no variant_id, skipping", component.id)
                continue
            stock_len = stock_length_map.get(component.id, 0.0)
            cut_len = component.data_length or 0.0
            key = (variant_id, stock_len)
            groups.setdefault(key, []).append((component.id, cut_len))
            _logger.info("WF optimizer: component %d -> variant_id=%s stock_len=%s cut_len=%s",
                         component.id, variant_id, stock_len, cut_len)

        _logger.info("WF optimizer: groups=%d", len(groups))
        for (vid, slen), items in groups.items():
            _logger.info("WF optimizer group: variant_id=%s stock_len=%s items=%s",
                         vid, slen, [(c[0], f"{c[1]:.3f}") for c in items])

        all_plans = []
        total_waste = 0.0
        total_leftover = 0.0

        for (variant_id, stock_len), items in groups.items():
            base_variant = Product.browse(variant_id)
            if not base_variant or not base_variant.exists():
                continue

            if not stock_len or stock_len <= 0:
                # Sin longitud definida — un corte por componente
                for comp_id, cut_len in items:
                    cut_label = f"Corte {base_variant.default_code} {cut_len:.1f}\""
                    cut_variant = self._create_cut_variant(
                        base_variant, cut_len, cut_label, comp_id=comp_id
                    )
                    all_plans.append({
                        'variant_id': cut_variant.id,
                        'stock_length': 0.0,
                        'cuts': [(comp_id, cut_len)],
                        'waste': 0.0,
                        'leftover': 0.0,
                        'leftover_variant_id': None,
                    })
                continue

            # Pasar suficientes piezas de stock — el optimizer decide cuántas usa
            # Usamos 1 pieza por cada corte como máximo (nunca se necesitan más)
            stock_lengths = [stock_len] * len(items)

            result = optimize_with_leftover_chain(
                cuts=items,
                stock_lengths=stock_lengths,
                stock_label=base_variant.default_code or base_variant.display_name,
                min_leftover_in=MIN_LEFTOVER_INCHES,
            )

            for plan in result.plans:
                # Calcular desperdicio real: stock_length - suma de cortes
                total_cut_in_plan = sum(cl for _, cl in plan.cuts)
                real_waste = max(0.0, plan.stock_length - total_cut_in_plan)

                # Crear UNA variante por plan (una pieza de stock)
                # que agrupa todos los cortes de este plan
                anchor_comp_id = plan.cuts[0][0] if plan.cuts else None
                comp_labels = []
                for comp_id, cut_len in plan.cuts:
                    comp = Component.browse(comp_id)
                    if comp.exists():
                        comp_labels.append(f"{cut_len:.1f}\"")
                cuts_desc = "+".join(comp_labels)

                cut_label = (
                    f"Corte {base_variant.default_code} "
                    f"{cuts_desc}"
                )
                cut_variant = self._create_cut_variant(
                    base_variant, plan.stock_length, cut_label, comp_id=anchor_comp_id
                )
                self._tag_variant_as_cut_consumable(cut_variant, base_variant)

                plan_dict = {
                    'variant_id': cut_variant.id,
                    'stock_length': plan.stock_length,
                    'cuts': plan.cuts,
                    'waste': real_waste,
                    'leftover': plan.leftover,
                    'leftover_variant_id': None,
                }
                all_plans.append(plan_dict)

                # Crear variante de resto si es aprovechable (>= 50cm)
                if plan.leftover >= MIN_LEFTOVER_INCHES - 1e-6:
                    leftover_label = (
                        f"Resto {base_variant.default_code} "
                        f"{plan.leftover:.1f}\""
                    )
                    leftover_variant = self._create_cut_variant(
                        base_variant, plan.leftover, leftover_label, comp_id=0
                    )
                    self._tag_variant_as_leftover_stock(leftover_variant)

                    self._print_colored(
                        f"WF panel resto creado → {leftover_label} "
                        f"(#{leftover_variant.id})",
                        tone="success",
                    )
                elif plan.waste > 0:
                    self._print_colored(
                        f"WF panel desperdicio → {plan.waste:.3f}\" "
                        f"(descartado por < {MIN_LEFTOVER_INCHES:.1f}\")",
                        tone="warning",
                    )

            total_waste += result.total_waste
            total_leftover += result.total_leftover

        efficiency = (
            total_waste / (total_waste + total_leftover + 1)
            if (total_waste + total_leftover) > 0
            else 1.0
        )

        self._print_colored(
            f"WF panel optimización de cortes → "
            f"{len(all_plans)} planes, "
            f"desperdicio={total_waste:.3f}\", "
            f"restos={total_leftover:.3f}\", "
            f"eficiencia={efficiency:.1%}",
            tone="info",
        )

        return {
            'cut_plans': all_plans,
            'total_waste': total_waste,
            'total_leftover': total_leftover,
            'efficiency': efficiency,
        }

    def _create_cut_variant(self, base_variant, length_in, label, comp_id=None):
        """
        Crea una variante con el atributo Length en el mismo template base.

        Como el atributo Length es 'no_variant', Odoo no genera variantes
        automáticamente. El wizard las crea manualmente.

        Args:
            base_variant: product.product — variante base (materia prima)
            length_in: float — largo en pulgadas
            label: str — ignorado
            comp_id: int or None — ignorado

        Returns:
            product.product — la variante (nueva o existente)
        """
        Product = self.env["product.product"].sudo()
        base_uom = self._get_base_length_uom()

        tmpl = base_variant.product_tmpl_id.sudo()

        # Productos sin stock lengths (OSB) no reciben atributo Length
        if not tmpl.wf_stock_length_ids:
            return base_variant

        # 1. Buscar el atributo Length — primero ver si el template ya tiene uno
        Attribute = self.env["product.attribute"].sudo()
        existing_line = tmpl.attribute_line_ids.filtered(
            lambda l: l.attribute_id.name in ("Length", "Largo")
        )[:1]
        if existing_line:
            length_attr = existing_line.attribute_id
        else:
            length_attr = self._find_attribute(("Length", "Largo"))
            if not length_attr:
                length_attr = Attribute.create({
                    "name": "Length",
                    "create_variant": "no_variant",
                })

        # 2. Si el template NO tiene línea de atributo Length, agregarla
        attr_line = tmpl.attribute_line_ids.filtered(
            lambda l: l.attribute_id == length_attr
        )[:1]

        if not attr_line:
            # Crear el valor PRIMERO
            attr_value = self._create_attribute_value(
                length_attr, length_in, tmpl, base_uom
            )
            # Agregar línea de atributo CON el valor
            tmpl.write({
                "attribute_line_ids": [(0, 0, {
                    "attribute_id": length_attr.id,
                    "value_ids": [(6, 0, [attr_value.id])],
                })]
            })
            attr_line = tmpl.attribute_line_ids.filtered(
                lambda l: l.attribute_id == length_attr
            )[:1]
        else:
            # 3. Buscar o crear el valor de atributo para este largo exacto
            formatted = self._format_measure(length_in)
            unit_label = self._extract_unit_label(length_attr, base_uom)
            value_name = f"{formatted} {unit_label}" if unit_label else formatted
            attr_value = length_attr.value_ids.filtered(
                lambda v: v.name == value_name
            )[:1]
            if not attr_value:
                attr_value = self._create_attribute_value(
                    length_attr, length_in, tmpl, base_uom
                )

        # 5. Buscar el PTAV existente — _create_attribute_value ya lo creó
        cr = self.env.cr
        cr.execute(
            "SELECT id FROM product_template_attribute_value "
            "WHERE attribute_line_id = %s AND product_attribute_value_id = %s",
            (attr_line.id, attr_value.id)
        )
        row = cr.fetchone()
        if row:
            ptav_id = row[0]
        else:
            # Si no existe, crearlo con SQL
            cr.execute(
                "INSERT INTO product_template_attribute_value "
                "(product_tmpl_id, attribute_id, product_attribute_value_id, attribute_line_id, ptav_active) "
                "VALUES (%s, %s, %s, %s, true) RETURNING id",
                (tmpl.id, length_attr.id, attr_value.id, attr_line.id)
            )
            ptav_id = cr.fetchone()[0]

        Ptav = self.env["product.template.attribute.value"].sudo()
        ptav = Ptav.browse(ptav_id)

        # 6. Buscar si ya existe una variante con este PTAV
        # (product_variant_combination es una tabla SQL, no un modelo ORM)
        existing_variant = tmpl.product_variant_ids.filtered(
            lambda v: ptav in v.product_template_attribute_value_ids
        )[:1]

        if existing_variant:
            return existing_variant

        # 7. Crear la variante manualmente
        new_variant = Product.create({
            "product_tmpl_id": tmpl.id,
            "product_template_attribute_value_ids": [(6, 0, [ptav.id])],
        })

        if not new_variant.active:
            new_variant.write({"active": True})

        self._print_colored(
            f"WF panel variante → #{new_variant.id} "
            f"{tmpl.default_code} Length={length_in:.1f}\"",
            tone="detail",
        )
        return new_variant

    def _prepare_raw_moves_optimized(self, production, component_map, stock_length_map):
        """
        Versión optimizada de _prepare_raw_moves que usa el optimizer
        para crear movimientos de stock con variantes de largo exacto
        y restos aprovechables.

        Returns:
            list of move dicts, list of leftover move dicts
        """
        self.ensure_one()
        moves = []
        leftover_moves = []

        if not component_map:
            return moves, leftover_moves

        # Obtener planes de corte optimizados
        _logger.info("WF _prepare_raw_moves_optimized: calling optimizer with component_map=%d stock_length_map=%d",
                     len(component_map), len(stock_length_map))
        opt_result = self._optimize_and_create_cut_variants(
            component_map, stock_length_map
        )
        _logger.info("WF _prepare_raw_moves_optimized: optimizer returned %d plans, waste=%s, leftover=%s",
                     len(opt_result.get('cut_plans', [])),
                     opt_result.get('total_waste', '?'),
                     opt_result.get('total_leftover', '?'))

        seq = 10
        for plan in opt_result['cut_plans']:
            cut_variant = self.env["product.product"].sudo().browse(plan['variant_id'])
            if not cut_variant or not cut_variant.exists():
                continue

            # Construir descripción del corte
            cut_descs = []
            Component = self.env["wf.panel.component"].sudo()
            for comp_id, cut_len in plan['cuts']:
                comp = Component.browse(comp_id)
                if not comp.exists():
                    continue
                parts = []
                if comp.data_length and comp.data_length > 0:
                    parts.append(f"L={comp.data_length:.3f}\"")
                if comp.data_width and comp.data_width > 0:
                    parts.append(f"W={comp.data_width:.3f}\"")
                if comp.data_depth and comp.data_depth > 0:
                    parts.append(f"D={comp.data_depth:.3f}\"")
                piece_label = comp.data_id or ""
                dims = ", ".join(parts)
                cut_descs.append(f"{piece_label} ({dims})" if piece_label else dims)

            cuts_str = " | ".join(cut_descs)
            if plan['stock_length'] > 0:
                description = (
                    f"{cut_variant.display_name} → "
                    f"Cortar: {cuts_str} | "
                    f"Desperdicio: {plan['waste']:.3f}\""
                )
            else:
                description = f"{cut_variant.display_name} → Cortar: {cuts_str}"

            # Crear movimiento de materia prima con la variante de largo exacto
            anchor_comp_id = plan['cuts'][0][0] if plan['cuts'] else False
            base_vals = production._get_move_raw_values(
                cut_variant, 1.0, cut_variant.uom_id
            )
            base_vals.update({
                "name": description,
                "description_picking": description,
                "wf_panel_component_id": anchor_comp_id,
                "sequence": seq,
            })
            moves.append(base_vals)
            seq += 1

            # Crear movimiento de subproducto para el resto aprovechable
            if plan.get('leftover_variant_id'):
                leftover_variant = self.env["product.product"].sudo().browse(
                    plan['leftover_variant_id']
                )
                if leftover_variant and leftover_variant.exists():
                    leftover_label = (
                        f"Resto - {leftover_variant.display_name} "
                        f"({plan['leftover']:.1f}\")"
                    )
                    move_vals = production._get_move_finished_values(
                        leftover_variant.id,
                        1.0,
                        leftover_variant.uom_id.id,
                        False,
                        False,
                        0,
                    )
                    move_vals.update({
                        "name": leftover_label,
                        "description_picking": leftover_label,
                    })
                    leftover_moves.append(move_vals)

        self._print_colored(
            f"WF panel raw moves (optimizado) → {len(moves)} cortes, "
            f"{len(leftover_moves)} restos",
            tone="detail",
        )

        return moves, leftover_moves

    def _archive_consumed_cut_variants(self):
        # Archive consumable cut variants once they no longer hold stock.
        self.ensure_one()
        bucket = set(self._get_cut_consumable_variant_bucket())
        if not bucket:
            return
        Product = self.env["product.product"].with_context(active_test=False).sudo()
        Quant = self.env["stock.quant"].with_context(active_test=False).sudo()
        variants = Product.browse(list(bucket)).exists()
        if not variants:
            self._clear_cut_consumable_variants()
            return
        tolerance = self._value_tolerance
        archived_ids = []
        skipped_ids = []
        for variant in variants:
            qty_available = variant.with_context(active_test=False).qty_available
            if qty_available and qty_available > tolerance:
                skipped_ids.append(variant.id)
                continue
            quant = Quant.search([
                ("product_id", "=", variant.id),
                ("quantity", ">", tolerance),
                ("location_id.usage", "in", ("internal", "transit", "inventory")),
            ], limit=1)
            if quant:
                skipped_ids.append(variant.id)
                continue
            variant.write({"active": False})
            archived_ids.append(variant.id)
        if archived_ids:
            self._print_colored(
                f"WF panel: variantes de corte archivadas → {archived_ids}",
                tone="detail",
            )
        if skipped_ids:
            self._print_colored(
                f"WF panel: variantes de corte conservadas por stock → {skipped_ids}",
                tone="detail",
            )
        self._clear_cut_consumable_variants()

    def _filter_variant_candidates(self, variants):
        # Prefer stock-friendly variants, then manufactured-only ones, and cut consumables last.
        Product = self.env["product.product"]
        if not variants:
            return Product.browse()
        primary_ids = []
        fallback_ids = []
        consumable_ids = []
        for variant in variants:
            if variant.wf_panel_cut_consumable:
                consumable_ids.append(variant.id)
                continue
            target = primary_ids if not variant.wf_panel_manufactured_only else fallback_ids
            target.append(variant.id)
        ordered_ids = list(primary_ids)
        ordered_ids.extend(vid for vid in fallback_ids if vid not in ordered_ids)
        ordered_ids.extend(vid for vid in consumable_ids if vid not in ordered_ids)
        if not ordered_ids:
            return Product.browse()
        return Product.browse(ordered_ids)

    def _mark_variant_manufacturable_only(self, variant):
        # Flag variants created for intermediate use to avoid re-selection later.
        if not variant:
            return
        safe_variant = variant.sudo()
        if safe_variant.wf_panel_manufactured_only:
            return
        safe_variant.write({"wf_panel_manufactured_only": True})
        self._print_colored(
            f"WF panel flag: variante marcada como manufacturable-only → #{safe_variant.id}",
            tone="detail",
        )

    def _tag_variant_as_cut_consumable(self, variant, base_variant):
        # Ensure cut-specific variants stay active for the MO then get archived later.
        if not variant:
            return
        safe_variant = variant.sudo()
        if base_variant and safe_variant.id == base_variant.id:
            return
        updates = {}
        if not safe_variant.wf_panel_manufactured_only:
            updates["wf_panel_manufactured_only"] = True
        if not safe_variant.wf_panel_cut_consumable:
            updates["wf_panel_cut_consumable"] = True
        if safe_variant.wf_panel_leftover_stock:
            updates["wf_panel_leftover_stock"] = False
        if not safe_variant.active:
            updates["active"] = True
        if updates:
            safe_variant.write(updates)
        self._remember_cut_consumable_variant(safe_variant)

    def _tag_variant_as_leftover_stock(self, variant):
        # Keep leftover variants visible and reusable in future panel runs.
        if not variant:
            return
        safe_variant = variant.sudo()
        updates = {}
        if safe_variant.wf_panel_manufactured_only:
            updates["wf_panel_manufactured_only"] = False
        if not safe_variant.wf_panel_leftover_stock:
            updates["wf_panel_leftover_stock"] = True
        if safe_variant.wf_panel_cut_consumable:
            updates["wf_panel_cut_consumable"] = False
        if not safe_variant.active:
            updates["active"] = True
        if updates:
            safe_variant.write(updates)

    def _select_variant_value(self, attribute, measure, values):
        # Choose the most suitable attribute value based on measure delta.
        if not values:
            return False
        base_uom = self._get_base_length_uom()
        best_value = False
        best_delta = None
        best_magnitude = None
        for value in values:
            magnitude = self._extract_magnitude(value)
            if magnitude is None:
                continue
            compare_measure = measure
            if value.uom_id and base_uom and value.uom_id.category_id == base_uom.category_id:
                compare_measure = base_uom._compute_quantity(measure, value.uom_id)
                compare_measure = self._apply_rounding(compare_measure, value.uom_id)
            delta = magnitude - compare_measure
            if delta + self._value_tolerance < 0:
                continue
            if (
                best_value is None
                or best_value is False
                or delta < best_delta - self._value_tolerance
                or (
                    abs(delta - best_delta) <= self._value_tolerance
                    and (best_magnitude is None or magnitude < best_magnitude)
                )
            ):
                best_value = value
                best_delta = delta
                best_magnitude = magnitude
        return best_value or values[:1]

    def _log_variant_attribute_state(self, variant, attribute, measure, reason, component=None, candidate_values=None):
        # Emit detailed diagnostics when a variant fails attribute validation.
        variant_label = f"{variant.display_name} (#{variant.id})"
        attr_label = attribute.display_name or attribute.name
        measure_label = self._format_measure_for_attribute(attribute, measure)
        component_label = component.display_name if component else None
        candidate_labels = ", ".join(val.name for val in (candidate_values or [])) or "—"
        variant_pairs = self._format_variant_attribute_pairs(variant)
        if self._get_lang_prefix() == "es":
            header = "WF panel discrepancia de variante:"
            lines = [
                header,
                f"  variante: {variant_label}",
                f"  atributo: {attr_label}",
                f"  objetivo: {measure_label}",
                f"  motivo: {reason}",
                f"  valores del atributo en variante: {candidate_labels}",
                f"  atributos del producto: {variant_pairs}",
            ]
            if component_label:
                lines.insert(1, f"  componente: {component_label}")
        else:
            header = "WF panel variant mismatch:"
            lines = [
                header,
                f"  variant: {variant_label}",
                f"  attribute: {attr_label}",
                f"  target: {measure_label}",
                f"  reason: {reason}",
                f"  attribute values on variant: {candidate_labels}",
                f"  product attributes: {variant_pairs}",
            ]
            if component_label:
                lines.insert(1, f"  component: {component_label}")
        message = "\n".join(lines)
        _logger.info(message)
        self._print_colored(message, tone="warning")
        return message

    def _format_variant_attribute_pairs(self, variant):
        # Represent a variant's attribute/value pairs as readable text.
        pairs = []
        for ptav in variant.product_template_attribute_value_ids:
            attribute = ptav.attribute_id.display_name or ptav.attribute_id.name
            value_name = ptav.product_attribute_value_id.name
            pairs.append(f"{attribute}={value_name}")
        return ", ".join(pairs) if pairs else "—"

    def _log_candidate_variants(self, variants, source, component=None):
        # Output a summary of candidate variants considered for matching.
        if not variants:
            return
        header = self._lang_choice(
            "WF panel search: productos encontrados por {source} ({count})",
            "WF panel search: variants found via {source} ({count})",
        ).format(source=source, count=len(variants))
        self._print_colored(header, tone="success")
        _logger.info(header)
        seen_templates = set()
        for variant in variants[:10]:
            template = variant.product_tmpl_id
            if template and template not in seen_templates:
                seen_templates.add(template)
                self._log_template_variant_snapshot(template, component)
            detail = self._format_variant_attribute_pairs(variant)
            no_detail = self._lang_choice("—", "—")  # keep dash regardless of language
            line = self._lang_choice(
                "  - #{vid}: {name} → {detail}",
                "  - #{vid}: {name} → {detail}",
            ).format(vid=variant.id, name=variant.display_name, detail=detail or no_detail)
            if component:
                comp_label = component.display_name or component.id
                suffix = self._lang_choice(
                    " (para {label})",
                    " (for {label})",
                ).format(label=comp_label)
                line += suffix
            self._print_colored(line, tone="detail")
            _logger.info(line)

    def _ensure_value_on_template(self, attribute, value, template):
        # Make sure a template has the attribute value linked so variants exist.
        template = template.sudo()
        attribute = attribute.sudo()
        if value not in attribute.value_ids:
            attribute.value_ids |= value
        line = template.attribute_line_ids.filtered(lambda l: l.attribute_id == attribute)
        if line:
            if value not in line.value_ids:
                line.value_ids = [(4, value.id)]
        else:
            template.write({
                "attribute_line_ids": [
                    (0, 0, {
                        "attribute_id": attribute.id,
                        "value_ids": [(6, 0, value.ids)],
                    })
                ]
            })
        template._create_variant_ids()
        return value

    def _create_attribute_value(self, attribute, measure, template, base_uom):
        # Create a new attribute value in the correct UoM when required.
        template = template.sudo()
        attribute = attribute.sudo()
        existing_uom = attribute.value_ids.filtered(lambda v: v.uom_id)[:1].uom_id
        reference_uom = self._get_reference_uom()
        target_uom = (
            getattr(attribute, "uom_id", False)
            or existing_uom
            or reference_uom
            or base_uom
        )
        if not target_uom:
            raise UserError(
                _("No se puede determinar la unidad de medida para el atributo '%s'.")
                % attribute.display_name
            )
        display_measure = measure
        if base_uom and target_uom.category_id == base_uom.category_id:
            display_measure = base_uom._compute_quantity(measure, target_uom)
        display_measure = self._apply_rounding(display_measure, target_uom)
        formatted = self._format_measure(display_measure)
        unit_label = self._extract_unit_label(attribute, target_uom)
        value_vals = {
            "name": f"{formatted} {unit_label}" if unit_label else formatted,
            "attribute_id": attribute.id,
            "uom_id": target_uom.id,
        }
        existing = attribute.value_ids.filtered(lambda v: v.name == value_vals["name"])[:1]
        if existing:
            line = template.attribute_line_ids.filtered(lambda l: l.attribute_id == attribute)[:1]
            if line:
                # Usar SQL directo para evitar _create_variant_ids()
                self.env.cr.execute(
                    "INSERT INTO product_attribute_value_product_template_attribute_line_rel "
                    "(product_template_attribute_line_id, product_attribute_value_id) "
                    "VALUES (%s, %s) ON CONFLICT DO NOTHING",
                    (line.id, existing.id)
                )
            else:
                template.write({
                    "attribute_line_ids": [
                        (0, 0, {
                            "attribute_id": attribute.id,
                            "value_ids": [(6, 0, existing.ids)],
                        })
                    ]
                })
            return existing
        new_value = self.env["product.attribute.value"].sudo().create(value_vals)
        line = template.attribute_line_ids.filtered(lambda l: l.attribute_id == attribute)[:1]
        if line:
            # Usar SQL directo para evitar _create_variant_ids()
            self.env.cr.execute(
                "INSERT INTO product_attribute_value_product_template_attribute_line_rel "
                "(product_template_attribute_line_id, product_attribute_value_id) "
                "VALUES (%s, %s) ON CONFLICT DO NOTHING",
                (line.id, new_value.id)
            )
        else:
            template.write({
                "attribute_line_ids": [
                    (0, 0, {
                        "attribute_id": attribute.id,
                        "value_ids": [(6, 0, new_value.ids)],
                    })
                ]
            })
        return new_value

    def _extract_unit_label(self, attribute, target_uom):
        # Deduce a suitable string label for the unit on attribute values.
        existing = attribute.value_ids.filtered(lambda v: v.uom_id == target_uom)[:1]
        if existing:
            magnitude, unit_label = self._split_value_label(existing.name)
            if unit_label:
                return unit_label
        symbol = getattr(target_uom, "symbol", False)
        if symbol:
            return symbol
        return target_uom.display_name or target_uom.name

    @staticmethod
    def _split_value_label(value_name):
        # Split an attribute value label into magnitude and unit tokens.
        if not value_name:
            return None, None
        parts = value_name.strip().split()
        if len(parts) < 2:
            return None, None
        try:
            magnitude = float(parts[0].replace(",", "."))
        except ValueError:
            magnitude = None
        unit = " ".join(parts[1:]).strip()
        return magnitude, unit

    def _format_measure_for_attribute(self, attribute, measure):
        # Present a measurement using the attribute's preferred unit.
        base_uom = self._get_base_length_uom()
        target_uom = (
            getattr(attribute, "uom_id", False)
            or attribute.value_ids.filtered(lambda v: v.uom_id)[:1].uom_id
            or self._get_reference_uom()
            or base_uom
        )
        if not target_uom:
            return self._format_measure(measure)
        display_measure = measure
        if base_uom and target_uom.category_id == base_uom.category_id:
            display_measure = base_uom._compute_quantity(measure, target_uom)
        display_measure = self._apply_rounding(display_measure, target_uom)
        unit_label = self._extract_unit_label(attribute, target_uom) if target_uom else ""
        formatted = self._format_measure(display_measure)
        return f"{formatted} {unit_label}".strip()

    def _debug_component_search(self, component, templates, reason):
        # Dump verbose diagnostics about a failed component match.
        component_label = component.display_name or component.data_id or component.id
        terms = self._extract_component_terms(component)[:10]
        measurements = {}
        for field_name, attr_names in self._component_attribute_map:
            value = getattr(component, field_name, None)
            if not value:
                continue
            names = attr_names if isinstance(attr_names, (list, tuple)) else (attr_names,)
            measurements[field_name] = {
                "value": value,
                "names": tuple(n for n in names if n),
            }

        product_lines = []
        for tmpl in templates[:5]:
            tmpl = tmpl.sudo()
            line = f"  - {tmpl.display_name}"
            if getattr(tmpl, "default_code", False):
                line += f" [{tmpl.default_code}]"
            product_lines.append(line)
            attr_lines = []
            for attr_line in tmpl.attribute_line_ids[:5]:
                attr_name = attr_line.attribute_id.display_name or attr_line.attribute_id.name
                value_names = [val.name for val in attr_line.value_ids[:6]]
                attr_lines.append(f"      {attr_name}: {', '.join(value_names) if value_names else '—'}")
            if attr_lines:
                product_lines.extend(attr_lines)

        lines = [
            f"WF panel search failure:",
            f"  componente: {component_label}",
            f"  motivo: {reason or 'Sin coincidencia'}",
            f"  términos: {', '.join(terms) or '—'}",
            "  medidas buscadas:" if measurements else "",
        ]
        for entry in measurements.values():
            labels = "/".join(entry["names"]) if entry["names"] else "?"
            lines.append(f"    {labels}: {entry['value']}")
        detailed_products = []
        for tmpl in templates[:5]:
            detailed_products.extend(self._describe_template_fit(tmpl, measurements))
        if detailed_products:
            lines.append("  productos considerados:")
            lines.extend(detailed_products)
        message = "\n".join([line for line in lines if line])
        _logger.info(message)
        self._print_colored(message, tone="error")

    def _print_colored(self, text, tone="info"):
        # Print helper honoring terminal colors unless disabled in context.
        if text is None:
            return
        tone_code = self._color_codes.get(tone)
        if not tone_code or self.env.context.get("no_color_output"):
            print(text)
            return
        print(f"{tone_code}{text}{self._color_reset}")

    def _describe_template_fit(self, template, measurements):
        # Summarize how well a template's attributes match required measures.
        template = template.sudo()
        prefix = f"  - {template.display_name}"
        if getattr(template, "default_code", False):
            prefix += f" [{template.default_code}]"
        lines = [prefix]
        matched_attrs = []
        missing_attrs = []
        for attr_line in template.attribute_line_ids:
            attribute = attr_line.attribute_id
            attr_name = attribute.display_name or attribute.name
            desired_measure = self._get_measure_for_attribute(attribute, measurements)
            compare_lines = []
            for value in attr_line.value_ids:
                compare_lines.append(f"      {value.name}")
            if compare_lines:
                lines.extend(compare_lines)
            if desired_measure is not None:
                formatted_measure = self._format_measure_for_attribute(attribute, desired_measure)
                matches = attr_line.value_ids.filtered(lambda val: self._is_measure_match(val, desired_measure))
                if matches:
                    matched_attrs.append(f"    ✓ {attr_name} contiene {formatted_measure}")
                else:
                    missing_attrs.append(f"    ✗ {attr_name} requiere {formatted_measure}")
        lines.extend(matched_attrs)
        lines.extend(missing_attrs)
        return lines

                # # Antes de crear variantes, verificar si ya existe una con los combination_indices requeridos
                # usable_variants = self._filter_variant_candidates(template.product_variant_ids)
                # if required_values_groups:
                #     for variant in usable_variants:
                #         variant_value_ids = variant.product_template_attribute_value_ids.product_attribute_value_id
                #         if all(variant_value_ids & value_group for value_group in required_values_groups):
                #             self._finalize_variant_selection(variant, component)
                #             return variant, None
                # # Si no existe, entonces crear variantes

    def _get_measure_for_attribute(self, attribute, measurements):
        # Extract the requested measure for an attribute from the collected map.
        attr_names = {
            attribute.name.lower(),
            attribute.display_name.lower() if attribute.display_name else attribute.name.lower(),
        }
        for entry in measurements.values():
            names = [name.lower() for name in entry["names"]]
            if attr_names.intersection(names):
                return entry["value"]
        return None

    def _is_measure_match(self, value, measure):
        # Check if an attribute value equals the target measure within tolerance.
        attribute = value.attribute_id
        base_uom = self._get_base_length_uom()
        magnitude = self._extract_magnitude(value)
        if magnitude is None:
            return False
        converted_measure = measure
        if value.uom_id and base_uom and value.uom_id.category_id == base_uom.category_id:
            converted_measure = base_uom._compute_quantity(measure, value.uom_id)
            converted_measure = self._apply_rounding(converted_measure, value.uom_id)
        return abs(converted_measure - magnitude) <= self._value_tolerance

    def _is_measure_sufficient(self, value, measure):
        # Validate whether an attribute value is long enough for the measure.
        base_uom = self._get_base_length_uom()
        magnitude = self._extract_magnitude(value)
        if magnitude is None:
            return False
        converted_measure = measure
        if value.uom_id and base_uom and value.uom_id.category_id == base_uom.category_id:
            converted_measure = base_uom._compute_quantity(measure, value.uom_id)
            converted_measure = self._apply_rounding(converted_measure, value.uom_id)
        return magnitude + self._value_tolerance >= converted_measure

    def _variant_matches_component(self, variant, component):
        # Validate that a candidate variant satisfies all component attributes.
        if component:
            self._reset_pending_cut_usage(component)
        reasons = []
        positive_checks = 0
        for field_name, attribute_names in self._component_attribute_map:
            measure = getattr(component, field_name, None)
            if not measure:
                continue
            attribute = self._find_attribute(attribute_names)
            if not attribute:
                continue
            variant_ptavs = variant.product_template_attribute_value_ids.filtered(
                lambda ptav: ptav.attribute_id == attribute
            )
            if not variant_ptavs:
                reason = "sin valores para el atributo"
                if field_name == "data_length":
                    msg = self._log_variant_attribute_state(
                        variant,
                        attribute,
                        measure,
                        reason=reason,
                        component=component,
                    )
                    reasons.append(msg)
                    return False, "; ".join(filter(None, reasons))
                self._log_variant_attribute_state(
                    variant,
                    attribute,
                    measure,
                    reason=f"{reason}, se omite",
                    component=component,
                )
                continue
            variant_values = variant_ptavs.mapped("product_attribute_value_id")
            numeric_values = variant_values.filtered(lambda val: self._extract_magnitude(val) is not None)
            if numeric_values:
                exact_matches = numeric_values.filtered(lambda val: self._is_measure_match(val, measure))
                matching_candidates = exact_matches
                if not matching_candidates and field_name in self._cuttable_fields:
                    matching_candidates = numeric_values.filtered(lambda val: self._is_measure_sufficient(val, measure))
                if not matching_candidates:
                    msg = self._log_variant_attribute_state(
                        variant,
                        attribute,
                        measure,
                        reason="medida no compatible",
                        component=component,
                        candidate_values=variant_values,
                    )
                    reasons.append(msg)
                    return False, "; ".join(filter(None, reasons))
                if field_name in self._cuttable_fields and component:
                    chosen_value = self._select_variant_value(attribute, measure, matching_candidates)
                    if chosen_value:
                        self._record_cut_usage(component, attribute, measure, chosen_value)
                positive_checks += 1
                continue
            acceptable_values = self._find_attribute_values(
                attribute,
                measure,
                field_name=field_name,
                component=component,
            )
            if acceptable_values:
                if not (variant_values & acceptable_values):
                    msg = self._log_variant_attribute_state(
                        variant,
                        attribute,
                        measure,
                        reason="valores categóricos sin coincidencia",
                        component=component,
                        candidate_values=variant_values,
                    )
                    reasons.append(msg)
                    return False, "; ".join(filter(None, reasons))
                positive_checks += 1
                continue
            # If the variant did not have numeric values and no acceptable value was found,
            # fall back to a direct attribute search to avoid false positives.
            msg = self._log_variant_attribute_state(
                variant,
                attribute,
                measure,
                reason="no se encontraron valores aceptables",
                component=component,
                candidate_values=variant_values,
            )
            reasons.append(msg)
            return False, "; ".join(filter(None, reasons))
        if positive_checks > 0:
            return True, None
        return False, _("Variante sin atributos comparables")

    def _identify_base_templates(self, component):
        # Gather candidate templates using name hints from the component.
        Template = self.env["product.template"]
        terms = self._extract_component_terms(component)
        templates = Template.browse()
        component_key = self._prepare_component_core_key(component)
        for term in terms:
            if not term:
                continue
            domain = [
                "|",
                ("default_code", "ilike", term),
                ("name", "ilike", term),
            ]
            found = Template.search(domain, limit=5)
            if found:
                if component_key and len(component_key) >= _CORE_MIN_MATCH:
                    filtered = Template.browse()
                    for template in found:
                        candidate_key = self._prepare_product_core_key(template.display_name or template.name or "")
                        if self._core_strings_match(component_key, candidate_key):
                            filtered |= template
                    if filtered:
                        templates |= filtered
                else:
                    templates |= found
        return templates

    def _extract_component_name_hints(self, component):
        # Produce potential search strings derived from the component code fields.
        seen = set()
        hints = []
        for field in self._component_code_fields:
            raw = getattr(component, field, None)
            if not raw:
                continue
            adjusted = self._prepare_code_field(raw)
            self._print_colored(
                f"WF panel search: valor base de {field} → '{adjusted}'",
                tone="detail",
            )
            normalized = adjusted.strip()
            if normalized and normalized not in seen:
                hints.append(normalized)
                seen.add(normalized)
        return hints

    def _expand_name_candidates_from_raw(self, raw):
        # Expand a raw identifier into multiple normalized search variants.
        normalized = raw.replace("_", " ").replace("-", " ").strip()
        normalized = re.sub(r"\s+", " ", normalized)
        return [normalized] if normalized else []

    def _compute_core_label_variations(self, raw):
        # Build canonicalized label options emphasizing numeric tokens.
        tokens = [token for token in raw.replace("-", "_").split("_") if token]
        if not tokens:
            return []
        while tokens and not any(char.isdigit() for char in tokens[0]):
            tokens.pop(0)
        if not tokens:
            return []
        result_tokens = []
        stop = False
        for token in tokens:
            if stop:
                break
            result_tokens.append(token)
            if token.isdigit() and len(result_tokens) >= 2 and result_tokens[-2].lower() == "no":
                stop = True
        if not result_tokens:
            return []
        filtered_tokens = [tok for tok in result_tokens if tok.lower() not in self._component_stop_tokens]
        if filtered_tokens:
            result_tokens = filtered_tokens
        variations = set()
        base_tokens = [self._normalize_core_token(tok) for tok in result_tokens]
        base = self._join_tokens(base_tokens)
        if base:
            variations.add(base)
        if "No " in base and "No. " not in base:
            variations.add(base.replace("No ", "No. "))
        if "No. " in base:
            variations.add(base.replace("No. ", "No "))
        fraction, consumed = self._convert_fraction_tokens(result_tokens)
        if fraction:
            rest_tokens = result_tokens[consumed:]
            normalized_rest = [self._normalize_core_token(tok) for tok in rest_tokens]
            frac_base = self._join_tokens([fraction] + normalized_rest)
            if frac_base:
                variations.add(frac_base)
                variations.add(frac_base.replace("\"", " in"))
        # Generate trimmed variations removing trailing tokens that are purely numeric or psuedo-length markers
        for trim in range(len(result_tokens), 2, -1):
            prefix = result_tokens[:trim]
            if not prefix:
                continue
            if not any(char.isdigit() for char in "".join(prefix)):
                continue
            normalized_prefix = [self._normalize_core_token(tok) for tok in prefix]
            prefix_base = self._join_tokens(normalized_prefix)
            if prefix_base:
                variations.add(prefix_base)
                if "No " in prefix_base:
                    variations.add(prefix_base.replace("No ", "No. "))
                    variations.add(prefix_base.replace("No ", "No."))
        # Compact variants with No. and fractional spacing
        extra_variations = set()
        for item in variations:
            if "No. " in item:
                extra_variations.add(item.replace("No. ", "No."))
            if "No " in item:
                extra_variations.add(item.replace("No ", "No."))
        variations.update(extra_variations)
        return [variation for variation in variations if variation]

    def _generate_sliding_candidates(self, raw, min_length=10):
        # Generate longer substring combinations to widen fuzzy searches.
        texts = set()
        base = raw.replace("_", " ").replace("-", " ")
        base = re.sub(r"\s+", " ", base).strip()
        if base:
            if self._has_minimum_alnum_count(base):
                texts.add(base)
            stripped = self._strip_stop_tokens(base)
            if stripped and self._has_minimum_alnum_count(stripped):
                texts.add(stripped)
        substrings = set()
        for text in texts:
            tokens = text.split()
            if not tokens:
                continue
            for start in range(len(tokens)):
                parts = []
                for end in range(start, len(tokens)):
                    parts.append(tokens[end])
                    candidate = " ".join(parts).strip()
                    if len(candidate) < min_length:
                        continue
                    if not self._has_minimum_alnum_count(candidate):
                        continue
                    substrings.add(candidate)
                    if "No " in candidate and "No. " not in candidate:
                        substrings.add(candidate.replace("No ", "No. "))
                    if "No. " in candidate:
                        substrings.add(candidate.replace("No. ", "No "))
        return list(substrings)

    def _generate_similarity_segments(self, text, min_letters=10):
        # Prepare segments used for difflib similarity matching.
        if not text:
            return []
        segments = set()
        tokens = [tok.strip() for tok in text.split() if tok.strip()]
        if not tokens:
            return []
        for start in range(len(tokens)):
            for end in range(start + 1, len(tokens) + 1):
                segment = " ".join(tokens[start:end]).strip()
                if not segment:
                    continue
                if not self._has_minimum_alnum_count(segment, minimum=min_letters):
                    continue
                segments.add(segment)
        ordered = sorted(
            segments,
            key=lambda item: (-len(re.sub(r"[^A-Za-z0-9]", "", item)), -len(item), item.lower()),
        )
        return ordered

    @staticmethod
    def _normalize_core_token(token):
        # Sanitize token casing and spacing for core label processing.
        cleaned = token.replace("-", " ")
        cleaned = cleaned.replace("__", "_")
        return cleaned.replace("X", "x")

    @staticmethod
    def _extract_inner_segment(text):
        # Keep the meaningful middle portion of a multi-token label.
        """Keep the portion after the first two spaces and before the final space."""
        if not text:
            return ""
        tokens = text.split()
        if len(tokens) < 4:
            return text
        inner_tokens = tokens[2:-1]
        return " ".join(inner_tokens) if inner_tokens else text

    @staticmethod
    def _prepare_code_field(value):
        # Normalize an identifier into a trimmed search-friendly string.
        # Use the original value without modification for matching
        return value or ""

    def _prepare_component_core_key(self, component):
        # Build the core comparison key from a component code.
        raw = getattr(component, "data_id", None) or getattr(component, "display_name", None) or getattr(component, "name", None) or ""
        # Use the original raw value for core key matching
        return (raw or "").upper()

    @staticmethod
    def _prepare_product_core_key(label):
        # Normalize a product name by removing spaces and separators.
        text = re.sub(r"[_-]+", " ", label or "").strip()
        text = re.sub(r"\s+", "", text)
        return text.upper()

    @staticmethod
    def _core_strings_match(component_key, candidate_key, min_chars=_CORE_MIN_MATCH):
        # Require a contiguous overlap of at least min_chars between keys.
        if not component_key or not candidate_key:
            return False
        if len(component_key) < min_chars or len(candidate_key) < min_chars:
            return False
        matcher = difflib.SequenceMatcher(None, component_key, candidate_key)
        match = matcher.find_longest_match(0, len(component_key), 0, len(candidate_key))
        return match.size >= min_chars

    def _derive_template_name(self, component):
        # Trim the component identifier to match a product template name.
        raw = getattr(component, "data_id", None) or getattr(component, "display_name", None) or getattr(component, "name", None) or ""
        # Use the original raw value for template name matching
        return raw

    def _find_template_by_exact_name(self, component):
        # Locate a product template whose name matches the derived component label.
        Template = self.env["product.template"]
        piece_name = getattr(component, "data_id", None) or getattr(component, "display_name", None) or getattr(component, "name", None) or ""
        if not piece_name:
            return Template.browse(), _("Nombre base de producto no derivado")
        # Sustituir _ por espacios en el nombre de la pieza
        piece_name_cmp = piece_name.replace("_", " ")
        self._print_colored(
            f"WF panel search: nombre de pieza → '{piece_name_cmp}'",
            tone="info",
        )
        template = Template.search([], limit=1000)
        for t in template:
            prod_name_cmp = (t.name or "").replace("_", " ")
            prod_code_cmp = (t.default_code or "").replace("_", " ")
            if prod_name_cmp and prod_name_cmp in piece_name_cmp:
                return t, None
            if prod_code_cmp and prod_code_cmp in piece_name_cmp:
                return t, None
        return Template.browse(), _("Producto no registrado o no incluido en el nombre de la pieza")

    def _get_variant_length(self, variant, attribute):
        # Extract the numeric length magnitude for a given variant.
        if not variant or not attribute:
            return None
        ptav = variant.product_template_attribute_value_ids.filtered(
            lambda record: record.attribute_id == attribute
        )[:1]
        if not ptav:
            return None
        return self._extract_magnitude(ptav.product_attribute_value_id)

    def _select_variant_by_length(self, template, component):
        # Choose the variant whose length best covers the component requirement.
        template = template.sudo()
        target_length = getattr(component, "data_length", 0.0) or 0.0
        length_attr = next(
            (self._find_attribute(mapping[1]) for mapping in self._component_attribute_map if mapping[0] == "data_length"),
            False,
        )
        if target_length <= 0 or not length_attr:
            reason = _("Componente %s sin medida de largo") % (component.display_name or component.data_id or component.id)
            return False, reason
        primary_candidates = []
        secondary_candidates = []
        for variant in template.product_variant_ids:
            if variant.wf_panel_cut_consumable:
                self._print_colored(
                    self._lang_choice(
                        "WF panel selección: se omite variante consumible de corte → #{vid}",
                        "WF panel selection: skip cut-consumable variant → #{vid}",
                    ).format(vid=variant.id),
                    tone="detail",
                )
                continue
            matches, reason = self._variant_matches_component(variant, component)
            # Get the Length attribute value record so we can use _is_measure_sufficient,
            # which properly converts target_length to the value's UOM before comparing.
            ptav_len = variant.product_template_attribute_value_ids.filtered(
                lambda r: r.attribute_id == length_attr
            )[:1]
            if not ptav_len:
                continue
            len_val = ptav_len.product_attribute_value_id
            magnitude = self._extract_magnitude(len_val)
            if magnitude is None:
                continue
            sufficient = self._is_measure_sufficient(len_val, target_length)
            if matches:
                if sufficient:
                    primary_candidates.append((magnitude, variant.wf_panel_manufactured_only, variant))
                else:
                    secondary_candidates.append((magnitude, variant.wf_panel_manufactured_only, variant))
            else:
                # Variant failed attribute matching, but if it is long enough (only
                # width/depth mismatch), still treat as secondary so we can fall back.
                if sufficient:
                    secondary_candidates.append((magnitude, variant.wf_panel_manufactured_only, variant))
        if primary_candidates:
            primary_candidates.sort(key=lambda item: (item[0], item[1]))
            best_magnitude, _flag, best_variant = primary_candidates[0]
            self._log_template_variant_snapshot(template, component)
            # Re-run attribute validation to rebuild cut usage caches before finalizing.
            self._variant_matches_component(best_variant, component)
            self._finalize_variant_selection(best_variant, component)
            self._print_colored(
                f"WF panel selección exacta → #{best_variant.id} {best_variant.display_name} (largo={best_magnitude:.3f})",
                tone="success",
            )
            return best_variant, None
        self._log_template_variant_snapshot(template, component)
        if secondary_candidates:
            # Sort: prefer shorter stock (less waste), prefer non-manufactured-only
            secondary_candidates.sort(key=lambda item: (item[0], item[1]))
            best_magnitude, _flag, best_variant = secondary_candidates[0]
            # All secondary candidates were pre-filtered to be length-sufficient.
            self._finalize_variant_selection(best_variant, component)
            self._print_colored(
                f"WF panel selección fallback → #{best_variant.id} {best_variant.display_name} (largo={best_magnitude:.3f})",
                tone="warning",
            )
            return best_variant, None
        reason = _("Sin variantes compatibles para %s") % (template.display_name or template.name)
        return False, reason

    @staticmethod
    def _join_tokens(tokens):
        # Join tokens collapsing redundant whitespace.
        text = " ".join(tokens)
        return re.sub(r"\s+", " ", text).strip()

    @staticmethod
    def _convert_fraction_tokens(tokens):
        # Interpret fraction-like tokens such as 1 1/2 for label normalization.
        if len(tokens) >= 2 and tokens[0].isdigit():
            match = re.match(r"(\d+)(?:X0|X|)$", tokens[1], re.IGNORECASE)
            if match:
                denominator = match.group(1)
                fraction = f"{tokens[0]}/{denominator}\""
                return fraction, 2
        return None, 0

    def _strip_stop_tokens(self, text):
        # Remove stop words configured for component code parsing.
        if not text:
            return ""
        if not self._component_stop_tokens:
            return text
        tokens = [token for token in text.split() if token.lower() not in self._component_stop_tokens]
        return " ".join(tokens).strip()

    def _extract_component_terms(self, component):
        # Build prioritized term list used for name-based variant searches.
        terms = []
        sources = [component.data_id or ""]
        for raw in sources:
            if not raw:
                continue
            cleaned = re.sub(r"[^0-9A-Za-z_]+", "_", raw)
            tokens = [token for token in cleaned.split("_") if token]
            if not tokens:
                continue
            for start in range(len(tokens)):
                tail = tokens[start:]
                if not tail:
                    continue
                slug = "_".join(tail)
                if not any(char.isdigit() for char in slug):
                    continue
                for variant in self._generate_term_variations(slug):
                    if (
                        variant
                        and len(variant) >= 3
                        and self._has_minimum_alnum_count(variant)
                        and variant not in terms
                    ):
                        terms.append(variant)
        return terms

    @staticmethod
    def _normalize_inch_tokens(text):
        # Convert occurrences like 1X0 into inch notation 1" for matching.
        if not text:
            return text
        pattern = re.compile(r"(?i)(\d)X0(?=\b|[^0-9A-Za-z])")
        return pattern.sub(lambda match: f"{match.group(1)}\"", text)

    @staticmethod
    def _generate_term_variations(slug):
        # Produce textual variations (underscores, spaces, apostrophes) for a slug.
        variations = {slug}
        variations.add(slug.replace("_", " "))
        variations.add(slug.replace("_", "-"))
        if "X0" in slug:
            # variations.add(slug.replace("X", "x"))
            variations.add(slug.replace("X0", '"'))
        space_slug = slug.replace("_", " ")
        apost = re.sub(r"(\d)$", r"\1'", space_slug)
        variations.add(apost)
        normalized_variations = set()
        for item in variations:
            normalized_variations.add(item)
            normalized_variations.add(WFPanelManufacturingWizard._normalize_inch_tokens(item))
        final_variations = []
        for item in normalized_variations:
            candidate = item.strip()
            if candidate and candidate not in final_variations:
                final_variations.append(candidate)
        return final_variations

    @staticmethod
    def _has_minimum_alnum_count(text, minimum=10):
        # Check that a string has enough alphanumeric characters to be useful.
        if not text:
            return False
        letters = re.sub(r"[^A-Za-z0-9]+", "", text)
        return len(letters) >= minimum

    @staticmethod
    def _format_measure(value):
        # Format numeric measures with consistent precision for display.
        rounded = float_round(value, precision_digits=3)
        text = ("%0.3f" % rounded).rstrip("0").rstrip(".")
        return text or "0"

    @staticmethod
    def _apply_rounding(value, uom):
        # Apply the UoM rounding to a floating-point measurement.
        if not uom or not getattr(uom, "rounding", 0.0):
            return value
        rounding = uom.rounding
        quotient = value / rounding
        rounded_value = round(quotient) * rounding
        return float_round(rounded_value, precision_digits=6)

    def _extract_magnitude(self, value):
        # Pull the numeric magnitude from an attribute value name.
        if not value:
            return None
        name = (value.name or "").strip()
        return self._parse_measure_string(name)

    def _parse_measure_string(self, text):
        # Parse various textual measurement formats into a float value.
        if not text:
            return None
        cleaned = str(text).strip()
        cleaned = cleaned.replace("\u2033", '"')
        cleaned = cleaned.replace("\u2032", "")
        cleaned = cleaned.replace("\u201D", '"').replace("\u201C", '"')
        cleaned = cleaned.replace("\u2019", "'")
        cleaned = re.sub(r"\(.*?\)", " ", cleaned)
        cleaned = re.sub(r"(?<=\d)-(?!\s)(?=\d)", " ", cleaned)
        cleaned = cleaned.replace(",", ".")
        cleaned = cleaned.replace('"', " ")
        cleaned = cleaned.replace("'", " ")
        cleaned = re.sub(r"\b(?:inch(?:es)?|inches|inch|in\.|in|mm|cm|m|ft|feet)\b", " ", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"[^0-9./\s-]", " ", cleaned)
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        if not cleaned:
            return None
        match = re.search(r"-?\d+(?:\s+\d+/\d+|/\d+|(?:\.\d+)?)", cleaned)
        if not match:
            return None
        token = match.group(0).strip()
        try:
            if " " in token and "/" in token:
                whole, frac = token.split(" ", 1)
                return float(whole) + float(Fraction(frac))
            if "/" in token:
                return float(Fraction(token))
            return float(token)
        except (ValueError, ZeroDivisionError):
            return None

    @api.model
    def _get_base_length_uom(self):
        # Retrieve the reference length UoM used for measurements.
        try:
            return self.env.ref(self._base_length_uom_xmlid)
        except ValueError:
            return None

    def _find_attribute(self, names, auto_create=False):
        # Locate a product attribute by any of the provided names.
        Attribute = self.env["product.attribute"]
        if isinstance(names, str):
            names = (names,)
        for name in names:
            attribute = Attribute.search([( "name", "=", name)], limit=1)
            if attribute:
                return attribute
        if auto_create:
            return self._create_measure_attribute(names)
        return Attribute.browse()

    def _log_template_variant_snapshot(self, template, component=None):
        # Print a quick summary of a template and its variants for debugging.
        if not template:
            return
        template.ensure_one()
        variants = template.product_variant_ids
        if not variants:
            return
        length_mapping = next(
            (mapping for mapping in self._component_attribute_map if mapping[0] == "data_length"),
            None,
        )
        length_attr = False
        if length_mapping:
            length_attr = self._find_attribute(length_mapping[1])
        variant_summaries = []
        target_measure = 0.0
        if component:
            target_measure = getattr(component, "data_length", 0.0) or 0.0
        for variant in variants[:20]:
            label = variant.display_name or variant.name or str(variant.id)
            if length_attr:
                ptav = variant.product_template_attribute_value_ids.filtered(
                    lambda record: record.attribute_id == length_attr
                )[:1]
                if ptav:
                    value_name = ptav.product_attribute_value_id.name
                    length_caption = self._lang_choice("Largo", "Length")
                    label = f"{label} [{length_caption}={value_name}]"
                    magnitude = self._extract_magnitude(ptav.product_attribute_value_id)
                    if magnitude is not None and target_measure:
                        status = (
                            "OK"
                            if magnitude + self._value_tolerance >= target_measure
                            else self._lang_choice("CORTO", "SHORT")
                        )
                        label = f"{label} ⇒ {status}"
            variant_summaries.append(label)
        if target_measure:
            summary_header = self._lang_choice(
                "WF panel plantilla → {name} | objetivo={target:.3f}",
                "WF panel template → {name} | target={target:.3f}",
            ).format(name=template.display_name or template.name or template.id, target=target_measure)
        else:
            summary_header = self._lang_choice(
                "WF panel plantilla → {name}",
                "WF panel template → {name}",
            ).format(name=template.display_name or template.name or template.id)
        joined = ", ".join(variant_summaries)
        message = f"{summary_header}: {joined}"
        self._print_colored(message, tone="detail")
        _logger.info(message)

    def _create_measure_attribute(self, names):
        # Create a new measurement attribute sharing the reference UoM category.
        Attribute = self.env["product.attribute"].sudo()
        base_attr = self._get_reference_length_attribute()
        vals = {
            "name": names[0] if names else "Measurement",
            "create_variant": "no_variant",
        }
        if base_attr:
            if base_attr.uom_category_id:
                vals["uom_category_id"] = base_attr.uom_category_id.id
        return Attribute.create(vals)

    def _get_reference_length_attribute(self):
        # Fetch an existing attribute that defines the reference length category.
        Attribute = self.env["product.attribute"].sudo()
        length_names = next((mapping[1] for mapping in self._component_attribute_map if mapping[0] == "data_length"), [])
        for name in length_names:
            attr = Attribute.search([("name", "=", name)], limit=1)
            if attr:
                return attr
        return Attribute.search([("uom_category_id", "!=", False)], limit=1)

    def _get_reference_uom(self):
        # Determine a reference UoM based on the preferred length attribute.
        reference_attr = self._get_reference_length_attribute()
        if not reference_attr:
            return False
        reference_value = reference_attr.value_ids.filtered(lambda v: v.uom_id)[:1]
        return reference_value.uom_id if reference_value else False

    @staticmethod
    def _describe_component(component, reason=None):
        # Provide a descriptive string for a component and its mismatch reason.
        pieces = []
        if component.data_id:
            pieces.append(component.data_id)
        if component.data_length:
            pieces.append(f"L={component.data_length}")
        if component.data_width:
            pieces.append(f"W={component.data_width}")
        if component.data_depth:
            pieces.append(f"D={component.data_depth}")
        label = ", ".join(pieces) or _("Componente sin datos")
        if reason:
            label = f"{label} → {reason}"
        return label

