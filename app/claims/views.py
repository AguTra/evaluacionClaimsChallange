from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models import Count, Q, Sum, Value
from django.db.models.functions import Coalesce, Concat
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
# PARTE 2# importar formulario de anulacion y modelo ClaimPayment
from .forms import ClaimForm, PaymentCancelForm, PaymentForm
from .models import Claim, ClaimPayment, Insurer, Policy

def dashboard(request):
    today = timezone.localdate()
    context = {
        "today": today,
        "open_claims": Claim.objects.filter(status=Claim.Status.OPEN).count(),
        "in_review_claims": Claim.objects.filter(status=Claim.Status.IN_REVIEW).count(),
        # Fix ticket 2, ajustando filtrado para abarcar correctamente el mes y año
        "reported_this_month": Claim.objects.filter(
            reported_at__year=today.year, reported_at__month=today.month
        ).count(),
        "by_status": Claim.objects.values("status").annotate(total=Count("id")).order_by("status"),
    }
    return render(request, "claims/dashboard.html", context)


def claim_list(request):
    # Ticket 3: Evita el problema N+1 en el template.
    # select_related trae policy, client e insurer en un solo JOIN en vez de 3 queries por fila.
    # annotate calcula el total pagado en SQL (evita llamar a claim.total_paid() por cada siniestro)
    claims = (
        Claim.objects
        .select_related("policy", "policy__client", "policy__insurer")
        # PARTE 2# excluir pagos anulados en el calculo del listado
        .annotate(
            paid_total=Coalesce(
                Sum("payments__amount", filter=Q(payments__cancelled_at__isnull=True)),
                Value(0.0),
            )
        )
    )

    insurer_id = request.GET.get("insurer")
    # tiket 3
    # ejecuta 2 query separadas, trae toda la info en memoria
    # Se cambia a un join para que la consulta filtre directamente sobre las polizas solicitadas
    if insurer_id:
        claims = claims.filter(policy__insurer_id=insurer_id)

    status = request.GET.get("status")
    if status:
        claims = claims.filter(status=status)

    search = request.GET.get("q", "").strip()
    # tiket 3
    # Antes traia toda la tabla a memoria en Python y disparaba 2 queries extra por cada siniestro (policy y client)
    # ahora concatena nombre y apellido directamente en SQL y filtra con Q (icontains) en una sola consulta.
    if search:
        claims = claims.annotate(
            client_full_name=Concat(
                "policy__client__first_name",
                Value(" "),
                "policy__client__last_name",
            )
        ).filter(
            Q(number__icontains=search)
            | Q(client_full_name__icontains=search)
        )

    # Ticket 3: Paginacion para evitar cargar y renderizar cientos de miles de registros de una sola vez (LIMIT/OFFSET en SQL).
    claims = claims.order_by("-reported_at", "-id")
    paginator = Paginator(claims, 25)
    page_number = request.GET.get("page")
    page_obj = paginator.get_page(page_number)

    # Conserva los filtros activos en la URL al cambiar de página
    query_params = request.GET.copy()
    query_params.pop("page", None)

    context = {
        "claims": page_obj,
        "page_obj": page_obj,
        "query_string": query_params.urlencode(),
        "insurers": Insurer.objects.all(),
        "statuses": Claim.Status.choices,
        "filters": {"insurer": insurer_id or "", "status": status or "", "q": search},
    }
    return render(request, "claims/claim_list.html", context)


def claim_detail(request, pk):
    claim = get_object_or_404(Claim, pk=pk)
    context = {
        "claim": claim,
        "payments": claim.payments.all(),
        "total_paid": claim.total_paid(),
        "pending_amount": claim.pending_amount(),
    }
    return render(request, "claims/claim_detail.html", context)


def claim_create(request):
    form = ClaimForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        claim = form.save()
        messages.success(request, f"Siniestro {claim.number} creado.")
        return redirect("claims:detail", pk=claim.pk)
    return render(request, "claims/claim_form.html", {"form": form})


def register_payment(request, pk):
    claim = get_object_or_404(Claim, pk=pk)
    form = PaymentForm(request.POST or None)

    if request.method == "POST" and form.is_valid():
        payment = form.save(commit=False)
        payment.claim = claim
        payment.save()

        # PARTE 2# centralizar actualizacion de estado en el modelo
        claim.recalculate_status()
        messages.success(request, "Pago registrado.")
        return redirect("claims:detail", pk=claim.pk)

    return render(request, "claims/payment_form.html", {"form": form, "claim": claim})

def cancel_payment(request, pk, payment_pk):
    claim = get_object_or_404(Claim, pk=pk)
    payment = get_object_or_404(ClaimPayment, pk=payment_pk, claim=claim)

    if payment.is_cancelled:
        messages.warning(request, "Este pago ya fue anulado.")
        return redirect("claims:detail", pk=claim.pk)

    form = PaymentCancelForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        payment.cancel(form.cleaned_data["reason"])
        messages.success(request, "Pago anulado correctamente.")
        return redirect("claims:detail", pk=claim.pk)

    return render(
        request,
        "claims/payment_cancel_form.html",
        {"form": form, "claim": claim, "payment": payment},
    )
