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

from datetime import date

# Que clase CSS lleva un indicador que no se pudo calcular. El dashboard la
# pinta en gris, no en rojo: no saber no es una mala noticia.
CLASE_NEUTRAL = 'neutral'

# Cuantos dias tiene el anio al que se refiere `tasa_costo_capital`. La tasa es
# ANUAL: este es el denominador que la baja al periodo que de verdad se midio.
DIAS_DEL_ANIO = 365


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


def dias_de_operacion(fecha_primer_movimiento, hoy=None):
    """Cuantos dias de operacion cubre el periodo que el dashboard esta sumando.

    DE DONDE SALE EL ARRANQUE (FASE-EVA-S3, Parte 1)

    De la fecha del primer movimiento real de la empresa: MIN(fecha) entre
    `gasto` e `ingreso`. No hay hoy en el modelo ningun campo de "inicio de
    operaciones" -- `Empresa.fecha_creacion` es cuando se dio de alta la cuenta
    en este sistema, que no es lo mismo: Korvo cargo movimientos anteriores a
    tener la cuenta, y contra ese campo el periodo saldria mas corto de lo que
    fue. Usar el primer movimiento no inventa ningun campo nuevo y describe
    exactamente el mismo rango de filas que el dashboard esta sumando.

    Devuelve None cuando no hay un solo movimiento: no hay periodo que medir, y
    ese caso ya lo cubre el estado neutral de FASE-EVA-S2.

    El minimo de 1 dia es el guard de division por cero del dia que se carga el
    primer dato -- y tambien cubre una fecha futura tipeada a mano, que daria
    negativo y le devolveria plata a la empresa en vez de cobrarle.
    """
    if fecha_primer_movimiento is None:
        return None
    return max(1, ((hoy or date.today()) - fecha_primer_movimiento).days)


def generar_analisis_completo(ingresos_totales, gastos_totales, config,
                              dias_transcurridos=DIAS_DEL_ANIO):
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

    QUE CAMBIO EN FASE-EVA-S3

    Lo que S2 dejo anotado como pendiente: `costo_capital` salia de una tasa
    ANUAL y se restaba entero, mientras que `ingresos_totales` y
    `gastos_totales` son la suma de toda la vida de la empresa sin filtro de
    fecha (app.py:493). A los tres meses de operar le cobraba un anio entero de
    costo de capital: la resta no cerraba dimensionalmente y el EVA salia mucho
    mas negativo de lo que era.

    Ahora la tasa se prorratea al periodo que de verdad se esta sumando:

        costo_capital = capital * (tasa / 100) * (dias_transcurridos / 365)

    `dias_transcurridos` lo calcula `dias_de_operacion()` a partir del primer
    movimiento de la empresa. El default de 365 NO es un valor inventado: es el
    supuesto viejo escrito donde se ve. Con un periodo de exactamente un anio la
    cuenta da identica a la de antes, que es la unica forma de saber que el fix
    solo cambia el caso que estaba mal.

    QUE NO CAMBIO

    El resto de la formula:

        utilidad_bruta = ingresos - gastos
        utilidad_neta  = utilidad_bruta - utilidad_bruta * tasa_impuestos
        EVA            = utilidad_neta - costo_capital

    QUE CAMBIO EN FASE-EVA-S5 (nada de esta funcion, y por eso hace falta
    leerlo aca)

    El dashboard dejo de MOSTRAR margen, ROI y EVA. Esta funcion los sigue
    calculando y los sigue devolviendo -- no se borro una sola linea de la
    cuenta -- pero de las claves que devuelve, la pantalla hoy usa solo estas
    cinco:

        ingresos, gastos, utilidad_neta, hay_movimiento, recomendaciones

    Las otras (margen_ganancia, roi, eva, ratio_gastos, costo_capital,
    dias_transcurridos, periodo_conocido y los cuatro `*_estado`) viajan hasta
    la plantilla y no se renderizan.

    POR QUE SE MOSTRABAN Y YA NO

    Los tres comparan los gastos historicos contra las ventas historicas. En un
    negocio que compra mercaderia para stock, buena parte de esos gastos es
    plata que todavia esta en el deposito sin vender: la resta describe el
    movimiento de caja, no la rentabilidad, y se estaba presentando con nombre
    de rentabilidad. El margen real -- el que descuenta el costo de lo
    efectivamente VENDIDO -- ya existe y esta bien hecho en /reportes/margen
    (rutas_reportes.py), abierto por producto y por canal.

    POR QUE NO SE BORRO LA CUENTA

    Es la segunda vez que se revisa y las dos veces la conclusion fue la misma:
    "no aplica al tamano de este negocio". Eso es una conclusion sobre el
    NEGOCIO, no sobre el codigo. El dia que Korvo separe compras de costo de lo
    vendido -- o que quiera medir el capital de los socios contra el resultado
    -- la formula, el prorrateo de S3 y el estado neutral de S2 estan enteros y
    probados. Borrarlos obligaria a re-derivar todo eso desde cero, incluidos
    los tres guards de division por cero que costaron una slice entera.

    Lo que si se saco es lo que llegaba a la pantalla: las tres tarjetas
    (templates/dashboard.html) y los consejos que hablaban de esos numeros
    (`generar_recomendaciones`, mas abajo).
    """
    utilidad_bruta = ingresos_totales - gastos_totales
    impuestos = utilidad_bruta * config.tasa_impuestos
    utilidad_neta = utilidad_bruta - impuestos

    # None = no hay ningun movimiento del cual medir el periodo. Se cae al
    # supuesto viejo en vez de partir la funcion en dos.
    #
    # FASE-EVA-S4 le abrio un caso que antes no existia. Cuando `ingresos` era
    # SUM(Ingreso.monto), un None aca implicaba ingresos y gastos en 0 -- el
    # periodo sale del MIN(fecha) de esas dos tablas (app.py) -- y entonces el
    # EVA quedaba en None mas abajo y este 365 no llegaba a pantalla. Ahora
    # los ingresos salen de Pedido, asi que una empresa con ventas y sin una
    # sola fila de Gasto ni de Ingreso tiene `hay_movimiento` en True con el
    # periodo desconocido: el costo de capital se le cobra como un anio entero.
    #
    # No se corrige aca a proposito -- el periodo lo arma el llamador y meterle
    # las fechas de los pedidos es cambiar el prorrateo de S3, que es otra
    # slice. La pantalla no miente mientras tanto: `periodo_conocido` viaja en
    # False y el dashboard ya dice que el periodo no se conoce.
    dias = (DIAS_DEL_ANIO if dias_transcurridos is None
            else max(1, int(dias_transcurridos)))

    costo_capital = (config.capital_invertido
                     * (config.tasa_costo_capital / 100)
                     * (dias / DIAS_DEL_ANIO))

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
        # Sobre que periodo se prorrateo el costo de capital. La plantilla lo
        # muestra debajo del EVA: sin esto el numero vuelve a ser opaco -- nadie
        # puede saber si "$-9000" es de un trimestre o de tres anios.
        "dias_transcurridos": dias,
        "periodo_conocido": dias_transcurridos is not None,
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
        # FASE-EVA-S5: recibe DOS cosas donde antes recibia seis. Las cuatro
        # que se fueron -- margen, roi, eva, hay_capital -- eran las que
        # generaban consejos sobre indicadores que el dashboard ya no muestra.
        # Los valores siguen calculados y siguen viajando en este mismo dict,
        # arriba; lo que se saco es el texto que los ponia en pantalla.
        "recomendaciones": generar_recomendaciones(ratio_gastos,
                                                   hay_movimiento)
    }


def generar_recomendaciones(ratio, hay_movimiento):
    """Las recomendaciones del pie del dashboard.

    Cada diagnostico se emite SOLO si su indicador se pudo calcular. Antes se
    emitian los tres sobre los valores inventados por los guards, asi que una
    cuenta vacia recibia "tu margen es muy bajo", "tu retorno es bajo" y
    "ALERTA! gastas mas del 80%": tres retos por no haber cargado nada.

    QUE CAMBIO EN FASE-EVA-S5

    Se fueron los tres consejos que hablaban de margen, ROI y EVA, y el que
    pedia cargar el capital invertido "para ver el ROI y el EVA". Las cuatro
    tarjetas de las que hablaban ya no estan en el dashboard (ver el comentario
    del bloque que se saco en templates/dashboard.html): dejar los consejos
    habria sido diagnosticar en el pie de la pantalla numeros que la pantalla
    ya no muestra, y encima con la cuenta que se decidio no mostrar.

    QUEDA el ratio de gastos, que es el unico de los cuatro que se arma con los
    dos numeros que SI siguen en pantalla -- Ingresos por Ventas y Gastos
    Totales -- y que ademas se enuncia por lo que es: cuanto de lo que entro se
    fue en gastos. Es una frase de caja, no de rentabilidad.

    OJO, y esta anotado a proposito: `ratio` y `margen_ganancia` son el MISMO
    numero visto al reves (margen = 100 - ratio). La diferencia no esta en la
    cuenta, esta en lo que cada uno afirma. "Margen 20%" dice "de cada peso que
    vendiste ganaste 20 centavos", y eso es falso mientras los gastos incluyan
    mercaderia que todavia no se vendio. "Gastaste el 80% de lo que entro" dice
    exactamente lo que paso con la caja, y eso es cierto sin importar donde
    este la mercaderia. Por eso uno se fue y el otro se queda.

    El parametro `hay_movimiento` sigue igual: sin un solo movimiento no hay
    nada que diagnosticar y se dice eso, en vez de retar por no haber cargado.
    """
    if not hay_movimiento:
        return [{
            "tipo": "info",
            "mensaje": "ℹ️ Todavía no cargaste ingresos ni gastos. Cuando "
                       "cargues los primeros movimientos vas a ver acá el "
                       "análisis del negocio."
        }]

    recomendaciones = []

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

    if not recomendaciones:
        recomendaciones.append({
            "tipo": "exito",
            "mensaje": "🎉 ¡Tu negocio está en buen estado!"
        })

    return recomendaciones
