# FASE-AUDITORIA-S2: aca vivia un `registrar_cambio()` que era una bomba de
# tiempo. Usaba los kwargs `tipo_accion`, `tipo_registro` y `registro_id`, y
# NINGUNO de los tres existe como columna en Historial (son `accion`, `tipo` e
# `id_registro`): cualquier llamada tiraba TypeError.
#
# No explotaba nunca porque el unico modulo que la importaba, app.py, define
# mas abajo su propia version -- con los nombres correctos -- que pisa al
# import. O sea que funcionaba por el orden de las lineas, no por diseno. El
# dia que otro modulo hiciera `from eva_utils import registrar_cambio` para
# auditar una pantalla nueva, se llevaba la rota.
#
# Se borro en vez de corregirse: una segunda copia correcta invita igual a
# llamarla desde el lugar equivocado. La version buena esta en app.py:723 para
# las cuatro entidades que ya audita a mano, y el hook de auditoria.py cubre
# el resto sin que nadie tenga que llamar nada.
#
# FASE-EVA-S2: se borraron por el MISMO criterio otras tres funciones.
#
#   `gastos_por_categoria` / `ingresos_por_categoria` eran duplicados de las de
#   app.py:1248 y :1257, con una diferencia peligrosa: estas sumaban
#   `gasto.monto` crudo, que es Numeric, o sea Decimal. Las de app.py castean a
#   float. Las que se usan son las de app.py -- el import de app.py trae solo
#   `generar_analisis_completo` -- asi que las de aca nunca corrieron. El dia
#   que una pantalla nueva hiciera `from eva_utils import gastos_por_categoria`
#   se llevaba la que devuelve Decimals, y el `tojson` de Jinja revienta con
#   Decimal: los graficos del dashboard se caian.
#
#   `calcular_eva()` no la llamaba nadie. Era una segunda formula de EVA,
#   matematicamente equivalente a la que si se usa (calculaba UODI en un paso
#   en vez de restar los impuestos aparte, misma cuenta). Dos formulas para un
#   solo numero es una invitacion a que se separen.

# Que clase CSS lleva un indicador que no se pudo calcular. El dashboard la
# pinta en gris, no en rojo: no saber no es una mala noticia.
CLASE_NEUTRAL = 'neutral'


def calcular_margen_ganancia(ingresos_totales, gastos_totales):
    """Margen % sobre ingresos. None si no hay ingresos.

    Antes devolvia 0 cuando `ingresos_totales == 0`. Ese 0 no era un margen del
    0%: era "no se puede dividir". El dashboard no distinguia una cosa de la
    otra y mostraba "0.0% -- Mal" sobre una cuenta sin una sola fila cargada.
    """
    if ingresos_totales == 0:
        return None
    ganancia = ingresos_totales - gastos_totales
    return (ganancia / ingresos_totales) * 100


def calcular_roi(ganancia_neta, capital_invertido):
    """Retorno sobre el capital invertido, en %. None si no hay capital.

    Mismo arreglo que el margen: el 0 que devolvia con capital 0 se leia como
    "tu inversion no rinde nada", cuando lo que pasa es que nadie cargo todavia
    cuanto se invirtio.
    """
    if capital_invertido == 0:
        return None
    return (ganancia_neta / capital_invertido) * 100


def calcular_ratio_deuda_ingresos(gastos_totales, ingresos_totales):
    """Que porcentaje de los ingresos se va en gastos. None si no hay ingresos.

    ESTE era el peor de los tres. Devolvia **100** cuando no habia ingresos, y
    100 pasa el umbral de 80, asi que el dashboard de una empresa vacia gritaba
    "ALERTA! Gastas mas del 80% de tus ingresos". Un artefacto de division por
    cero disfrazado de diagnostico.

    Se saca de raiz en vez de parchearse aguas abajo: mientras la funcion
    siguiera inventando un 100, cualquier pantalla nueva que la llamara heredaba
    la misma mentira.
    """
    if ingresos_totales == 0:
        return None
    return (gastos_totales / ingresos_totales) * 100


def evaluar_indicador(valor, umbral_min, umbral_max=None, falta=None):
    """Semaforo de un indicador. `valor is None` => neutral, nunca rojo.

    `falta` es el texto que explica QUE falta cargar. Decir "sin datos" a secas
    deja a Roman sin saber que hacer; decirle "carga el capital invertido en
    Configuracion" es accionable.

    Era una funcion anidada dentro de `generar_analisis_completo`. Sale al
    modulo para que el test del estado neutral pueda ejercerla sola.
    """
    if valor is None:
        return (CLASE_NEUTRAL, falta or 'Todavía no hay datos suficientes')

    # `is not None` y no `if umbral_max:` -- un umbral maximo de 0 es falsy y se
    # caia al otro branch en silencio.
    if umbral_max is not None:
        if umbral_min <= valor <= umbral_max:
            return 'excelente', '✅ Excelente'
        if valor > umbral_max:
            return 'malo', '❌ Demasiado alto'
        return 'malo', '❌ Muy bajo'

    if valor >= umbral_min:
        return 'excelente', '✅ Bien'
    return 'malo', '❌ Mal'


def generar_analisis_completo(ingresos_totales, gastos_totales, config):
    """Analisis financiero del dashboard.

    QUE CAMBIO EN FASE-EVA-S2

    Los cuatro indicadores (margen, ROI, ratio de gastos, EVA) ahora pueden
    valer None. None significa "no se puede calcular todavia", y es distinto de
    0. Antes los tres guards de division por cero devolvian numeros inventados
    -- 0, 0 y 100 -- que el semaforo interpretaba como resultados reales: la
    pantalla de aterrizaje de una empresa recien creada mostraba tres alarmas
    rojas sin tener un solo dato cargado.

    QUE NO CAMBIO

    La formula, cuando SI hay datos, es exactamente la de antes:

        utilidad_bruta = ingresos - gastos
        utilidad_neta  = utilidad_bruta - utilidad_bruta * tasa_impuestos
        costo_capital  = capital_invertido * (tasa_costo_capital / 100)
        EVA            = utilidad_neta - costo_capital

    LO QUE SIGUE PENDIENTE (no es de esta slice)

    `costo_capital` sale de una tasa ANUAL, pero el dashboard suma ingresos y
    gastos de toda la vida de la empresa, sin filtro de fecha (ver app.py:493).
    A los tres meses de operar le cobra un anio entero de costo de capital. La
    resta no cierra dimensionalmente y ningun estado neutral lo arregla: hace
    falta acotar el periodo, que es una slice aparte.
    """
    utilidad_bruta = ingresos_totales - gastos_totales
    impuestos = utilidad_bruta * config.tasa_impuestos
    utilidad_neta = utilidad_bruta - impuestos

    costo_capital = config.capital_invertido * (config.tasa_costo_capital / 100)

    # Las dos condiciones que deciden que se puede calcular y que no.
    #
    # `hay_movimiento` mira ingresos O gastos: una empresa que todavia no vendio
    # pero ya compro mercaderia SI tiene un resultado (negativo) que vale la
    # pena mostrar.
    hay_movimiento = ingresos_totales > 0 or gastos_totales > 0
    hay_capital = config.capital_invertido > 0

    margen_ganancia = calcular_margen_ganancia(ingresos_totales, gastos_totales)
    ratio_gastos = calcular_ratio_deuda_ingresos(gastos_totales, ingresos_totales)

    # ROI y EVA necesitan las DOS cosas. Con capital en 0 el costo de capital da
    # 0 y el EVA queda identico a la ganancia neta: seria mostrar el mismo
    # numero dos veces, una de ellas con un nombre que asusta.
    if hay_movimiento and hay_capital:
        roi = calcular_roi(utilidad_neta, config.capital_invertido)
        eva = utilidad_neta - costo_capital
    else:
        roi = None
        eva = None

    falta_ingresos = 'Todavía no cargaste ingresos'
    falta_movimiento = 'Todavía no cargaste ingresos ni gastos'
    falta_capital = 'Cargá el capital invertido en Configuración'
    falta_roi_eva = falta_movimiento if not hay_movimiento else falta_capital

    if eva is None:
        eva_estado = (CLASE_NEUTRAL, falta_roi_eva)
    elif eva > 0:
        eva_estado = ('excelente', '✅ Rentable')
    else:
        eva_estado = ('malo', '❌ No rentable')

    return {
        "ingresos": ingresos_totales,
        "gastos": gastos_totales,
        "utilidad_bruta": utilidad_bruta,
        "impuestos": impuestos,
        "utilidad_neta": utilidad_neta,
        "eva": eva,
        "costo_capital": costo_capital,
        # Las lee la plantilla para decidir si muestra un numero o un guion.
        "hay_movimiento": hay_movimiento,
        "hay_capital": hay_capital,
        "margen_ganancia": margen_ganancia,
        "margen_estado": evaluar_indicador(margen_ganancia, 20,
                                           falta=falta_ingresos),
        "roi": roi,
        "roi_estado": evaluar_indicador(roi, 15, falta=falta_roi_eva),
        "ratio_gastos": ratio_gastos,
        "ratio_estado": evaluar_indicador(ratio_gastos, 0, 70,
                                          falta=falta_ingresos),
        "eva_estado": eva_estado,
        "recomendaciones": generar_recomendaciones(
            margen_ganancia, roi, ratio_gastos, eva, hay_movimiento, hay_capital
        )
    }


def generar_recomendaciones(margen, roi, ratio, eva, hay_movimiento, hay_capital):
    """Las recomendaciones del pie del dashboard.

    Cada diagnostico se emite SOLO si su indicador se pudo calcular. Antes se
    emitian los tres sobre los valores inventados por los guards, asi que una
    cuenta vacia recibia "tu margen es muy bajo", "tu retorno es bajo" y
    "ALERTA! gastas mas del 80%": tres retos por no haber cargado nada.

    Los ultimos dos parametros eran `ingresos, gastos` y solo se usaban para
    preguntar `if ingresos == 0`. Ahora llegan ya resueltos como los dos
    booleanos que de verdad deciden, que son los mismos que usa el semaforo.
    """
    if not hay_movimiento:
        return [{
            "tipo": "info",
            "mensaje": "ℹ️ Todavía no cargaste ingresos ni gastos. Cuando "
                       "cargues los primeros movimientos vas a ver acá el "
                       "análisis del negocio."
        }]

    recomendaciones = []

    if margen is not None:
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

    if roi is not None:
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

    if ratio is not None:
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

    if eva is not None and eva < 0:
        recomendaciones.append({
            "tipo": "peligro",
            "mensaje": "🚨 Tu EVA es negativo. No generas valor económico real."
        })

    # No es un reto: es lo que falta para que dos de los cuatro cuadros dejen de
    # estar en gris.
    if not hay_capital:
        recomendaciones.append({
            "tipo": "info",
            "mensaje": "ℹ️ Para ver el ROI y el EVA, cargá el capital "
                       "invertido en Configuración."
        })

    if not recomendaciones:
        recomendaciones.append({
            "tipo": "exito",
            "mensaje": "🎉 ¡Tu negocio está en buen estado!"
        })

    return recomendaciones
