import logging

from django.forms import ModelForm
from django.http import JsonResponse, HttpResponse, HttpResponseRedirect
from django.shortcuts import get_object_or_404, render, redirect
from django.views import View
from rest_framework import status

from apps.portfolio.models import Allocation, Asset, Portfolio


class AssetForm(ModelForm):
    class Meta:
        model = Asset
        fields = ['name', 'symbol', 'asset_class', ]

class AssetView(View):
    def dispatch(self, request, asset_symbol="", *args, **kwargs):
        self.asset = Asset.objects.filter(symbol=asset_symbol).first()
        if asset_symbol and not self.asset:
            return HttpResponse(status=status.HTTP_404_NOT_FOUND)
        return super().dispatch(request, *args, **kwargs)

    # def get(self, request, *args, **kwargs):
    #     self.asset_form = AssetForm(instance=self.asset)
    #     self.context = {
    #         "asset": self.asset,
    #         "asset_form": self.asset_form,
    #     }
    #     return render(request, 'asset/edit_asset.html', self.context)

    def post(self, request, *args, **kwargs):

        asset_form = AssetForm(request.POST, instance=self.asset)
        if asset_form.is_valid():
            self.asset = asset_form.save()

        portfolio = Portfolio.objects.filter(user=request.user, id=request.POST.get('portfolio')).first()
        if self.asset and portfolio:
            allocation, a_created = Allocation.objects.get_or_create(portfolio=portfolio, asset=self.asset)
            allocation.quantity_offline = request.POST.get('quantity_offline', 0)
            allocation.save()

        if "portfolio" in request.META.get('HTTP_REFERER'):
            return HttpResponseRedirect(request.META.get('HTTP_REFERER', '/'))
        else:
            return redirect('home')


# class AssetsView(View):
#     def dispatch(self, request, asset_symbol="", *args, **kwargs):
#         return super().dispatch(request, *args, **kwargs)
#
#     def get(self, request, *args, **kwargs):
#         self.context = {
#             "assets": Asset.objects.all(),
#             "asset_form": AssetForm(),
#         }
#         return render(request, 'asset/assets.html', self.context)
