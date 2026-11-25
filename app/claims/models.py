from decimal import Decimal

from django.core.validators import MinValueValidator, RegexValidator
# PARTE 2# imports para transacciones atomicas y fecha actual
from django.db import models, transaction
from django.db.models import Sum
from django.utils import timezone

only_digits = RegexValidator(r"^\d+$", "Only digits are allowed.")


class Insurer(models.Model):
    name = models.CharField(max_length=120, unique=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class Client(models.Model):
    first_name = models.CharField(max_length=80)
    last_name = models.CharField(max_length=80)
    document = models.CharField(max_length=11, unique=True, validators=[only_digits])
    email = models.EmailField()

    class Meta:
        ordering = ["last_name", "first_name"]

    def __str__(self):
        return self.full_name

    @property
    def full_name(self):
        return f"{self.first_name} {self.last_name}"


class Policy(models.Model):
    number = models.CharField(max_length=20, unique=True)
    client = models.ForeignKey(Client, on_delete=models.PROTECT, related_name="policies")
    insurer = models.ForeignKey(Insurer, on_delete=models.PROTECT, related_name="policies")
    sum_insured = models.DecimalField(
        max_digits=14, decimal_places=2, validators=[MinValueValidator(Decimal("0.01"))]
    )
    start_date = models.DateField()
    end_date = models.DateField()

    class Meta:
        verbose_name_plural = "policies"
        ordering = ["number"]

    def __str__(self):
        return self.number


class Claim(models.Model):
    class Status(models.TextChoices):
        OPEN = "open", "Abierto"
        IN_REVIEW = "in_review", "En revisión"
        SETTLED = "settled", "Liquidado"
        REJECTED = "rejected", "Rechazado"

    number = models.CharField(max_length=20, unique=True)
    policy = models.ForeignKey(Policy, on_delete=models.PROTECT, related_name="claims")
    reported_at = models.DateField()
    cause = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    amount_claimed = models.DecimalField(
        max_digits=14, decimal_places=2, validators=[MinValueValidator(Decimal("0.01"))]
    )
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.OPEN)

    # Tiket 3, acelerar el ordenamiento y filtrado
    class Meta:
        ordering = ["-reported_at", "-id"]
        indexes = [
            models.Index(fields=["-reported_at", "-id"], name="claim_reported_id_idx"),
            models.Index(fields=["status", "-reported_at", "-id"], name="claim_status_rep_idx"),
        ]

    def __str__(self):
        return self.number

    def total_paid(self):
        # Ticket 1: Si no hay pagos, aggregate devuelve None; devolvemos 0 para evitar errores de negocio
        # PARTE 2# excluir pagos anulados del total pagado
        return (
            self.payments.filter(cancelled_at__isnull=True).aggregate(total=Sum("amount"))["total"]
            or 0
        )

    def pending_amount(self):
        return self.amount_claimed - Decimal(str(self.total_paid()))

    # PARTE 2# recalcular estado del siniestro segun pagos activos
    def recalculate_status(self):
        if self.status == self.Status.REJECTED:
            return

        paid = Decimal(str(self.total_paid()))
        if paid >= self.amount_claimed:
            new_status = self.Status.SETTLED
        elif paid > 0:
            new_status = self.Status.IN_REVIEW
        else:
            new_status = self.Status.OPEN

        if self.status != new_status:
            self.status = new_status
            self.save(update_fields=["status"])


class ClaimPayment(models.Model):
    claim = models.ForeignKey(Claim, on_delete=models.PROTECT, related_name="payments")
    paid_at = models.DateField()
    amount = models.FloatField()
    reference = models.CharField(max_length=60, blank=True)
    # PARTE 2# campos para baja logica de pagos con fecha y motivo
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancellation_reason = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["paid_at", "id"]

    def __str__(self):
        return f"{self.claim.number} - {self.amount}"

    # PARTE 2# propiedad para saber si el pago esta anulado
    @property
    def is_cancelled(self):
        return self.cancelled_at is not None

    # PARTE 2# anular pago de forma atomica y actualizar estado del siniestro
    def cancel(self, reason):
        if self.is_cancelled:
            raise ValueError("El pago ya se encuentra anulado.")

        with transaction.atomic():
            self.cancelled_at = timezone.now()
            self.cancellation_reason = reason
            self.save(update_fields=["cancelled_at", "cancellation_reason"])
            self.claim.recalculate_status()