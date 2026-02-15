from __future__ import annotations

from django.conf import settings
from django.db import models, transaction
from django.utils import timezone
import logging
from django.db.models import F

logger = logging.getLogger(__name__)


class Escrow(models.Model):
    """
    Mini escrow between a buyer and a seller.

    State machine:
        CREATED -> FUNDED -> [RELEASED | REFUNDED | EXPIRED]
    """

    class State(models.TextChoices):
        CREATED = "CREATED", "Created"
        FUNDED = "FUNDED", "Funded"
        RELEASED = "RELEASED", "Released"
        REFUNDED = "REFUNDED", "Refunded"
        EXPIRED = "EXPIRED", "Expired"

    buyer_id = models.CharField(max_length=64, db_index=True)
    seller_id = models.CharField(max_length=64, db_index=True)

    amount = models.DecimalField(max_digits=12, decimal_places=2)
    currency = models.CharField(max_length=8, default="USD")

    state = models.CharField(
        max_length=16,
        choices=State.choices,
        default=State.CREATED,
        db_index=True,
    )

    # Expiration is set when escrow becomes FUNDED
    expires_at = models.DateTimeField(null=True, blank=True, db_index=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    funded_at = models.DateTimeField(null=True, blank=True)
    released_at = models.DateTimeField(null=True, blank=True)
    refunded_at = models.DateTimeField(null=True, blank=True)
    expired_at = models.DateTimeField(null=True, blank=True)

    # Simple optimistic locking to guard against race conditions on updates
    version = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"Escrow#{self.pk} {self.state}"

    # --- State transition helpers -------------------------------------------------

    def _set_state(self, new_state: str) -> None:
        """
        Low-level state setter that also logs a callback.
        """
        old_state = self.state
        if old_state == new_state:
            return
        self.state = new_state
        self.version += 1  # bump version on any state change

        # Callback: log state change (could be swapped for webhooks, etc.)
        logger.info(
            "Escrow %s state change: %s -> %s (buyer=%s seller=%s)",
            self.pk,
            old_state,
            new_state,
            self.buyer_id,
            self.seller_id,
        )

    @transaction.atomic
    def fund(self, now=None) -> None:
        """
        CREATED -> FUNDED.
        Assign an expiration time. Guarded by a DB transaction.
        """
        if self.state != self.State.CREATED:
            raise ValueError("Escrow can only be funded from CREATED state.")

        now = now or timezone.now()
        self.funded_at = now

        # Determine expiration from settings; default 24h.
        seconds = getattr(settings, "ESCROW_DEFAULT_EXPIRATION_SECONDS", 24 * 3600)
        self.expires_at = now + timezone.timedelta(seconds=seconds)

        self._set_state(self.State.FUNDED)
        self.save(update_fields=["state", "version", "funded_at", "expires_at", "updated_at"])

    @transaction.atomic
    def release(self, now=None) -> None:
        """
        FUNDED -> RELEASED.
        """
        if self.state != self.State.FUNDED:
            raise ValueError("Escrow can only be released from FUNDED state.")
        now = now or timezone.now()

        # Optimistic update: attempt to flip state only if still FUNDED and
        # version matches. This avoids long row locks and lets concurrent
        # actors race with a single winner.
        updated = Escrow.objects.filter(pk=self.pk, state=self.State.FUNDED, version=self.version).update(
            state=self.State.RELEASED,
            released_at=now,
            version=F("version") + 1,
            updated_at=now,
        )
        if not updated:
            raise ValueError("Escrow cannot be released (state changed by concurrent action).")

        # Refresh instance and log callback
        self.refresh_from_db()

    @transaction.atomic
    def refund(self, now=None) -> None:
        """
        FUNDED -> REFUNDED.
        """
        if self.state != self.State.FUNDED:
            raise ValueError("Escrow can only be refunded from FUNDED state.")
        now = now or timezone.now()

        updated = Escrow.objects.filter(pk=self.pk, state=self.State.FUNDED, version=self.version).update(
            state=self.State.REFUNDED,
            refunded_at=now,
            version=F("version") + 1,
            updated_at=now,
        )
        if not updated:
            raise ValueError("Escrow cannot be refunded (state changed by concurrent action).")

        self.refresh_from_db()

    @transaction.atomic
    def expire(self, now=None) -> None:
        """
        FUNDED -> EXPIRED.
        Used by scheduled Celery task; idempotent.
        """
        if self.state != self.State.FUNDED:
            # Idempotent: do nothing if already terminal state.
            return
        now = now or timezone.now()

        updated = Escrow.objects.filter(pk=self.pk, state=self.State.FUNDED, version=self.version).update(
            state=self.State.EXPIRED,
            expired_at=now,
            version=F("version") + 1,
            updated_at=now,
        )
        if not updated:
            # Another actor moved the state concurrently; treat as idempotent.
            return

        self.refresh_from_db()
