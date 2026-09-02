# -*- coding: utf-8 -*-
"""Mapeo de un pago de Mercado Pago a un MovimientoCuenta (FASE-MP-S1).

Funciones puras: no tocan la base ni la red. Eso permite testear el mapeo con
payloads guardados y sin credenciales, igual que `ingestor_tiendanube.py`.

No implementa la clase abstracta IngestorCanal a proposito. Ese contrato esta
escrito alrededor de un CANAL DE VENTA (traer_pedidos, traer_productos,
variantes_de_producto) y lo que hay aca es una CUENTA DE COBRO: no vende nada,
solo dice cuanta plata entro. Forzarla adentro del ABC obligaria a implementar
tres metodos devolviendo listas vacias, que es peor documentacion que este
parrafo. Lo que si se respeta del contrato es lo que importa aguas abajo:

  - los importes salen como Decimal, nunca float;
  - las fechas salen en UTC naive, como el resto del modelo;
  - hash_movimiento() es estable entre corridas, para que resincronizar no
    duplique plata.

SOBRE EL NETO
-------------
`movimiento_cuenta.monto` es lo que REALMENTE entro a la cuenta, no lo que
pago el cliente. Mercado Pago devuelve el bruto en `transaction_amount` y el
desglose de comisiones en `fee_details`, un array de {type, amount, fee_payer}.

Se restan solo las comisiones con fee_payer='collector', que es quien cobra
(nosotros). Las que paga el comprador (fee_payer='payer', tipico del costo de
financiacion en cuotas) no salen de nuestra plata: restarlas subestimaria lo
que entro. Cuando fee_payer no viene se asume 'collector', que es el caso
abrumadoramente mas comun y el conservador: preferimos mostrar un numero un
poco por debajo de lo que entro antes que uno por encima.
"""

import hashlib
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

# Los unicos pagos que cuentan como plata que entro. El resto (pending,
# rejected, cancelled, in_process) todavia no es plata, y refunded/charged_back
# son eventos que la sacan -- eso es materia de otra slice, no se inventa aca.
ESTADO_APROBADO = 'approved'

# Quien paga la comision, segun Mercado Pago. Solo las del cobrador salen de
# nuestro bolsillo.
PAGADOR_COBRADOR = 'collector'

# tipo de movimiento_cuenta. Un pago aprobado es un cobro; las comisiones no
# generan su propia fila porque ya vienen descontadas del neto.
TIPO_COBRO = 'cobro'

PROCESADOR = 'mercadopago'


class PagoIgnorado(Exception):
    """El pago no corresponde a un movimiento. No es un error de la corrida."""


def es_aprobado(crudo):
    return (crudo or {}).get('status') == ESTADO_APROBADO


def _decimal(valor):
    """Decimal desde lo que sea que mande la API (str, int, float, None).

    Pasa por str() antes de Decimal incluso para los float: Decimal(0.1) da
    0.1000000000000000055511151231257827, y Decimal(str(0.1)) da 0.1.
    """
    if valor is None:
        raise InvalidOperation('valor nulo')
    if isinstance(valor, Decimal):
        return valor
    return Decimal(str(valor).strip())


def _redondear(monto):
    """A dos decimales, que es la precision de Numeric(14, 2)."""
    return monto.quantize(Decimal('0.01'))


def monto_neto(crudo):
    """(neto, aviso). El aviso es None si el desglose de fees se entendio.

    Politica ante un fee_details roto, que es lo que hace que un pago raro no
    voltee la corrida entera:

      - Sin fee_details, o vacio: el neto ES el bruto. No es un caso de error;
        un cobro en efectivo o una transferencia de dinero en cuenta no tienen
        comision, y ahi 0 es la comision correcta. Sin aviso.

      - fee_details con alguna entrada ilegible (amount que no es un numero,
        entrada que no es un dict): se descartan SOLO esas entradas, se restan
        las que si se entendieron y se devuelve un aviso. El pago se guarda
        igual.

    Por que se guarda y no se saltea: el numero que Roman quiere ver es cuanta
    plata entro en total. Descartar el pago lo hace faltar entero, que es un
    error de miles de pesos; guardarlo con una comision de menos lo deja corto
    por unos cientos, y el aviso queda registrado en sync_log para poder
    revisarlo. Un movimiento aproximado y marcado es mas util que un agujero.
    """
    try:
        bruto = _redondear(_decimal(crudo.get('transaction_amount')))
    except (InvalidOperation, ValueError, TypeError, AttributeError) as exc:
        # Sin bruto no hay movimiento posible: aca si se saltea el pago.
        raise PagoIgnorado(
            'el pago %s no trae un transaction_amount valido (%r)'
            % ((crudo or {}).get('id'), (crudo or {}).get('transaction_amount'))
        ) from exc

    detalles = crudo.get('fee_details')
    if not detalles:
        return bruto, None

    if not isinstance(detalles, list):
        return bruto, ('el pago %s trae fee_details con un formato inesperado '
                       '(%s): se registro el monto bruto'
                       % (crudo.get('id'), type(detalles).__name__))

    comision = Decimal('0.00')
    ilegibles = 0
    for detalle in detalles:
        if not isinstance(detalle, dict):
            ilegibles += 1
            continue
        # fee_payer ausente se toma como 'collector': ver el docstring del
        # modulo. Las del comprador no salen de nuestra plata.
        pagador = detalle.get('fee_payer') or PAGADOR_COBRADOR
        if str(pagador).lower() != PAGADOR_COBRADOR:
            continue
        try:
            comision += _decimal(detalle.get('amount'))
        except (InvalidOperation, ValueError, TypeError):
            ilegibles += 1

    neto = _redondear(bruto - comision)

    aviso = None
    if ilegibles:
        aviso = ('el pago %s trae %d comision(es) que no se pudieron leer: el '
                 'neto quedo por encima de lo real' % (crudo.get('id'), ilegibles))

    return neto, aviso


def hash_movimiento(crudo):
    """Huella estable para movimiento_cuenta.hash_dedup.

    Se deriva SOLO del id del pago, que es el unico dato que Mercado Pago
    garantiza inmutable y unico. Meter monto o fecha adentro seria peor: un
    pago cuya comision se ajusta despues cambiaria de hash y entraria dos
    veces, que es exactamente lo que la columna existe para impedir.

    El prefijo con el procesador evita que un id de Mercado Pago colisione con
    el de otro procesador el dia que haya mas de uno.
    """
    id_pago = (crudo or {}).get('id')
    if id_pago in (None, ''):
        raise PagoIgnorado('el pago vino sin id de Mercado Pago')
    semilla = '%s:pago:%s' % (PROCESADOR, id_pago)
    return hashlib.sha256(semilla.encode('utf-8')).hexdigest()


def _fecha(texto):
    """El ISO 8601 con offset que manda Mercado Pago -> UTC naive.

    Formato tipico: '2026-08-14T10:20:30.000-04:00'. Python 3.11+ parsea la
    'Z' y los offsets con dos puntos sin ayuda; para los que vienen sin dos
    puntos ('-0400') se normaliza a mano.
    """
    if not texto:
        return None
    crudo = str(texto).strip()
    try:
        momento = datetime.fromisoformat(crudo.replace('Z', '+00:00'))
    except ValueError:
        # Ultimo intento: offset pegado sin dos puntos.
        if len(crudo) >= 5 and crudo[-5] in '+-':
            arreglado = '%s:%s' % (crudo[:-2], crudo[-2:])
            try:
                momento = datetime.fromisoformat(arreglado)
            except ValueError:
                return None
        else:
            return None

    if momento.tzinfo is None:
        return momento
    # A UTC y sin tzinfo: el modelo entero guarda UTC naive.
    return (momento - momento.utcoffset()).replace(tzinfo=None)


def _descripcion(crudo):
    """Texto corto para la fila del extracto, recortado a la columna (255)."""
    partes = []
    metodo = crudo.get('payment_method_id') or crudo.get('payment_type_id')
    if metodo:
        partes.append(str(metodo))
    detalle = crudo.get('description')
    if detalle:
        partes.append(str(detalle))
    if not partes:
        partes.append('Cobro de Mercado Pago')
    return ' - '.join(partes)[:255]


def normalizar_movimiento(crudo):
    """Un pago aprobado -> los campos de MovimientoCuenta, listos para escribir.

    No incluye cuenta_id: eso lo pone quien persiste, que es el unico que sabe
    de que cuenta era el token con el que se leyo.

    Levanta PagoIgnorado si el pago no es un movimiento (no aprobado, sin id,
    sin fecha de acreditacion o sin monto). Quien llama lo cuenta como salteado,
    no como error.
    """
    crudo = crudo or {}

    if not es_aprobado(crudo):
        raise PagoIgnorado('el pago %s esta en estado %r, no aprobado'
                           % (crudo.get('id'), crudo.get('status')))

    hash_dedup = hash_movimiento(crudo)

    # date_approved es el momento en que la plata quedo aprobada. Si faltara
    # (no deberia, con status=approved) se cae a date_created antes de
    # descartar el pago: la fecha exacta importa menos que no perder el monto.
    fecha = _fecha(crudo.get('date_approved')) or _fecha(crudo.get('date_created'))
    if fecha is None:
        raise PagoIgnorado('el pago %s no trae una fecha interpretable'
                           % crudo.get('id'))

    neto, aviso = monto_neto(crudo)

    return {
        'fecha': fecha,
        'tipo': TIPO_COBRO,
        'descripcion': _descripcion(crudo),
        'moneda': (crudo.get('currency_id') or 'ARS')[:3],
        'monto': neto,
        'id_externo_procesador': str(crudo.get('id'))[:120],
        'hash_dedup': hash_dedup,
        'aviso': aviso,
    }


def token_vencido(expira_en, margen=timedelta(minutes=5)):
    """True si el token ya vencio (o esta por vencer dentro del margen).

    El margen evita el caso feo: empezar un backfill de varias paginas con un
    token al que le quedan diez segundos y que se muera a mitad de camino.
    `expira_en` None significa "no se sabe" y NO se trata como vencido: la
    credencial se guardo antes de que se registrara el vencimiento, y la unica
    forma de saberlo es probando contra la API.
    """
    if expira_en is None:
        return False
    return expira_en <= datetime.utcnow() + margen
