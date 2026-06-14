import base64
import logging
import tempfile
from collections import defaultdict
from pathlib import Path

from odoo import _, fields, models
from odoo.exceptions import UserError


_logger = logging.getLogger(__name__)


class WFPanel(models.Model):
    _inherit = "wf.panel"

    def ensure_manufacturing_profile(self, section=None):
        self.ensure_one()
        section = section or self.section_ids[:1]
        if not section:
            raise UserError(_("El panel %s no tiene secciones asociadas.") % (self.display_name,))
        aggregated, missing = self._compute_component_matches(section)
        product = self._ensure_manufactured_product(section)
        bom = self._ensure_section_bom(section, product)
        aggregated_dict = dict(aggregated)
        self._sync_bom_lines(bom, aggregated_dict)
        return aggregated_dict, missing

    def _compute_component_matches(self, section):
        wizard_id = self.env.context.get("_wf_panel_mrp_wizard_id")
        wizard = False
        cleanup_wizard = False
        if wizard_id:
            wizard = self.env["wf.panel.mrp.wizard"].browse(wizard_id).exists()
        if not wizard:
            wizard = self.env["wf.panel.mrp.wizard"].with_context(
                active_model="wf.panel.section",
                active_ids=[section.id],
            ).create({"section_id": section.id})
            cleanup_wizard = True
        aggregated = defaultdict(float)
        missing = []
        for component in section.component_ids:
            variant, reason = wizard._match_component_to_variant(component)
            if variant:
                aggregated[variant] += 1.0
            else:
                missing.append(wizard._describe_component(component, reason))
        if cleanup_wizard:
            wizard.unlink()
        return aggregated, missing

    def _ensure_manufactured_product(self, section):
        self.ensure_one()
        if section.manufactured_product_id:
            product = section.manufactured_product_id
            self._attach_section_glb(section, product)
            return product
        Product = self.env["product.product"].with_context(active_test=False).sudo()
        product_name = self._build_section_product_name(section)
        default_code = self._build_section_product_code(section)
        product = Product.search([("default_code", "=", default_code)], limit=1)
        if not product:
            product = Product.search([("name", "=", product_name)], limit=1)
        if product:
            if not product.active:
                product.write({"active": True})
            section.manufactured_product_id = product
            if not self.manufactured_product_id:
                self.manufactured_product_id = product
            self._attach_section_glb(section, product)
            return product
        template_vals = {
            "name": product_name,
            "default_code": default_code,
            "type": "consu",
            "tracking": "none",
            "sale_ok": False,
            "purchase_ok": False,
        }
        template = self.env["product.template"].sudo().create(template_vals)
        product = template.product_variant_id
        section.manufactured_product_id = product
        if not self.manufactured_product_id:
            self.manufactured_product_id = product
        self._attach_section_glb(section, product)
        return product

    def _ensure_section_bom(self, section, product):
        self.ensure_one()
        bom = section.manufactured_bom_id.sudo() if section.manufactured_bom_id else False
        Bom = self.env["mrp.bom"].sudo()
        if bom and bom.exists():
            updates = {}
            if bom.product_id != product:
                updates.update({
                    "product_tmpl_id": product.product_tmpl_id.id,
                    "product_id": product.id,
                })
            desired_code = self._build_section_bom_code(section)
            if bom.code != desired_code:
                updates["code"] = desired_code
            # Clear stale project_id to avoid FK violation on mrp.production
            if bom.project_id:
                updates["project_id"] = False
            if updates:
                bom.write(updates)
            return bom
        bom_vals = {
            "product_tmpl_id": product.product_tmpl_id.id,
            "product_id": product.id,
            "product_qty": 1.0,
            "code": self._build_section_bom_code(section),
            "type": "normal",
            "project_id": False,
        }
        bom = Bom.create(bom_vals)
        section.manufactured_bom_id = bom
        if not self.bom_id:
            self.bom_id = bom
        return bom

    def _sync_bom_lines(self, bom, aggregated):
        BomLine = self.env["mrp.bom.line"].sudo()
        existing = {line.product_id.id: line.sudo() for line in bom.bom_line_ids.sudo()}
        kept = set()
        for variant, quantity in aggregated.items():
            line = existing.get(variant.id)
            values = {
                "product_qty": quantity,
                "product_uom_id": variant.uom_id.id,
            }
            if line:
                line.write(values)
            else:
                values.update({
                    "bom_id": bom.id,
                    "product_id": variant.id,
                })
                BomLine.create(values)
            kept.add(variant.id)
        for line in bom.bom_line_ids.sudo():
            if line.product_id.id not in kept:
                line.unlink()

    def _build_section_product_name(self, section):
        project_label = (
            section.project_id.project
            or section.project_id.name
            or self.name
            or f"Panel {self.id}"
        )
        section_label = section.name or section.display_name or f"Section {section.id}"
        if project_label and section_label:
            return f"{project_label} - {section_label}"
        return section_label or project_label

    def _build_section_product_code(self, section):
        project_key = section.project_id.project or section.project_id.name or self.name or str(self.id)
        section_key = section.name or section.display_name or str(section.id)
        if project_key and section_key:
            code = f"{project_key}:{section_key}"
        else:
            code = project_key or section_key or f"WF-PANEL-{section.id}"
        return code[:64]

    def _build_section_bom_code(self, section):
        return section.name or self.name or f"BOM-{section.id}"

    def _attach_section_glb(self, section, product):
        try:
            glb_bytes = self._fetch_section_glb(section)
        except Exception as exc:  # pragma: no cover - defensive logging
            _logger.debug("GLB fetch failed for section %s: %s", section.id, exc)
            return
        if not glb_bytes:
            return
        template = product.product_tmpl_id.sudo()
        current = template.model_3d
        if current:
            try:
                current_bytes = base64.b64decode(current, validate=True)
            except Exception:  # pragma: no cover - corrupted data fallback
                current_bytes = None
            if current_bytes == glb_bytes:
                return
        template.write({"model_3d": base64.b64encode(glb_bytes)})
        _logger.info(
            "Modelo 3D adjuntado al producto %s desde GLB de la sección %s",
            product.display_name,
            section.name,
        )

    def _fetch_section_glb(self, section):
        config = self.env["ir.config_parameter"].sudo()
        base_dir = config.get_param("wf_panel_importer.svg_temp_dir")
        if not base_dir:
            base_dir = tempfile.gettempdir()
        svg_dir = Path(base_dir) / "svg_pages"
        if not svg_dir.exists():
            return None
        base_name = None
        if section.source_file:
            source_stem = Path(section.source_file).stem
            for suffix in ("_paso5_reescalado_frontal", "_paso5_reescalado"):
                if source_stem.endswith(suffix):
                    source_stem = source_stem[: -len(suffix)]
                    break
            base_name = source_stem
        if not base_name:
            base_name = section.name
        if not base_name:
            return None
        glb_path = svg_dir / f"{base_name}.glb"
        if not glb_path.exists():
            return None
        try:
            data = glb_path.read_bytes()
        except OSError:
            return None
        if not data:
            return None
        return data
