from __future__ import annotations

from django.db import transaction, DatabaseError
import time
from django.utils import timezone
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from .models import Escrow
from .permissions import BuyerOnly, IsEscrowParticipant
from .serializers import EscrowSerializer


class EscrowViewSet(
    mixins.CreateModelMixin,
    mixins.RetrieveModelMixin,
    mixins.ListModelMixin,
    viewsets.GenericViewSet,
):
    """
    Escrow endpoints:
      - POST /api/escrows/           (create)
      - GET  /api/escrows/           (list own escrows)
      - GET  /api/escrows/{id}/      (retrieve)
      - POST /api/escrows/{id}/fund/ (fund)
      - POST /api/escrows/{id}/release/ (release funds)
      - POST /api/escrows/{id}/refund/  (refund to buyer)
    """

    serializer_class = EscrowSerializer
    permission_classes = [IsEscrowParticipant]
    queryset = Escrow.objects.all()

    def get_permissions(self):
        # Enforce buyer-only for create action; other actions use the
        # view's permission_classes (object-level checks apply where needed).
        if self.action == "create":
            return [BuyerOnly()]
        return [permission() for permission in self.permission_classes]

    def get_queryset(self):
        """
        Restrict visibility based on role:
          - Buyer: own escrows
          - Seller: escrows where they are the seller
        """
        user = self.request.user
        base_qs = super().get_queryset()
        if not getattr(user, "is_authenticated", False):
            return base_qs.none()

        if user.role == "buyer":
            return base_qs.filter(buyer_id=user.id)
        if user.role == "seller":
            return base_qs.filter(seller_id=user.id)
        return base_qs.none()

    def perform_create(self, serializer):
        # buyer_id comes from auth; seller_id from payload (validated in serializer).
        serializer.save()

    @action(detail=True, methods=["post"], permission_classes=[IsEscrowParticipant, BuyerOnly])
    def fund(self, request, pk=None):
        """
        Transition CREATED -> FUNDED.
        Uses a transaction + select_for_update to protect from races.
        """
        now = timezone.now()
        # Single attempt: acquire short, non-blocking lock and perform transition.
        try:
            with transaction.atomic():
                escrow = Escrow.objects.select_for_update(nowait=True).get(pk=pk)
                self.check_object_permissions(request, escrow)
                try:
                    escrow.fund(now=now)
                except ValueError as exc:
                    return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except DatabaseError:
            return Response({"detail": "Resource busy, try again."}, status=status.HTTP_409_CONFLICT)

        return Response(EscrowSerializer(escrow).data)

    @action(detail=True, methods=["post"], permission_classes=[IsEscrowParticipant, BuyerOnly])
    def release(self, request, pk=None):
        """
        Transition FUNDED -> RELEASED.
        Protected by a DB transaction to avoid races with refund/expire.
        """
        now = timezone.now()
        try:
            with transaction.atomic():
                escrow = Escrow.objects.select_for_update(nowait=True).get(pk=pk)
                self.check_object_permissions(request, escrow)
                try:
                    escrow.release(now=now)
                except ValueError as exc:
                    return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except DatabaseError:
            return Response({"detail": "Resource busy, try again."}, status=status.HTTP_409_CONFLICT)

        return Response(EscrowSerializer(escrow).data)

    @action(detail=True, methods=["post"], permission_classes=[IsEscrowParticipant, BuyerOnly])
    def refund(self, request, pk=None):
        """
        Transition FUNDED -> REFUNDED.
        Protected by a DB transaction to avoid races with release/expire.
        """
        now = timezone.now()
        try:
            with transaction.atomic():
                escrow = Escrow.objects.select_for_update(nowait=True).get(pk=pk)
                self.check_object_permissions(request, escrow)
                try:
                    escrow.refund(now=now)
                except ValueError as exc:
                    return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except DatabaseError:
            return Response({"detail": "Resource busy, try again."}, status=status.HTTP_409_CONFLICT)

        return Response(EscrowSerializer(escrow).data)
