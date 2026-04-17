from models import db, Historial
from datetime import datetime

def registrar_cambio(usuario_id, tipo_accion, tipo_registro, registro_id, descripcion):
    """Registra un cambio en el historial"""
    historial = Historial(
        usuario_id=usuario_id,
        tipo_accion=tipo_accion,
        tipo_registro=tipo_registro,
        registro_id=registro_id,
        descripcion=descripcion
    )
    db.session.add(historial)
    db.session.commit()

def calcular_eva(ingresos_totales, gastos_totales, config):
    """Calcula el EVA"""
    utilidad_operativa = ingresos_totales - gastos_totales
    uodi = utilidad_operativa * (1 - config.tasa_impuestos)
    costo_capital = config.capital_invertido * (config.tasa_costo_capital / 100)
    eva = uodi - costo_capital
    
    return {
        "ingresos": ingresos_totales,
        "gastos": gastos_totales,
        "utilidad_bruta": utilidad_operativa,
        "impuestos": utilidad_operativa * config.tasa_impuestos,
        "uodi": uodi,
        "capital_invertido": config.capital_invertido,
        "costo_capital": costo_capital,
        "eva": eva,
        "es_rentable": eva > 0
    }

def gastos_por_categoria(gastos):
    """Agrupa gastos por categoría"""
    resultado = {}
    for gasto in gastos:
        cat = gasto.categoria.nombre
        resultado[cat] = resultado.get(cat, 0) + gasto.monto
    return resultado

def ingresos_por_categoria(ingresos):
    """Agrupa ingresos por categoría"""
    resultado = {}
    for ingreso in ingresos:
        cat = ingreso.categoria.nombre
        resultado[cat] = resultado.get(cat, 0) + ingreso.monto
    return resultado

def calcular_margen_ganancia(ingresos_totales, gastos_totales):
    """Calcula el margen de ganancia"""
    if ingresos_totales == 0:
        return 0
    ganancia = ingresos_totales - gastos_totales
    margen = (ganancia / ingresos_totales) * 100
    return margen

def calcular_roi(ganancia_neta, capital_invertido):
    """ROI = Return on Investment"""
    if capital_invertido == 0:
        return 0
    roi = (ganancia_neta / capital_invertido) * 100
    return roi

def calcular_ratio_deuda_ingresos(gastos_totales, ingresos_totales):
    """Ratio de gastos vs ingresos"""
    if ingresos_totales == 0:
        return 100
    ratio = (gastos_totales / ingresos_totales) * 100
    return ratio

def generar_analisis_completo(ingresos_totales, gastos_totales, config):
    """Genera análisis financiero COMPLETO"""
    utilidad_bruta = ingresos_totales - gastos_totales
    impuestos = utilidad_bruta * config.tasa_impuestos
    utilidad_neta = utilidad_bruta - impuestos
    
    margen_ganancia = calcular_margen_ganancia(ingresos_totales, gastos_totales)
    roi = calcular_roi(utilidad_neta, config.capital_invertido)
    ratio_gastos = calcular_ratio_deuda_ingresos(gastos_totales, ingresos_totales)
    
    costo_capital = config.capital_invertido * (config.tasa_costo_capital / 100)
    eva = utilidad_neta - costo_capital
    
    def evaluar_indicador(valor, umbral_min, umbral_max=None):
        if umbral_max:
            if valor >= umbral_min and valor <= umbral_max:
                return "excelente", "✅ Excelente"
            elif valor > umbral_max:
                return "malo", "❌ Demasiado alto"
            else:
                return "malo", "❌ Muy bajo"
        else:
            if valor >= umbral_min:
                return "excelente", "✅ Bien"
            else:
                return "malo", "❌ Mal"
    
    return {
        "ingresos": ingresos_totales,
        "gastos": gastos_totales,
        "utilidad_bruta": utilidad_bruta,
        "impuestos": impuestos,
        "utilidad_neta": utilidad_neta,
        "eva": eva,
        "costo_capital": costo_capital,
        "margen_ganancia": margen_ganancia,
        "margen_estado": evaluar_indicador(margen_ganancia, 20),
        "roi": roi,
        "roi_estado": evaluar_indicador(roi, 15),
        "ratio_gastos": ratio_gastos,
        "ratio_estado": evaluar_indicador(ratio_gastos, 0, 70),
        "eva_estado": ("excelente", "✅ Rentable") if eva > 0 else ("malo", "❌ No rentable"),
        "recomendaciones": generar_recomendaciones(
            margen_ganancia, roi, ratio_gastos, eva, ingresos_totales, gastos_totales
        )
    }

def generar_recomendaciones(margen, roi, ratio, eva, ingresos, gastos):
    """Genera recomendaciones automáticas"""
    recomendaciones = []
    
    if margen < 10:
        recomendaciones.append({
            "tipo": "peligro",
            "mensaje": "⚠️ Tu margen de ganancia es muy bajo. Necesitas reducir gastos o aumentar precios."
        })
    elif margen < 20:
        recomendaciones.append({
            "tipo": "advertencia",
            "mensaje": "⚠️ Tu margen está bajo. Considera optimizar costos."
        })
    
    if roi < 10:
        recomendaciones.append({
            "tipo": "peligro",
            "mensaje": "⚠️ Tu retorno sobre inversión es bajo."
        })
    elif roi < 15:
        recomendaciones.append({
            "tipo": "advertencia",
            "mensaje": "💡 Podrías mejorar el retorno de tu inversión."
        })
    
    if ratio > 80:
        recomendaciones.append({
            "tipo": "peligro",
            "mensaje": "🚨 ¡ALERTA! Gastas más del 80% de tus ingresos."
        })
    elif ratio > 70:
        recomendaciones.append({
            "tipo": "advertencia",
            "mensaje": "⚠️ Tus gastos son demasiado altos."
        })
    
    if eva < 0:
        recomendaciones.append({
            "tipo": "peligro",
            "mensaje": "🚨 Tu EVA es negativo. No generas valor económico real."
        })
    
    if ingresos == 0:
        recomendaciones.append({
            "tipo": "info",
            "mensaje": "ℹ️ No has registrado ingresos. Agrega tus primeros ingresos para ver el análisis."
        })
    
    if not recomendaciones:
        recomendaciones.append({
            "tipo": "exito",
            "mensaje": "🎉 ¡Tu negocio está en buen estado!"
        })
    
    return recomendaciones