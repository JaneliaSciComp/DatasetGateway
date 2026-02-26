"""Web UI forms."""

from django import forms


class GrantForm(forms.Form):
    email = forms.EmailField()
    permission = forms.IntegerField()
    version = forms.IntegerField(required=False)
