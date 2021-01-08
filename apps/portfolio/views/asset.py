from django.forms import ModelForm
from django.http import JsonResponse, HttpResponse
from django.shortcuts import get_object_or_404, render
from django.views import View
from rest_framework import status

from apps.portfolio.models import Allocation


class AssetForm(ModelForm):
    class Meta:
        model = Allocation
        fields = ['name', 'symbol', 'asset_class', ]

class AllocationForm(ModelForm):
    class Meta:
        model = Allocation
        fields = ['quantity_offline', ]


class AllocationView(View):
    def dispatch(self, request, asset_symbol="", *args, **kwargs):
        self.allocation = Allocation.objects.filter(asset__symbol=asset_symbol).first()
        if asset_symbol and not self.allocation:
            return HttpResponse(status=status.HTTP_404_NOT_FOUND)
        self.context = {
            "allocation": self.allocation,
        }
        return super().dispatch(request, *args, **kwargs)

    def get(self, request, *args, **kwargs):
        self.context['allocation_form'] = AssetForm(instance=self.allocation)
        return render(request, 'portfolio/allocation_edit.html', self.context)

    def post(self, request, *args, **kwargs):
        asset_form = AssetForm(request.POST, instance=self.allocation.asset)
        allocation_form = AssetForm(request.POST, instance=self.allocation)
        if asset_form.is_valid():
            # add asset to allocation form
            pass
        if allocation_form.is_valid():
            self.allocation = allocation_form.save()
        return JsonResponse(self.allocation.__dict__)
