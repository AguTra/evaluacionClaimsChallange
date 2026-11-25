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
