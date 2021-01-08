from django.forms import ModelForm
from django.http import JsonResponse, HttpResponse
from django.shortcuts import get_object_or_404, render
from django.views import View
from rest_framework import status

from apps.portfolio.models.asset import Asset


class AssetForm(ModelForm):
    class Meta:
        model = Asset
        fields = ['name', 'symbol', 'asset_class', ]


class AssetView(View):
    def dispatch(self, request, asset_symbol="", *args, **kwargs):
        self.asset = Asset.objects.filter(symbol=asset_symbol).first()
        if asset_symbol and not self.asset:
            return HttpResponse(status=status.HTTP_404_NOT_FOUND)
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
