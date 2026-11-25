# Entrega — Challenge Técnico - Traico Ignacio Agustin

## Parte 1 — Tickets de soporte

### Ticket #1
> "Cuando entro a ver un siniestro que se denunció hace poco, me sale una pantalla de error con codigo que no se entiende nada. Con otros siniestros anda bien."

**Cómo lo encontré:**
Se corroboró el error localmente. Como primera acción expuse el puerto de PostgreSQL en `docker-compose.yml` para inspeccionar la base de datos y comparar un siniestro que fallaba (`1469`) contra uno que funcionaba (`436`):

```sql
-- 1469 tiene error / 436 no tiene (se descarta que el error sea por decimales)
SELECT *
FROM public.claims_claim
WHERE id IN (1469, 436);

SELECT id, paid_at, amount, reference, claim_id
FROM public.claims_claimpayment
WHERE claim_id = 1469
LIMIT 10;

-- Cruce entre siniestros y pagos: se detecta que el error ocurre cuando no hay pagos asociados
-- (siniestros abiertos sin pagos o rechazados, no necesariamente los más recientes)
SELECT *
FROM public.claims_claim AS cl
LEFT JOIN claims_claimpayment AS cc
    ON cc.claim_id = cl.id
WHERE cl.id IN (1469, 436);
```

**Causa raíz:**
El error no dependía de la fecha del siniestro, sino de que no tenía pagos registrados. Al no haber pagos, `total_paid()` devolvía `None` (`NULL`), y al intentar restarlo en `pending_amount()` (`self.amount_claimed - Decimal(str(self.total_paid()))`) lanzaba `InvalidOperation`.

**Solución:**
Se modificó `total_paid()` en `app/claims/models.py` para que devuelva `0` en lugar de `None` cuando el siniestro no tiene pagos (`...["total"] or 0`). De esta forma `pending_amount()` calcula correctamente `amount_claimed - 0` sin lanzar excepciones y mostrando el saldo pendiente real.

---

### Ticket #2
> "El tablero dice que este mes se denunciaron muchos más siniestros de los que realmente cargamos. Los números no nos cierran."

**Cómo lo encontré:**
Analicé los templates HTML para identificar quién renderizaba la página principal (`dashboard.html`) y ubiqué la variable `{{ reported_this_month }}`. Al rastrearla en la vista `dashboard` (`app/claims/views.py`), vi la consulta:

```python
"reported_this_month": Claim.objects.filter(reported_at__month=today.month).count()
```

Para contrastar los datos reales en PostgreSQL ejecuté:

```sql
SELECT *
FROM public.claims_claim
WHERE reported_at BETWEEN '2026-11-01' AND '2026-11-24';
```

Confirmé que había solo 143 reportes en el mes actual, mientras que el tablero mostraba muchos más.

**Causa raíz:**
El filtro `reported_at__month=today.month` no filtraba por año, por lo que sumaba los siniestros del mes actual correspondientes a **todos los años** registrados en la base.

**Solución:**
Se ajustó el filtro en `app/claims/views.py` agregando el año actual:

```python
"reported_this_month": Claim.objects.filter(
    reported_at__year=today.year, reported_at__month=today.month
).count(),
```

---

### Ticket #3
> "Por ahora el listado de siniestros anda bien, pero sabemos que la base va a crecer mucho. Nos preocupa que se ponga lento, sobre todo al filtrar y buscar."

Al analizar `app/claims/views.py`, `app/claims/models.py` y `app/claims/templates/claims/claim_list.html`, se identificaron 6 problemas de escalabilidad:

#### 1) Búsqueda en memoria con Python + N+1 (`views.py`)
```python
if search:
    matching_ids = [
        claim.id
        # Trae toda la tabla a RAM
        for claim in Claim.objects.all()
        # Acá se realizan 2 queries por cada siniestro: una por policy y otra por client.
        # Como se necesita el nombre completo del cliente de la póliza, busca primero en
        # claims_policy para obtener el client_id y luego en claims_client.
        if search.lower() in claim.policy.client.full_name.lower()
        or search.lower() in claim.number.lower()
    ]
    claims = claims.filter(id__in=matching_ids)
```
* **Por qué escala mal:** Carga toda la tabla `claims_claim` en RAM, dispara 2 queries extra por cada registro (N+1) y genera una cláusula `WHERE id IN (...)` gigante.
* **Solución:** Se movió la búsqueda a SQL usando `Concat` (para concatenar `first_name + " " + last_name` directamente en PostgreSQL) junto con `Q` e `icontains`.

#### 2) Filtro por aseguradora materializando IDs (`views.py`)
```python
policy_ids = list(
    # En esta lista se guardan en memoria todos los IDs de pólizas de una aseguradora
    # para luego volver a enviar esa información como base de una nueva consulta (WHERE IN) en búsqueda de siniestros
    Policy.objects.filter(insurer_id=insurer_id).values_list("id", flat=True)
)
claims = claims.filter(policy_id__in=policy_ids)
```
* **Por qué escala mal:** Ejecuta dos queries separadas, carga potencialmente cientos de miles de IDs en memoria de Python y los reenvía a PostgreSQL en un `IN (...)` masivo.
* **Solución:** Se reemplazó por `claims.filter(policy__insurer_id=insurer_id)`, delegando el cruce a un único `INNER JOIN` en SQL.

#### 3) Problema N+1 de relaciones en el template (`claim_list.html`)
```html
<!-- Se ejecutan 3 consultas extra por cada fila renderizada (policy, client e insurer) -->
<td>{{ claim.policy.client.full_name }}</td>
<td>{{ claim.policy.number }}</td>
<td>{{ claim.policy.insurer.name }}</td>
```
* **Por qué escala mal:** Con 100 filas son 300 queries extra; con cientos de miles colapsa la base de datos.
* **Solución:** Se agregó `.select_related("policy", "policy__client", "policy__insurer")` en `claim_list` para resolver todas las relaciones en un solo `JOIN`.

#### 4) Problema N+1 en el cálculo de pagos (`claim_list.html` / `models.py`)
En `claim_list.html` se llamaba al método `claim.total_paid` en cada iteración del `{% for claim in claims %}`, lo que ejecutaba una query `SUM` por cada fila:

```python
def total_paid(self):
    return self.payments.aggregate(total=Sum("amount"))["total"]
```
* **Por qué escala mal:** Multiplica linealmente las consultas de agregación según la cantidad de siniestros listados.
* **Solución:** Se calculó el total en la consulta principal con `.annotate(paid_total=Coalesce(Sum("payments__amount"), Value(0.0)))` y se reemplazó `claim.total_paid` por `claim.paid_total` en el template.

#### 5) Falta de paginación (`views.py` y `claim_list.html`)
* **Por qué escala mal:** Traer y renderizar cientos de miles de filas de una sola vez satura la RAM del servidor, el tráfico de red y el DOM del navegador.
* **Solución:** Se implementó `Paginator` de Django (25 registros por página aplicando `LIMIT`/`OFFSET` en SQL), preservando los filtros activos en la URL (`query_string`) y agregando `.order_by("-reported_at", "-id")` explícito para mantener un orden consistente tras el `annotate`.

#### 6) Falta de índices en la base de datos (`models.py`)
* **Por qué escala mal:** Sin índices en `reported_at` y `status`, PostgreSQL debe realizar *Full Table Scans* y ordenar toda la tabla en memoria/disco en cada consulta.
* **Solución:** Se agregaron índices compuestos en `Claim.Meta.indexes` para `["-reported_at", "-id"]` y `["status", "-reported_at", "-id"]` junto con su migración correspondiente.

---

## Parte 2 — Funcionalidad nueva (Anulación de pagos)

> "Necesitamos poder anular un pago. No queremos que se borre: tiene que quedar registrado que existió, cuándo se anuló y por qué. Un pago anulado no debe contar en lo pagado del siniestro, y el estado del siniestro tiene que quedar coherente después de la anulación."

### Decisiones de diseño

1. **Baja lógica (*soft delete*) en `ClaimPayment`:**
   * Se agregaron dos campos al modelo: `cancelled_at` (`DateTimeField`, nullable) y `cancellation_reason` (`CharField`, max 255).
   * Se expuso la propiedad `is_cancelled` (`self.cancelled_at is not None`).
   * **Alternativa descartada:** Agregar un campo booleano `is_cancelled` separado de `cancelled_at`. Se descartó para evitar estados inconsistentes en base de datos (por ejemplo, un registro con `is_cancelled=True` y `cancelled_at=NULL`). Con un único campo `cancelled_at` como fuente de verdad, la inconsistencia es imposible.
   * **Alternativa descartada:** Crear un pago negativo (contraasiento contable). Si bien es válido en sistemas contables estrictos, para este caso de uso agrega complejidad innecesaria en la UI y no responde tan directo a "anular un pago puntual con fecha y motivo".

2. **Lógica de negocio centralizada en los modelos + atomicidad:**
   * `ClaimPayment.cancel(reason)` valida que el pago no esté ya anulado y ejecuta dentro de `transaction.atomic()` la marca de anulación (`cancelled_at`, `cancellation_reason`) y el recálculo del estado del siniestro (`self.claim.recalculate_status()`).
   * `Claim.recalculate_status()` centraliza las reglas de transición de estado y también se reutilizó en `register_payment` para no duplicar lógica entre vistas:
     * Si el siniestro está `REJECTED`, no se altera.
     * Si `total_paid >= amount_claimed` → `SETTLED`.
     * Si `total_paid > 0` → `IN_REVIEW`.
     * Si `total_paid == 0` → `OPEN`.
   * **Alternativa descartada:** Dejar la lógica de anulación y cambio de estado directamente en `views.py` o usar *Django signals* (`post_save`). Ponerla en la vista dificulta reutilizarla desde consola/admin/API, y los *signals* ocultan el flujo de ejecución dificultando el debugging.

3. **Consistencia en el cálculo de pagos activos:**
   * Se actualizó `Claim.total_paid()` para filtrar únicamente `cancelled_at__isnull=True`.
   * Se actualizó el `annotate` de `claim_list` en `views.py` usando `Sum("payments__amount", filter=Q(payments__cancelled_at__isnull=True))` para que el listado y el detalle muestren exactamente el mismo monto pagado.

4. **Flujo en pantalla (UX):**
   * En `claim_detail.html` los pagos anulados siguen visibles para mantener la trazabilidad, pero se muestran atenuados, con el monto tachado, badge `Anulado`, fecha/hora y motivo, y sin botón de anulación.
   * Al hacer clic en `Anular`, se redirige a una pantalla de confirmación (`payment_cancel_form.html`) con `PaymentCancelForm`, que exige un motivo obligatorio (no vacío) antes de confirmar por `POST`.

---

## Parte 3 — Tests

Se implementaron 9 tests automatizados en `app/claims/tests.py` (`docker compose exec web python manage.py test`):

* **Ticket #1:** Verifica que el detalle de un siniestro sin pagos responda `200 OK`, con `total_paid == 0` y `pending_amount == amount_claimed`.
* **Ticket #2:** Verifica que el contador mensual del dashboard excluya siniestros del mismo mes en años anteriores.
* **Ticket #3:** Verifica mediante `assertNumQueries(3)` que el listado, el filtro por aseguradora y la búsqueda mantengan un número fijo de consultas (sin N+1) y paginen correctamente.
* **Parte 2 (Anulación de pagos):**
  * Anular el único pago parcial revierte el estado del siniestro a `OPEN` y deja `total_paid` en `0`.
  * Anular uno de dos pagos en un siniestro `SETTLED` lo revierte a `IN_REVIEW` y recalcula el saldo pendiente.
  * Los pagos anulados quedan excluidos del `paid_total` anotado en el listado (`/claims/`).
  * El formulario rechaza anulaciones con motivo vacío o solo espacios.
  * No se permite anular un pago que ya fue anulado (`ValueError`).

---

## Parte 4 — Pregunta para investigar (CBV vs FBV)

### 1. ¿Qué clase base usaría para cada vista y por qué?

| Vista actual | Clase base CBV | Por qué |
| :--- | :--- | :--- |
| `dashboard` | `TemplateView` | Solo renderiza un template con métricas agregadas (no opera sobre un único modelo ni formulario). Se sobrescribe `get_context_data()` para inyectar los contadores. |
| `claim_list` | `ListView` | Encaja perfecto con el ciclo de vida de un listado paginado. Trae paginación incorporada (`paginate_by = 25`), maneja el `page_obj` automáticamente y permite aislar la consulta optimizada (`select_related`, `annotate`, filtros) en `get_queryset()` y los filtros extra (`insurers`, `statuses`, `query_string`) en `get_context_data()`. |
| `claim_detail` | `DetailView` | Obtiene automáticamente el `Claim` por `pk` (o devuelve 404). Solo habría que sobrescribir `get_context_data()` para agregar `payments`, `total_paid` y `pending_amount`. |
| `claim_create` | `CreateView` | Es un alta directa de un modelo con `ClaimForm`. Resuelve automáticamente `GET` (mostrar form) y `POST` (validar y guardar), usando `form_valid()` solo para agregar el mensaje de éxito y `get_success_url()` para redirigir al detalle. |
| `register_payment` | `CreateView` (o `FormView`) | Si bien crea un `ClaimPayment`, depende de un objeto padre (`Claim`) que viene por URL (`pk`) y además dispara lógica de negocio posterior (`claim.recalculate_status()`). Con `CreateView` se obtiene el `Claim` (por ejemplo en `dispatch()` o `get_claim()`), se inyecta en `get_context_data()` y en `form_valid()` se asigna `form.instance.claim = self.claim`, se guarda y se llama a `recalculate_status()`. Si la lógica creciera (por ejemplo, múltiples validaciones cruzadas), una `View` simple también sería una alternativa válida y más directa. |
| `cancel_payment` | `FormView` o `View` | No crea ni edita un modelo directamente con un `ModelForm`, sino que ejecuta una acción de dominio (`payment.cancel(reason)`) sobre un objeto ya existente con validaciones previas (verificar si ya está anulado). Con `View` (definiendo `get()` y `post()`) el flujo queda explícito y sin forzar el ciclo de vida de una vista genérica. |

### 2. ¿Cuándo conviene una CBV genérica y cuándo una `View` simple?

* **Conviene una CBV genérica (`ListView`, `DetailView`, `CreateView`, `UpdateView`)** cuando la vista sigue el patrón estándar de CRUD o listado: operar sobre **un único modelo principal**, con paginación estándar o un único formulario asociado. Ahí ahorran mucho código repetitivo (*boilerplate*) y estandarizan la estructura del proyecto.
* **Conviene una `View` simple** cuando la pantalla se aleja del CRUD clásico. Por ejemplo:
  * Vistas que manejan **múltiples formularios o múltiples modelos** en un mismo `POST`.
  * **Acciones de negocio o transiciones de estado** (como anular un pago, aprobar un lote o disparar un proceso) donde no hay un `save()` directo del formulario.
  * Endpoints que devuelven respuestas no estándar (JSON, descarga de CSV/PDF, webhooks).
* **Cuándo una genérica estorba:** Cuando terminás sobrescribiendo casi todos sus métodos (`dispatch`, `get_object`, `get_form_kwargs`, `form_valid`, `get_success_url`) para "pelear" contra lo que la clase hace por defecto. En esos casos, una `View` simple con `get()` y `post()` es mucho más corta, clara y fácil de mantener.

### 3. ¿Qué se gana y qué se pierde con CBV frente a FBV al leer y depurar código ajeno?

* **Qué se gana con CBV:**
  * **Estructura predecible:** En un equipo que usa CBV, siempre sabés dónde buscar cada cosa (`get_queryset` para consultas, `get_context_data` para variables del template, `form_valid` para efectos secundarios).
  * **Reutilización con Mixins:** Permisos, autenticación (`LoginRequiredMixin`), filtros por tenant o lógica común se inyectan heredando mixins sin repetir código en cada vista.
  * **Separación por método HTTP:** `get()` y `post()` están separados de forma limpia en lugar de bloques `if/else` largos.

* **Qué se pierde con CBV:**
  * **Flujo oculto ("magia" por herencia):** En una FBV leés de arriba hacia abajo y ves exactamente todo lo que ocurre. En una CBV, el flujo real está repartido en una jerarquía de clases padre y mixins (MRO de Python), lo que obliga a conocer bien el ciclo de vida interno de Django para saber en qué orden se ejecutan los métodos.
  * **Depuración más costosa:** Cuando algo falla dentro de un mixin o un método heredado que no está explícito en `views.py`, rastrear de dónde viene un bug o poner un breakpoint lleva más tiempo que en una función lineal.

---

## Qué harías con más tiempo

1. **Migrar `ClaimPayment.amount` de `FloatField` a `DecimalField`:** En sistemas financieros/seguros, usar flotantes introduce errores de redondeo binario y problemas de formato (por ejemplo, `10,0` en vez de `10,00`).
2. **Validar sobrepagos en `PaymentForm`:** Impedir (o pedir confirmación explícita) cuando se intenta registrar un pago cuyo monto supera el `pending_amount()` del siniestro, evitando saldos pendientes negativos.
3. **Auditoría de usuarios (`cancelled_by`):** Agregar autenticación y registrar qué usuario realizó la carga y la anulación de cada pago.
4. **Optimización avanzada de búsquedas de texto en PostgreSQL:** Los filtros `icontains` (`ILIKE '%texto%'`) no aprovechan índices B-Tree tradicionales. Con volumen real de producción, agregaría la extensión `pg_trgm` con índices `GIN` sobre `number`, `first_name` y `last_name`.
5. **Seguridad y permisos en acciones sensibles:** Restringir la anulación de pagos solo a roles autorizados (por ejemplo, supervisores de siniestros) y validar que no se puedan registrar pagos sobre siniestros `REJECTED`.

---

## Parte 4 — Pregunta para investigar (CBV vs FBV)

### 1. ¿Qué clase base usaría para cada vista y por qué?

| Vista actual | Clase base CBV | Por qué |
| :--- | :--- | :--- |
| `dashboard` | `TemplateView` | Solo renderiza un template con métricas agregadas (no opera sobre un único modelo ni formulario). Se sobrescribe `get_context_data()` para inyectar los contadores. |
| `claim_list` | `ListView` | Encaja perfecto con el ciclo de vida de un listado paginado. Trae paginación incorporada (`paginate_by = 25`), maneja el `page_obj` automáticamente y permite aislar la consulta optimizada (`select_related`, `annotate`, filtros) en `get_queryset()` y los filtros extra (`insurers`, `statuses`, `query_string`) en `get_context_data()`. |
| `claim_detail` | `DetailView` | Obtiene automáticamente el `Claim` por `pk` (o devuelve 404). Solo habría que sobrescribir `get_context_data()` para agregar `payments`, `total_paid` y `pending_amount`. |
| `claim_create` | `CreateView` | Es un alta directa de un modelo con `ClaimForm`. Resuelve automáticamente `GET` (mostrar form) y `POST` (validar y guardar), usando `form_valid()` solo para agregar el mensaje de éxito y `get_success_url()` para redirigir al detalle. |
| `register_payment` | `CreateView` (o `FormView`) | Si bien crea un `ClaimPayment`, depende de un objeto padre (`Claim`) que viene por URL (`pk`) y además dispara lógica de negocio posterior (`claim.recalculate_status()`). Con `CreateView` se obtiene el `Claim` (por ejemplo en `dispatch()` o `get_claim()`), se inyecta en `get_context_data()` y en `form_valid()` se asigna `form.instance.claim = self.claim`, se guarda y se llama a `recalculate_status()`. Si la lógica creciera (por ejemplo, múltiples validaciones cruzadas), una `View` simple también sería una alternativa válida y más directa. |
| `cancel_payment` | `FormView` o `View` | No crea ni edita un modelo directamente con un `ModelForm`, sino que ejecuta una acción de dominio (`payment.cancel(reason)`) sobre un objeto ya existente con validaciones previas (verificar si ya está anulado). Con `View` (definiendo `get()` y `post()`) el flujo queda explícito y sin forzar el ciclo de vida de una vista genérica. |

### 2. ¿Cuándo conviene una CBV genérica y cuándo una `View` simple?

* **Conviene una CBV genérica (`ListView`, `DetailView`, `CreateView`, `UpdateView`)** cuando la vista sigue el patrón estándar de CRUD o listado: operar sobre **un único modelo principal**, con paginación estándar o un único formulario asociado. Ahí ahorran mucho código repetitivo (*boilerplate*) y estandarizan la estructura del proyecto.
* **Conviene una `View` simple** cuando la pantalla se aleja del CRUD clásico. Por ejemplo:
  * Vistas que manejan **múltiples formularios o múltiples modelos** en un mismo `POST`.
  * **Acciones de negocio o transiciones de estado** (como anular un pago, aprobar un lote o disparar un proceso) donde no hay un `save()` directo del formulario.
  * Endpoints que devuelven respuestas no estándar (JSON, descarga de CSV/PDF, webhooks).
* **Cuándo una genérica estorba:** Cuando terminás sobrescribiendo casi todos sus métodos (`dispatch`, `get_object`, `get_form_kwargs`, `form_valid`, `get_success_url`) para "pelear" contra lo que la clase hace por defecto. En esos casos, una `View` simple con `get()` y `post()` es mucho más corta, clara y fácil de mantener.

### 3. ¿Qué se gana y qué se pierde con CBV frente a FBV al leer y depurar código ajeno?

* **Qué se gana con CBV:**
  * **Estructura predecible:** En un equipo que usa CBV, siempre sabés dónde buscar cada cosa (`get_queryset` para consultas, `get_context_data` para variables del template, `form_valid` para efectos secundarios).
  * **Reutilización con Mixins:** Permisos, autenticación (`LoginRequiredMixin`), filtros por tenant o lógica común se inyectan heredando mixins sin repetir código en cada vista.
  * **Separación por método HTTP:** `get()` y `post()` están separados de forma limpia en lugar de bloques `if/else` largos.

* **Qué se pierde con CBV:**
  * **Flujo oculto ("magia" por herencia):** En una FBV leés de arriba hacia abajo y ves exactamente todo lo que ocurre. En una CBV, el flujo real está repartido en una jerarquía de clases padre y mixins (MRO de Python), lo que obliga a conocer bien el ciclo de vida interno de Django para saber en qué orden se ejecutan los métodos.
  * **Depuración más costosa:** Cuando algo falla dentro de un mixin o un método heredado que no está explícito en `views.py`, rastrear de dónde viene un bug o poner un breakpoint lleva más tiempo que en una función lineal.

---

## Qué harías con más tiempo

1. **Migrar `ClaimPayment.amount` de `FloatField` a `DecimalField`:** En sistemas financieros/seguros, usar flotantes introduce errores de redondeo binario y problemas de formato (por ejemplo, `10,0` en vez de `10,00`).
2. **Validar sobrepagos en `PaymentForm`:** Impedir (o pedir confirmación explícita) cuando se intenta registrar un pago cuyo monto supera el `pending_amount()` del siniestro, evitando saldos pendientes negativos.
3. **Auditoría de usuarios (`cancelled_by`):** Agregar autenticación y registrar qué usuario realizó la carga y la anulación de cada pago.
4. **Optimización avanzada de búsquedas de texto en PostgreSQL:** Los filtros `icontains` (`ILIKE '%texto%'`) no aprovechan índices B-Tree tradicionales. Con volumen real de producción, agregaría la extensión `pg_trgm` con índices `GIN` sobre `number`, `first_name` y `last_name`.
5. **Seguridad y permisos en acciones sensibles:** Restringir la anulación de pagos solo a roles autorizados (por ejemplo, supervisores de siniestros) y validar que no se puedan registrar pagos sobre siniestros `REJECTED`.