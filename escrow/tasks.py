from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from celery import shared_task

from .models import Escrow


@shared_task
def expire_funded_escrows() -> int:
    """
    Scheduled task:
      - Find escrows in FUNDED state past their expiration time
      - Transition them to EXPIRED

    Idempotent and safe under concurrency because each row is updated
    in a transaction with select_for_update.
    Returns number of escrows expired.
    """
    now = timezone.now()
    expired_count = 0

    # Process in batches to avoid locking too many rows at once.
    batch_size = 100

    while True:
        # We take a small batch of candidates; each will be locked row-by-row.
        candidates = (
            Escrow.objects.filter(
                state=Escrow.State.FUNDED,
                expires_at__lte=now,
            )
            .order_by("id")[:batch_size]
        )
        if not candidates:
            break

        for escrow in candidates:
            with transaction.atomic():
                # Lock the row; if another process changed it, we reload.
                locked = Escrow.objects.select_for_update().get(pk=escrow.pk)
                # Idempotent: skip if already moved out of FUNDED by another actor.
                if locked.state != Escrow.State.FUNDED:
                    continue
                locked.expire(now=now)
                expired_count += 1

    return expired_count

