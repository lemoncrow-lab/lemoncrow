"""Fixture views: only read by the data path engine, never imported."""

import logging

from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework.views import APIView

from . import services
from .models import Widget

logger = logging.getLogger(__name__)


class WidgetView(APIView):
    def post(self, request):
        name = request.data.get("name")
        services.create_widget(name)
        return Response(status=201)

    def delete(self, request, pk):
        widget = Widget.objects.get(pk=pk)
        widget.delete()
        return Response(status=204)


@api_view(["POST"])
def audit(request):
    logger.info("audit %s", request.data)
    return Response(status=202)
