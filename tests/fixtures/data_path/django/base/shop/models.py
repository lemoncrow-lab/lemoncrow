"""Fixture models: only read by the data path engine, never imported."""

from django.db import models


class Widget(models.Model):
    name = models.CharField(max_length=80)


class Order(models.Model):
    label = models.CharField(max_length=80)
