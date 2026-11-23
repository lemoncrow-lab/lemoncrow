"""Fixture views: only read by the data path engine, never imported."""

import logging

from django.shortcuts import get_object_or_404
from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework.views import APIView

from . import services
from .models import Order

logger = logging.getLogger(__name__)


class WidgetView(APIView):
    def post(self, request):
        name = request.data.get("name")
        services.create_widget(services.normalize(name))
        return Response(status=201)


@api_view(["POST"])
def audit(request):
    logger.info("audit %s", request.data)
    return Response(status=202)


@api_view(["PUT"])
def rename(request, pk):
    order = get_object_or_404(Order, pk=pk)
    order.label = "renamed"
    order.save()
    return Response(status=200)
