# WF Panel Manufacturing

**Versión:** 18.0.4.0.1  
**Repositorio:** [github.com/danylook/WF_panel_manufacturing](https://github.com/danylook/WF_panel_manufacturing)

Genera órdenes de fabricación (MO) para paneles woodframe importados. Extiende `mrp.production`, `mrp.workorder`, `product.product` y `stock.move` para integrar el flujo completo desde la importación del panel hasta la fabricación y el envío al robot ROS2.

---

## 📁 Estructura del addon

```
WF_panel_manufacturing/
├── __manifest__.py
├── __init__.py
├── controllers/
│   ├── __init__.py
│   └── ros2_api.py              → API REST para consulta de piezas desde ROS2
├── models/
│   ├── __init__.py
│   ├── mrp_production.py         → Extiende mrp.production
│   ├── mrp_workorder.py          → Extiende mrp.workorder
│   ├── wf_panel.py               → Lógica de manufactura del panel
│   ├── wf_panel_section.py       → Extiende wf.panel.section
│   ├── product_product.py        → Extiende product.product
│   ├── product_template.py       → Extiende product.template + wf.product.stock.length
│   ├── res_config_settings.py    → Configuración de tiempos de corte
│   ├── stock_move.py             → Extiende stock.move
│   └── project_house.py          → Modelo legacy "Proyecto de Casa"
├── security/
│   └── ir.model.access.csv
├── static/
│   └── description/
├── views/
│   ├── panel_manufacturing_views.xml
│   ├── mrp_workorder_views.xml
│   ├── panel_mrp_dictionary_wizard_views.xml
│   ├── panel_mrp_mo_creator_button_views.xml
│   ├── product_product_views.xml
│   ├── product_template_views.xml
│   └── res_config_settings_views.xml
└── wizard/
    ├── __init__.py
    ├── panel_mrp_wizard.py               → Wizard principal de generación de MO
    ├── panel_mrp_MO_creator_wizard.py    → Wizard legacy de creación de MO
    └── panel_mrp_dictionary_wizard.py    → Diccionario de piezas extendido
```

---

## 🧩 Modelos

### `mrp.production` — `MrpProduction` (inherit)

| Campo | Tipo | Descripción |
|---|---|---|
| `panel_section_id` | Many2one → `wf.panel.section` | Sección WF vinculada a la MO |
| `wf_is_assembly_mo` | Boolean | Marca MOs de ensamblaje WF |
| `wf_cut_production_id` | Many2one → `mrp.production` | MO de corte WF asociada |

**Métodos:**

| Método | Descripción |
|---|---|
| `_link_bom(bom)` | Vincula una LdM a la MO, sincroniza líneas, operaciones y órdenes de trabajo. Sobrescribe comportamiento estándar de Odoo |
| `_wf_panel_align_component_move(move_raw, bom_lines_by_id, ratio)` | Alinea movimientos de componentes de panel con líneas de LdM |

### `mrp.workorder` — `MrpWorkorder` (inherit)

| Campo | Tipo | Descripción |
|---|---|---|
| `wf_instruction` | Text | Instrucciones WF para la orden de trabajo |
| `wf_planned_duration_min` | Float | Duración planificada WF (minutos) |
| `company_currency_id` | Related → `company_id.currency_id` | Moneda (store=True) |
| `wf_planned_cost` | Monetary | Costo planificado WF |
| `panel_id` | Many2one → `project.house.panel` | Panel de casa (legacy) |

### `wf.panel` — `WFPanel` (inherit)

| Método | Descripción |
|---|---|
| `ensure_manufacturing_profile(section)` | **Punto de entrada** — asegura que existan producto + LdM para la sección |
| `_compute_component_matches(section)` | Mapea cada componente del panel a una variante de producto existente |
| `_ensure_manufactured_product(section)` | Crea o recupera el producto terminado para la sección |
| `_ensure_section_bom(section, product)` | Crea o recupera la LdM para la sección |
| `_sync_bom_lines(bom, aggregated)` | Sincroniza líneas de la LdM con los componentes agregados |
| `_build_section_product_name(section)` | Construye nombre del producto: "Proyecto - Sección" |
| `_build_section_product_code(section)` | Construye código único del producto |
| `_build_section_bom_code(section)` | Construye código de la LdM |
| `_attach_section_glb(section, product)` | Adjunta modelo 3D (GLB) al producto desde el SVG de la sección |
| `_fetch_section_glb(section)` | Lee bytes del archivo GLB de la sección desde disco |

### `wf.panel.section` — `WFPanelSection` (inherit)

| Campo | Tipo | Descripción |
|---|---|---|
| `production_id` | Many2one → `mrp.production` | Orden de fabricación asociada |
| `manufactured_product_id` | Many2one → `product.product` | Producto fabricado para esta sección |
| `manufactured_bom_id` | Many2one → `mrp.bom` | Lista de materiales asociada |

**Métodos:**

| Método | Descripción |
|---|---|
| `action_create_panel_mo()` | Botón "Crear MO" — abre wizard para crear orden de fabricación |
| `action_prepare_manufacturing_profile()` | Botón "Preparar perfil fabricación" — genera producto + LdM |

### `product.product` — `ProductProduct` (inherit)

| Campo | Tipo | Descripción |
|---|---|---|
| `wf_panel_manufactured_only` | Boolean | Variante solo fabricable para WF panel |
| `wf_panel_cut_consumable` | Boolean | Pieza de corte generada para un trabajo de panel |
| `wf_panel_leftover_stock` | Boolean | Sobrante mantenido como stock disponible |

### `product.template` — `ProductTemplateWf` (inherit)

| Campo | Tipo | Descripción |
|---|---|---|
| `wf_stock_length_ids` | One2many → `wf.product.stock.length` | Longitudes de stock disponibles |

### `wf.product.stock.length` — `WfProductStockLength`

| Campo | Tipo | Descripción |
|---|---|---|
| `product_tmpl_id` | Many2one → `product.template` | Producto |
| `length_in` | Float | Longitud en pulgadas |

### `stock.move` — `StockMove` (inherit)

| Campo | Tipo | Descripción |
|---|---|---|
| `wf_panel_component_id` | Many2one → `wf.panel.component` | Componente del panel vinculado al movimiento |

### `res.config.settings` — Configuración

| Campo | Parámetro sistema | Descripción |
|---|---|---|
| `wf_cut_length_minutes_per_piece` | `wf_panel_manufacturing.cut_length_minutes_per_piece` | Minutos por corte de largo |
| `wf_cut_length_setup_minutes` | `wf_panel_manufacturing.cut_length_setup_minutes` | Preparación corte de largo |
| `wf_cut_width_minutes_per_piece` | `wf_panel_manufacturing.cut_width_minutes_per_piece` | Minutos por corte de ancho |
| `wf_cut_width_setup_minutes` | `wf_panel_manufacturing.cut_width_setup_minutes` | Preparación corte de ancho |

### `project.house` — `ProjectHouse` (legacy)

| Campo | Descripción |
|---|---|
| `name` | Nombre del proyecto de casa |
| `panel_ids` | One2many → `project.house.panel` |

### `project.house.panel` — `ProjectHousePanel` (legacy)

| Campo | Descripción |
|---|---|
| `name` | Nombre del panel |
| `house_id` | Many2one → `project.house` |
| `product_id` | Many2one → `product.product` |
| `bom_id` | Many2one → `mrp.bom` |
| `mo_id` | Many2one → `mrp.production` |
| `wo_ids` | One2many → `mrp.workorder` |

---

## 🧙 Wizard principal: `wf.panel.mrp.wizard`

**Clase:** `WFPanelManufacturingWizard`  
**Función principal:** `action_generate()`

Orquesta la creación completa de la MO para una sección de panel:

1. Verifica que la sección no tenga MO asociada
2. Llama a `ensure_manufacturing_profile()` para crear/verificar producto + LdM
3. Crea la MO (`mrp.production`)
4. Prepara movimientos de materia prima (`_prepare_raw_moves`)
5. Crea órdenes de trabajo predeterminadas (`_create_default_workorders`)
6. Prepara transiciones entre WO de corte y ensamblaje
7. Crea movimientos de sobrantes/recortes
8. Publica lista de corte en el chatter de la MO

### Métodos principales del wizard

| Método | Descripción |
|---|---|
| `default_get(fields_list)` | Precarga la sección activa en el wizard |
| `action_generate()` | **Método principal** — genera la MO completa |
| `_match_component_to_variant(component)` | Busca variante de producto que coincida con dimensiones del componente |
| `_prepare_raw_moves(production, aggregated, ...)` | Prepara movimientos de stock (materia prima) para la MO |
| `_create_default_workorders(production, section, ...)` | Crea WO predeterminadas (corte largo, corte ancho, ensamblaje) |
| `_prepare_cut_piece_transitions(...)` | Prepara transiciones entre WO de corte y ensamblaje |
| `_create_leftover_moves(production, ...)` | Crea movimientos de sobrantes/recortes |
| `_post_cut_list_to_mo(...)` | Publica la lista de corte en el chatter de la MO |
| `_get_component_variant_map()` | Cache de mapeo componente → variante |
| `_get_component_stock_length_map()` | Cache de longitudes de stock por componente |
| `_get_width_cut_components()` | Componentes que requieren corte de ancho |
| `_assign_cut_components_to_workorder(...)` | Asigna componentes de corte a sus WO |
| `_assign_assembly_components_to_workorder(...)` | Asigna componentes de ensamblaje a sus WO |
| `_sync_workorder_instructions(...)` | Sincroniza instrucciones en las WO |

### Wizards adicionales

| Wizard | Función | Propósito |
|---|---|---|
| `panel.mrp.mo.creator.wizard` | `action_create_panel_mo()` | Wizard legacy de creación de MO |
| `panel.mrp.dictionary.wizard` (extendido) | `action_panel_mrp_dictionary_wizard()` | Diccionario de piezas con estado de coincidencia |

---

## 🌐 API REST

**Controlador:** `WFRos2Controller` en `controllers/ros2_api.py`

| Método | Ruta | Descripción |
|---|---|---|
| **GET** | `/api/wf/ros2/section/<id>/pieces` | Posiciones de todas las piezas de una sección |
| **GET** | `/api/wf/ros2/production/<id>/pieces` | Posiciones de piezas filtradas por MO |
| **GET** | `/api/wf/ros2/workorder/<id>/pieces` | Posiciones de piezas filtradas por WO |
| **POST** | `/api/wf/ros2/workorder/<id>/acknowledge` | Robot confirma inicio de WO |

Autenticación: Bearer token (API key de Odoo) o sesión web.

---

## 🔄 Flujo de datos

```
WF_panel_importer
    │
    ▼  (wf.panel.section + wf.panel.component)
WF_panel_manufacturing
    │
    ├── action_prepare_manufacturing_profile()
    │     → Crea producto terminado + LdM
    │
    ├── action_generate() (wizard)
    │     → Crea MO, asigna workorders, movimientos de stock
    │     → panel_section_id → mrp.production
    │
    └── API REST → ROS2 consulta piezas
          │
          ▼
WF_ros2_bridge
    └── action_send_to_robot() → POST /start_job
```

---

## ⚙️ Parámetros del sistema

| Clave | Valor | Descripción |
|---|---|---|
| `wf_panel_manufacturing.cut_length_minutes_per_piece` | — | Minutos por corte de largo |
| `wf_panel_manufacturing.cut_length_setup_minutes` | — | Preparación corte de largo |
| `wf_panel_manufacturing.cut_width_minutes_per_piece` | — | Minutos por corte de ancho |
| `wf_panel_manufacturing.cut_width_setup_minutes` | — | Preparación corte de ancho |
