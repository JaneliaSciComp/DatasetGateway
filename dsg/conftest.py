"""Shared pytest fixtures for DatasetGateway tests."""

import pytest


@pytest.fixture
def api_client():
    from rest_framework.test import APIClient

    return APIClient()
