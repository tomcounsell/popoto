from django.contrib import admin

from .models import Portfolio, Allocation, Asset


@admin.register(Portfolio)
class PortfolioAdmin(admin.ModelAdmin):
    list_display = (
        'id',
        'user',
        'name',
        'system_weight',
        'created_at',
        'modified_at',
    )
    list_filter = ('created_at', 'modified_at', 'user')
    search_fields = ('name',)
    date_hierarchy = 'created_at'


@admin.register(Allocation)
class AllocationAdmin(admin.ModelAdmin):
    list_display = (
        'id',
        'portfolio',
        'asset',
        'quantity_offline',
        'user_votes',
        'system_votes',
        'created_at',
        'modified_at',
    )
    list_filter = ('portfolio__user__username', 'asset', 'quantity_offline')
    search_fields = ('portfolio__user__username',)
    date_hierarchy = 'created_at'


@admin.register(Asset)
class AssetAdmin(admin.ModelAdmin):
    list_display = (
        'id',
        'symbol',
        'name',
        'asset_class',
        'exchange',
        'alternative_symbols',
        'created_at',
        'modified_at',
    )
    list_filter = ('asset_class', 'exchange')
    search_fields = ('name', 'symbol')
    date_hierarchy = 'created_at'
