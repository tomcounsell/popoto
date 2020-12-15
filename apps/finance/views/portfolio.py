from django.shortcuts import render, redirect
from django.views.generic import View


class Portfolio(View):
    def dispatch(self, request, *args, **kwargs):
        return super(Portfolio, self).dispatch(request, *args, **kwargs)

    def get(self, request, *args, **kwargs):
        context = {

        }
        return render(request, 'portfolio.html', context)


