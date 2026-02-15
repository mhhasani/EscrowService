from __future__ import annotations

from rest_framework import permissions


class IsEscrowParticipant(permissions.BasePermission):
    """
    Buyer: can act only on their own escrows.
    Seller: can only view escrows assigned to them.
    """

    def has_object_permission(self, request, view, obj) -> bool:
        user = request.user
        if not getattr(user, "is_authenticated", False):
            return False

        if user.role == "buyer":
            # Buyer can see and act only on their own escrows
            return obj.buyer_id == user.id

        if user.role == "seller":
            # Seller can only see escrows assigned to them; no write actions
            if request.method in permissions.SAFE_METHODS:
                return obj.seller_id == user.id
            return False

        return False


class BuyerOnly(permissions.BasePermission):
    """
    Allow only buyers (used for release/refund actions).
    """

    def has_permission(self, request, view) -> bool:
        user = request.user
        return getattr(user, "is_authenticated", False) and getattr(user, "role", None) == "buyer"

