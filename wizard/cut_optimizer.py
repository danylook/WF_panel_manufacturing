"""
WF Panel Cut Optimizer
======================
Algoritmo de optimización de cortes independiente de Odoo.

Toma una lista de cortes requeridos (largo en pulgadas) y una longitud de stock
disponible, y devuelve la disposición óptima de cortes que minimiza el desperdicio.

Estrategia: First-Fit Decreasing (FFD) con backtracking para mejorar
el aprovechamiento cuando hay piezas de longitudes similares.

Uso:
    from cut_optimizer import optimize_cuts, CutPlan

    result = optimize_cuts(
        cuts=[(comp_id, 96.0), (comp_id2, 48.0), ...],
        stock_length=240.0,
        min_leftover_in=19.685,  # 50 cm en pulgadas
    )
    for plan in result.plans:
        print(f"Stock {plan.stock_label}: {plan.cuts} → desperdicio {plan.waste:.3f}")
        if plan.leftover:
            print(f"  Resto aprovechable: {plan.leftover:.3f}")
"""

from dataclasses import dataclass, field
from typing import List, Optional, Tuple


# 36 pulgadas (~91 cm) — mínimo para considerar un resto como aprovechable
# Restos menores se descartan como desperdicio
MIN_LEFTOVER_INCHES = 36.0


@dataclass
class CutPlan:
    """Representa un plan de corte para una pieza de stock."""
    stock_label: str          # Identificador del stock (ej: "Perfil Aluminio 96\"")
    stock_length: float       # Longitud total de la pieza de stock (pulgadas)
    cuts: List[Tuple[int, float]] = field(default_factory=list)  # [(component_id, cut_length), ...]
    waste: float = 0.0        # Desperdicio en pulgadas
    leftover: float = 0.0      # Resto aprovechable (>= MIN_LEFTOVER_INCHES)
    is_leftover: bool = False  # True si este plan es para un resto de un corte anterior


@dataclass
class OptimizationResult:
    """Resultado completo de la optimización."""
    plans: List[CutPlan] = field(default_factory=list)
    total_waste: float = 0.0
    total_leftover: float = 0.0
    stock_used: int = 0
    efficiency: float = 0.0  # 0.0 - 1.0


def _ffd_bin_pack(cuts: List[Tuple[int, float]], stock_length: float) -> List[List[Tuple[int, float]]]:
    """
    First-Fit Decreasing bin packing.
    
    Args:
        cuts: Lista de (component_id, cut_length)
        stock_length: Longitud disponible por pieza de stock
    
    Returns:
        Lista de bins, cada bin es una lista de (component_id, cut_length)
    """
    if not stock_length or stock_length <= 0:
        return [[c] for c in cuts]
    
    # Orden descendente por longitud (FFD)
    sorted_cuts = sorted(cuts, key=lambda x: x[1], reverse=True)
    bins: List[List[Tuple[int, float]]] = []
    bin_remaining: List[float] = []
    
    for comp_id, cut_len in sorted_cuts:
        if cut_len <= 0:
            continue
        placed = False
        for i, remaining in enumerate(bin_remaining):
            if remaining >= cut_len - 1e-6:
                bins[i].append((comp_id, cut_len))
                bin_remaining[i] = remaining - cut_len
                placed = True
                break
        if not placed:
            bins.append([(comp_id, cut_len)])
            bin_remaining.append(stock_length - cut_len)
    
    return bins


def _try_improve_placement(
    cuts: List[Tuple[int, float]],
    stock_length: float,
    iterations: int = 100
) -> List[List[Tuple[int, float]]]:
    """
    Intenta mejorar el placement mediante búsqueda local.
    Intercambia cortes entre bins para reducir el número de bins usados.
    """
    bins = _ffd_bin_pack(cuts, stock_length)
    
    if len(bins) <= 1:
        return bins
    
    for _ in range(iterations):
        improved = False
        for i in range(len(bins)):
            for j in range(i + 1, len(bins)):
                # Intentar mover cortes del bin i al j
                bin_i_total = sum(cl for _, cl in bins[i])
                bin_j_remaining = stock_length - sum(cl for _, cl in bins[j])
                
                if bin_i_total <= bin_j_remaining + 1e-6:
                    # Podemos mover todo el bin i al j
                    bins[j].extend(bins[i])
                    bins.pop(i)
                    improved = True
                    break
                else:
                    # Intentar intercambiar un corte de i por uno de j
                    for ci, (c1_id, c1_len) in enumerate(bins[i]):
                        for cj, (c2_id, c2_len) in enumerate(bins[j]):
                            # Swap temporal
                            new_i_total = bin_i_total - c1_len + c2_len
                            new_j_total = (stock_length - bin_j_remaining) - c2_len + c1_len
                            if new_i_total <= stock_length + 1e-6 and new_j_total <= stock_length + 1e-6:
                                bins[i][ci], bins[j][cj] = bins[j][cj], bins[i][ci]
                                improved = True
                                break
                        if improved:
                            break
                if improved:
                    break
            if improved:
                break
        
        if not improved:
            break
    
    return bins


def optimize_cuts(
    cuts: List[Tuple[int, float]],
    stock_length: float,
    stock_label: str = "",
    min_leftover_in: float = MIN_LEFTOVER_INCHES,
    improve: bool = True,
) -> OptimizationResult:
    """
    Optimiza la disposición de cortes para minimizar desperdicio.
    
    Args:
        cuts: Lista de (component_id, cut_length) en pulgadas
        stock_length: Longitud de cada pieza de stock en pulgadas
        stock_label: Etiqueta descriptiva del material
        min_leftover_in: Longitud mínima para considerar un resto como aprovechable
        improve: Si aplicar mejora iterativa
    
    Returns:
        OptimizationResult con los planes de corte
    """
    if not cuts:
        return OptimizationResult()
    
    # Filtrar cortes válidos
    valid_cuts = [(cid, cl) for cid, cl in cuts if cl > 0]
    if not valid_cuts:
        return OptimizationResult()
    
    # Obtener bins optimizados
    if improve and stock_length > 0:
        bins = _try_improve_placement(valid_cuts, stock_length)
    else:
        bins = _ffd_bin_pack(valid_cuts, stock_length)
    
    plans = []
    total_waste = 0.0
    total_leftover = 0.0
    
    for i, bin_cuts in enumerate(bins):
        used = sum(cl for _, cl in bin_cuts)
        remaining = max(0.0, stock_length - used)
        
        if remaining >= min_leftover_in - 1e-6:
            # Resto aprovechable
            leftover = remaining
            waste = 0.0
            total_leftover += leftover
        else:
            leftover = 0.0
            waste = remaining
            total_waste += waste
        
        label = f"{stock_label} #{i + 1}" if stock_label else f"Stock #{i + 1}"
        
        plan = CutPlan(
            stock_label=label,
            stock_length=stock_length,
            cuts=bin_cuts,
            waste=waste,
            leftover=leftover,
            is_leftover=False,
        )
        plans.append(plan)
    
    total_used = sum(stock_length for _ in bins) if stock_length > 0 else sum(sum(cl for _, cl in b) for b in bins)
    total_cut = sum(sum(cl for _, cl in b) for b in bins)
    efficiency = total_cut / total_used if total_used > 0 else 0.0
    
    return OptimizationResult(
        plans=plans,
        total_waste=total_waste,
        total_leftover=total_leftover,
        stock_used=len(bins),
        efficiency=efficiency,
    )


def optimize_with_leftover_chain(
    cuts: List[Tuple[int, float]],
    stock_lengths: List[float],
    stock_label: str = "",
    min_leftover_in: float = MIN_LEFTOVER_INCHES,
) -> OptimizationResult:
    """
    Optimización con encadenamiento de restos.
    
    Los restos aprovechables de cortes anteriores se reutilizan como
    stock para cortes posteriores más pequeños.
    
    Args:
        cuts: Lista de (component_id, cut_length)
        stock_lengths: Lista de longitudes de stock disponibles
        stock_label: Etiqueta base
        min_leftover_in: Mínimo para resto aprovechable
    
    Returns:
        OptimizationResult con planes encadenados
    """
    if not cuts or not stock_lengths:
        return OptimizationResult()
    
    # Ordenar cortes de mayor a menor
    sorted_cuts = sorted(cuts, key=lambda x: x[1], reverse=True)
    
    all_plans = []
    pending = list(sorted_cuts)
    leftovers: List[float] = []
    
    # Primero usar las piezas de stock completas
    for sl in stock_lengths:
        if not pending:
            break
        
        # Tomar cortes que quepan en esta pieza
        bin_cuts = []
        remaining = sl
        still_pending = []
        
        for cid, cl in pending:
            if cl <= remaining + 1e-6:
                bin_cuts.append((cid, cl))
                remaining -= cl
            else:
                still_pending.append((cid, cl))
        
        pending = still_pending
        
        if not bin_cuts:
            continue
        
        if remaining >= min_leftover_in - 1e-6:
            leftovers.append(remaining)
        
        used = sum(cl for _, cl in bin_cuts)
        waste = max(0.0, remaining) if remaining < min_leftover_in else 0.0
        leftover = remaining if remaining >= min_leftover_in else 0.0
        
        label = f"{stock_label} #{len(all_plans) + 1}" if stock_label else f"Stock #{len(all_plans) + 1}"
        all_plans.append(CutPlan(
            stock_label=label,
            stock_length=sl,
            cuts=bin_cuts,
            waste=waste,
            leftover=leftover,
            is_leftover=False,
        ))
    
    # Luego usar restos aprovechables para cortes más pequeños
    leftovers.sort(reverse=True)
    for lr in leftovers:
        if not pending:
            break
        
        bin_cuts = []
        remaining = lr
        still_pending = []
        
        for cid, cl in pending:
            if cl <= remaining + 1e-6:
                bin_cuts.append((cid, cl))
                remaining -= cl
            else:
                still_pending.append((cid, cl))
        
        pending = still_pending
        
        if not bin_cuts:
            continue
        
        waste = max(0.0, remaining) if remaining < min_leftover_in else 0.0
        leftover = remaining if remaining >= min_leftover_in else 0.0
        
        label = f"Resto {lr:.1f}\""
        all_plans.append(CutPlan(
            stock_label=label,
            stock_length=lr,
            cuts=bin_cuts,
            waste=waste,
            leftover=leftover,
            is_leftover=True,
        ))
    
    total_used = sum(p.stock_length for p in all_plans)
    total_cut = sum(sum(cl for _, cl in p.cuts) for p in all_plans)
    efficiency = total_cut / total_used if total_used > 0 else 0.0
    
    return OptimizationResult(
        plans=all_plans,
        total_waste=sum(p.waste for p in all_plans),
        total_leftover=sum(p.leftover for p in all_plans),
        stock_used=len(all_plans),
        efficiency=efficiency,
    )
