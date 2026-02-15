from __future__ import annotations

from rest_framework import serializers

from .models import Escrow


class EscrowSerializer(serializers.ModelSerializer):
    class Meta:
        model = Escrow
        fields = [
            "id",
            "buyer_id",
            "seller_id",
            "amount",
            "currency",
            "state",
            "expires_at",
            "created_at",
            "updated_at",
            "funded_at",
            "released_at",
            "refunded_at",
            "expired_at",
        ]
        read_only_fields = [
            "id",
            "state",
            "expires_at",
            "created_at",
            "updated_at",
            "funded_at",
            "released_at",
            "refunded_at",
            "expired_at",
            "buyer_id",
        ]

    def create(self, validated_data):
        """
        Buyer and seller are always taken from the authenticated user / payload,
        not from arbitrary client-provided values. Buyer is the authenticated user.
        """
        request = self.context["request"]
        user = request.user
        buyer_id = user.id

        # Require seller_id from payload (validated_data) and don't allow
        # overriding buyer from client-provided values.
        seller_id = validated_data.pop("seller_id", None)
        if not seller_id:
            raise serializers.ValidationError({"seller_id": "This field is required."})

        # Ensure buyer_id isn't coming from the client
        validated_data.pop("buyer_id", None)

        return Escrow.objects.create(
            buyer_id=buyer_id,
            seller_id=str(seller_id),
            **validated_data,
        )
