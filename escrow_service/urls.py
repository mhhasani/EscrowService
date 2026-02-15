from django.contrib import admin
from django.urls import include, path
from rest_framework import permissions, routers
from drf_yasg.views import get_schema_view
from drf_yasg import openapi

from escrow.views import EscrowViewSet
from django.http import JsonResponse

router = routers.DefaultRouter()
router.register(r"escrows", EscrowViewSet, basename="escrow")

schema_view = get_schema_view(
    openapi.Info(
        title="Mini Escrow Service API",
        default_version="v1",
        description="Backend technical assignment: mini escrow service.",
    ),
    public=True,
    permission_classes=(permissions.AllowAny,),
    authentication_classes=(),  # allow Swagger UI without auth headers
)

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/", include(router.urls)),
    path("health/", lambda request: JsonResponse({"status": "ok"})),
    path("swagger/", schema_view.with_ui("swagger", cache_timeout=0), name="schema-swagger-ui"),
    path("swagger.json", schema_view.without_ui(cache_timeout=0), name="schema-json"),
]
