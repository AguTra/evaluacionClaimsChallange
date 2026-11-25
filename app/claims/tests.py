from datetime import date
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import Claim, ClaimPayment, Client, Insurer, Policy


class BaseClaimTestCase(TestCase):
    def setUp(self):
        self.insurer = Insurer.objects.create(name="La Segunda")
        self.client_obj = Client.objects.create(
            first_name="Juan",
            last_name="Perez",
            document="12345678",
            email="juan@example.com",
        )
        self.policy = Policy.objects.create(
            number="POL-001",
            client=self.client_obj,
            insurer=self.insurer,
            sum_insured=Decimal("1000000.00"),
            start_date=date(2025, 1, 1),
            end_date=date(2027, 1, 1),
        )


class Ticket1ClaimDetailWithoutPaymentsTest(BaseClaimTestCase):
    def test_claim_detail_without_payments_returns_200(self):
        claim = Claim.objects.create(
            number="CLM-001",
            policy=self.policy,
            reported_at=timezone.localdate(),
            cause="Choque",
            amount_claimed=Decimal("50000.00"),
        )

        # Con el código original esto Fallaba con TypeError / InvalidOperation (500)
        response = self.client.get(reverse("claims:detail", args=[claim.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["total_paid"], 0)
        self.assertEqual(response.context["pending_amount"], Decimal("50000.00"))


class Ticket2DashboardMonthlyCountTest(BaseClaimTestCase):
    def test_reported_this_month_excludes_previous_years(self):
        today = timezone.localdate()
        last_year_same_month = today.replace(year=today.year - 1)

        Claim.objects.create(
            number="CLM-THIS-YEAR",
            policy=self.policy,
            reported_at=today,
            cause="Robo",
            amount_claimed=Decimal("10000.00"),
        )
        Claim.objects.create(
            number="CLM-LAST-YEAR",
            policy=self.policy,
            reported_at=last_year_same_month,
            cause="Granizo",
            amount_claimed=Decimal("20000.00"),
        )

        # Con el código original devolvía 2 en vez de 1
        response = self.client.get(reverse("claims:dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["reported_this_month"], 1)


class Ticket3ClaimListPerformanceTest(BaseClaimTestCase):
    def setUp(self):
        super().setUp()
        today = timezone.localdate()
        # Creamos 30 siniestros con pagos para probar N+1 y paginación
        for i in range(30):
            claim = Claim.objects.create(
                number=f"CLM-{i:03d}",
                policy=self.policy,
                reported_at=today,
                cause="Siniestro de prueba",
                amount_claimed=Decimal("10000.00"),
            )
            ClaimPayment.objects.create(
                claim=claim,
                paid_at=today,
                amount=2500.0,
                reference=f"REF-{i}",
            )

    def test_claim_list_queries_and_pagination(self):
        # 3 queries fijas: COUNT(*) del paginador + SELECT paginado con JOIN/SUM + lista de insurers
        # Con el código original disparaba +120 queries
        with self.assertNumQueries(3):
            response = self.client.get(reverse("claims:list"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["claims"]), 25)

    def test_claim_list_search_and_insurer_filter_queries(self):
        # Con el código original traía toda la tabla a Python y disparaba N+1 queries
        with self.assertNumQueries(3):
            response = self.client.get(
                reverse("claims:list"),
                {"insurer": self.insurer.id, "q": "Juan Perez"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["page_obj"].paginator.count, 30)

# PARTE 2# tests para anulacion de pagos y coherencia de estados
class Part2PaymentCancellationTest(BaseClaimTestCase):
    def setUp(self):
        super().setUp()
        self.today = timezone.localdate()
        self.claim = Claim.objects.create(
            number="CLM-CANCEL-001",
            policy=self.policy,
            reported_at=self.today,
            cause="Choque",
            amount_claimed=Decimal("10000.00"),
        )

    def test_cancel_partial_payment_reverts_status_to_open(self):
        # Pago parcial deja el siniestro en IN_REVIEW
        payment = ClaimPayment.objects.create(
            claim=self.claim, paid_at=self.today, amount=4000.0, reference="P-1"
        )
        self.claim.recalculate_status()
        self.assertEqual(self.claim.status, Claim.Status.IN_REVIEW)

        # Anular el unico pago vuelve el siniestro a OPEN y deja pagado en 0
        url = reverse("claims:payment_cancel", args=[self.claim.pk, payment.pk])
        response = self.client.post(url, {"reason": "Monto equivocado"})
        self.assertRedirects(response, reverse("claims:detail", args=[self.claim.pk]))

        payment.refresh_from_db()
        self.claim.refresh_from_db()

        self.assertTrue(payment.is_cancelled)
        self.assertIsNotNone(payment.cancelled_at)
        self.assertEqual(payment.cancellation_reason, "Monto equivocado")
        self.assertEqual(self.claim.total_paid(), 0)
        self.assertEqual(self.claim.pending_amount(), Decimal("10000.00"))
        self.assertEqual(self.claim.status, Claim.Status.OPEN)

    def test_cancel_one_of_two_payments_reverts_settled_to_in_review(self):
        p1 = ClaimPayment.objects.create(
            claim=self.claim, paid_at=self.today, amount=6000.0, reference="P-1"
        )
        p2 = ClaimPayment.objects.create(
            claim=self.claim, paid_at=self.today, amount=4000.0, reference="P-2"
        )
        self.claim.recalculate_status()
        self.assertEqual(self.claim.status, Claim.Status.SETTLED)

        # Al anular p2, queda solo p1 (6000 < 10000), debe volver a IN_REVIEW
        p2.cancel("Pago duplicado")
        self.claim.refresh_from_db()

        self.assertEqual(self.claim.total_paid(), 6000.0)
        self.assertEqual(self.claim.pending_amount(), Decimal("4000.00"))
        self.assertEqual(self.claim.status, Claim.Status.IN_REVIEW)

    def test_cancelled_payment_is_excluded_from_claim_list(self):
        p1 = ClaimPayment.objects.create(
            claim=self.claim, paid_at=self.today, amount=3000.0, reference="P-1"
        )
        p2 = ClaimPayment.objects.create(
            claim=self.claim, paid_at=self.today, amount=2000.0, reference="P-2"
        )
        p2.cancel("Error de carga")

        response = self.client.get(reverse("claims:list"), {"q": "CLM-CANCEL-001"})
        self.assertEqual(response.status_code, 200)
        listed_claim = response.context["claims"][0]
        self.assertEqual(listed_claim.paid_total, 3000.0)

    def test_cancel_payment_requires_reason(self):
        payment = ClaimPayment.objects.create(
            claim=self.claim, paid_at=self.today, amount=1000.0, reference="P-1"
        )
        url = reverse("claims:payment_cancel", args=[self.claim.pk, payment.pk])
        response = self.client.post(url, {"reason": "   "})

        self.assertEqual(response.status_code, 200)
        payment.refresh_from_db()
        self.assertFalse(payment.is_cancelled)

    def test_cannot_cancel_payment_twice(self):
        payment = ClaimPayment.objects.create(
            claim=self.claim, paid_at=self.today, amount=1000.0, reference="P-1"
        )
        payment.cancel("Primer motivo")

        with self.assertRaises(ValueError):
            payment.cancel("Segundo motivo")