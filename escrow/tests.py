from __future__ import annotations

from decimal import Decimal

from django.test import TestCase, TransactionTestCase
from django.utils import timezone
from rest_framework.test import APIClient

from .models import Escrow
from .tasks import expire_funded_escrows
import threading
from django.db import connection
import random
import time


class EscrowModelStateMachineTests(TestCase):
    def setUp(self):
        self.escrow = Escrow.objects.create(
            buyer_id="buyer-1",
            seller_id="seller-1",
            amount=Decimal("10.00"),
            currency="USD",
        )

    def test_created_to_funded_to_released(self):
        self.assertEqual(self.escrow.state, Escrow.State.CREATED)

        self.escrow.fund(now=timezone.now())
        self.escrow.refresh_from_db()
        self.assertEqual(self.escrow.state, Escrow.State.FUNDED)
        self.assertIsNotNone(self.escrow.expires_at)

        self.escrow.release(now=timezone.now())
        self.escrow.refresh_from_db()
        self.assertEqual(self.escrow.state, Escrow.State.RELEASED)

    def test_created_to_funded_to_refunded(self):
        self.escrow.fund(now=timezone.now())
        self.escrow.refresh_from_db()
        self.assertEqual(self.escrow.state, Escrow.State.FUNDED)

        self.escrow.refund(now=timezone.now())
        self.escrow.refresh_from_db()
        self.assertEqual(self.escrow.state, Escrow.State.REFUNDED)

    def test_invalid_transitions_raise(self):
        # Cannot release or refund directly from CREATED
        with self.assertRaises(ValueError):
            self.escrow.release()
        with self.assertRaises(ValueError):
            self.escrow.refund()

        self.escrow.fund(now=timezone.now())

        # Once released, cannot refund; once refunded, cannot release
        self.escrow.release(now=timezone.now())
        with self.assertRaises(ValueError):
            self.escrow.refund()


class EscrowAPIPermissionsTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.buyer_headers = {
            "HTTP_X_USER_ID": "buyer-1",
            "HTTP_X_USER_ROLE": "buyer",
        }
        self.seller_headers = {
            "HTTP_X_USER_ID": "seller-1",
            "HTTP_X_USER_ROLE": "seller",
        }

    def test_buyer_can_create_and_list_own_escrows(self):
        payload = {
            "seller_id": "seller-1",
            "amount": "50.00",
            "currency": "USD",
        }
        resp = self.client.post("/api/escrows/", payload, format="json", **self.buyer_headers)
        self.assertEqual(resp.status_code, 201, resp.content)
        escrow_id = resp.data["id"]

        # Buyer lists escrows and sees the created one
        resp = self.client.get("/api/escrows/", **self.buyer_headers)
        self.assertEqual(resp.status_code, 200)
        ids = [item["id"] for item in resp.data]
        self.assertIn(escrow_id, ids)

    def test_seller_can_only_view_assigned_escrows(self):
        # Buyer creates escrow with seller-1
        payload = {
            "seller_id": "seller-1",
            "amount": "50.00",
            "currency": "USD",
        }
        resp = self.client.post("/api/escrows/", payload, format="json", **self.buyer_headers)
        self.assertEqual(resp.status_code, 201)
        escrow_id = resp.data["id"]

        # Seller-1 can see it
        resp = self.client.get("/api/escrows/", **self.seller_headers)
        self.assertEqual(resp.status_code, 200)
        ids = [item["id"] for item in resp.data]
        self.assertIn(escrow_id, ids)

        # Another seller cannot see it
        other_seller_headers = {
            "HTTP_X_USER_ID": "seller-2",
            "HTTP_X_USER_ROLE": "seller",
        }
        resp = self.client.get("/api/escrows/", **other_seller_headers)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.data), 0)

    def test_seller_cannot_release_or_refund(self):
        # Buyer creates and funds an escrow
        payload = {
            "seller_id": "seller-1",
            "amount": "50.00",
            "currency": "USD",
        }
        resp = self.client.post("/api/escrows/", payload, format="json", **self.buyer_headers)
        escrow_id = resp.data["id"]

        resp = self.client.post(f"/api/escrows/{escrow_id}/fund/", **self.buyer_headers)
        self.assertEqual(resp.status_code, 200)

        # Seller attempts to release or refund -> forbidden
        resp = self.client.post(f"/api/escrows/{escrow_id}/release/", **self.seller_headers)
        self.assertEqual(resp.status_code, 403)
        resp = self.client.post(f"/api/escrows/{escrow_id}/refund/", **self.seller_headers)
        self.assertEqual(resp.status_code, 403)

    def test_buyer_cannot_act_on_someone_elses_escrow(self):
        # buyer-1 creates escrow
        payload = {
            "seller_id": "seller-1",
            "amount": "50.00",
            "currency": "USD",
        }
        resp = self.client.post("/api/escrows/", payload, format="json", **self.buyer_headers)
        escrow_id = resp.data["id"]

        # buyer-2 cannot see or modify it
        other_buyer_headers = {
            "HTTP_X_USER_ID": "buyer-2",
            "HTTP_X_USER_ROLE": "buyer",
        }
        resp = self.client.get(f"/api/escrows/{escrow_id}/", **other_buyer_headers)
        self.assertEqual(resp.status_code, 404)
        resp = self.client.post(f"/api/escrows/{escrow_id}/fund/", **other_buyer_headers)
        self.assertIn(resp.status_code, (403, 404))


class EscrowExpirationTaskTests(TransactionTestCase):
    def setUp(self):
        self.escrow = Escrow.objects.create(
            buyer_id="buyer-1",
            seller_id="seller-1",
            amount=Decimal("10.00"),
            currency="USD",
        )

    def test_expiration_task_moves_funded_to_expired_and_is_idempotent(self):
        # Fund escrow and force an already-past expiration time
        now = timezone.now()
        past = now - timezone.timedelta(hours=1)
        self.escrow.fund(now=past)
        Escrow.objects.filter(pk=self.escrow.pk).update(expires_at=past)

        # First run should expire it
        count1 = expire_funded_escrows()
        self.assertEqual(count1, 1)
        self.escrow.refresh_from_db()
        self.assertEqual(self.escrow.state, Escrow.State.EXPIRED)

        # Second run should be a no-op (idempotent)
        count2 = expire_funded_escrows()
        self.assertEqual(count2, 0)

    def test_race_condition_release_vs_expire_is_consistent(self):
        """
        Simulate a scenario where an escrow is eligible for expiration
        while a release request is being processed.

        Our locking & validation ensure that only one terminal state wins.
        """
        client = APIClient()
        headers = {
            "HTTP_X_USER_ID": "buyer-1",
            "HTTP_X_USER_ROLE": "buyer",
        }

        # Recreate via API so viewset permissions apply
        payload = {
            "seller_id": "seller-1",
            "amount": "20.00",
            "currency": "USD",
        }
        resp = client.post("/api/escrows/", payload, format="json", **headers)
        self.assertEqual(resp.status_code, 201)
        escrow_id = resp.data["id"]

        # Fund it
        resp = client.post(f"/api/escrows/{escrow_id}/fund/", **headers)
        self.assertEqual(resp.status_code, 200)

        # Force expiration time into the past
        escrow = Escrow.objects.get(pk=escrow_id)
        past = timezone.now() - timezone.timedelta(hours=1)
        Escrow.objects.filter(pk=escrow.pk).update(expires_at=past)

        # Run the expiration task (one contender in the race)
        expire_funded_escrows()

        # Now attempt to release; because state is no longer FUNDED,
        # the API must reject the request with a 400.
        resp = client.post(f"/api/escrows/{escrow_id}/release/", **headers)
        self.assertEqual(resp.status_code, 400)

    def test_concurrent_release_and_refund(self):
        """
        Deterministic-ish concurrency test: use a barrier to start two threads
        simultaneously, one attempting `release` and the other `refund`.
        Verify exactly one terminal state is applied.
        """
        client_main = APIClient()
        headers = {
            "HTTP_X_USER_ID": "buyer-1",
            "HTTP_X_USER_ROLE": "buyer",
        }

        # Create via API so permissions apply
        payload = {"seller_id": "seller-1", "amount": "25.00", "currency": "USD"}
        resp = client_main.post("/api/escrows/", payload, format="json", **headers)
        self.assertEqual(resp.status_code, 201)
        escrow_id = resp.data["id"]

        # Fund it
        resp = client_main.post(f"/api/escrows/{escrow_id}/fund/", **headers)
        self.assertEqual(resp.status_code, 200)

        barrier = threading.Barrier(2)
        results = {}

        def do_release():
            c = APIClient()
            barrier.wait()
            r = c.post(f"/api/escrows/{escrow_id}/release/", **headers)
            results["release"] = r.status_code

        def do_refund():
            c = APIClient()
            barrier.wait()
            r = c.post(f"/api/escrows/{escrow_id}/refund/", **headers)
            results["refund"] = r.status_code

        t1 = threading.Thread(target=do_release)
        t2 = threading.Thread(target=do_refund)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        # Threads may encounter lock contention; accept 200 (success),
        # 409 (resource busy) or 400 (invalid transition). The important
        # property is that exactly one terminal state is applied to the
        # escrow (RELEASED or REFUNDED).
        self.assertIn(results.get("release"), (200, 400, 409))
        self.assertIn(results.get("refund"), (200, 400, 409))

        escrow = Escrow.objects.get(pk=escrow_id)
        self.assertIn(escrow.state, (Escrow.State.RELEASED, Escrow.State.REFUNDED))
        self.assertNotEqual(escrow.state, Escrow.State.FUNDED)


class EscrowHeavyConcurrencyTests(TransactionTestCase):
    """Deterministic-ish heavy-load concurrency tests.

    Spawn many concurrent release/refund requests while an expiration
    task runs. The assertions verify each escrow ends up in exactly one
    terminal state and that no escrow stays in `FUNDED`.
    """

    def setUp(self):
        self.client = APIClient()
        self.headers = {
            "HTTP_X_USER_ID": "buyer-1",
            "HTTP_X_USER_ROLE": "buyer",
        }

    def test_expire_vs_release_refund_under_load(self):
        # Create and fund a small batch of escrows
        count = 10
        escrow_ids = []
        for i in range(count):
            payload = {"seller_id": f"seller-{i}", "amount": "10.00", "currency": "USD"}
            resp = self.client.post("/api/escrows/", payload, format="json", **self.headers)
            self.assertEqual(resp.status_code, 201)
            escrow_id = resp.data["id"]
            escrow_ids.append(escrow_id)

            resp = self.client.post(f"/api/escrows/{escrow_id}/fund/", **self.headers)
            self.assertEqual(resp.status_code, 200)

        # Force all expirations into the past so the expire task is a contender
        past = timezone.now() - timezone.timedelta(seconds=1)
        for eid in escrow_ids:
            Escrow.objects.filter(pk=eid).update(expires_at=past)

        # Prepare threads: for each escrow spawn a release and a refund request
        total_threads = len(escrow_ids) * 2 + 1  # +1 for the expire task runner
        barrier = threading.Barrier(total_threads)
        results = {}

        def make_release(eid):
            c = APIClient()
            barrier.wait()
            r = c.post(f"/api/escrows/{eid}/release/", **self.headers)
            results.setdefault(eid, []).append(("release", r.status_code))

        def make_refund(eid):
            c = APIClient()
            barrier.wait()
            r = c.post(f"/api/escrows/{eid}/refund/", **self.headers)
            results.setdefault(eid, []).append(("refund", r.status_code))

        def run_expire_task():
            barrier.wait()
            # Run expire task repeatedly to simulate a beating worker
            for _ in range(3):
                expire_funded_escrows()
                time.sleep(0.01)

        threads = []
        for eid in escrow_ids:
            t1 = threading.Thread(target=make_release, args=(eid,))
            t2 = threading.Thread(target=make_refund, args=(eid,))
            threads.extend([t1, t2])

        t_exp = threading.Thread(target=run_expire_task)
        threads.append(t_exp)

        # Start all threads
        for t in threads:
            t.start()

        for t in threads:
            t.join()

        # Validate results: each escrow must be in exactly one terminal state
        final_states = Escrow.objects.filter(pk__in=escrow_ids)
        for e in final_states:
            self.assertIn(e.state, (Escrow.State.RELEASED, Escrow.State.REFUNDED, Escrow.State.EXPIRED))
            # No escrow should remain FUNDED
            self.assertNotEqual(e.state, Escrow.State.FUNDED)

        # Also ensure every escrow had at least one attempted action recorded
        for eid in escrow_ids:
            self.assertIn(eid, results)
            self.assertTrue(len(results[eid]) >= 2)
