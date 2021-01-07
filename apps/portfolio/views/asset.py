from django.forms import forms
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, render
from django.views import View

from apps.portfolio.models.asset import Asset


class AssetForm(forms.ModelForm):
    class Meta:
        model = Asset
        fields = ['name', 'symbol', 'asset_class', ]


class AssetView(View):
    def dispatch(self, request, asset_symbol="", *args, **kwargs):
        self.asset = get_object_or_404(Asset, symbol=asset_symbol)
        self.context = {
            "asset": self.asset,
        }
        return super().dispatch(request, *args, **kwargs)

    def get(self, request, *args, **kwargs):
        self.context['asset_form'] = AssetForm(instance=self.asset)
        return render(request, 'portfolio/asset_edit.html', self.context)

    def post(self, request, *args, **kwargs):
        asset_form = AssetForm(request.POST, instance=self.asset)
        if asset_form.is_valid():
            self.asset = asset_form.save()
        return JsonResponse(self.asset.__dict__)
