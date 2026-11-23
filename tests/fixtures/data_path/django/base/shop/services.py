"""Fixture services: only read by the data path engine, never imported."""

from .models import Widget


def create_widget(name):
    return Widget.objects.create(name=name)
