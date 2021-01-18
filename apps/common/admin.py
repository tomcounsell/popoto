from django.contrib import admin

from .models import Address, Country, Note, Upload


# @admin.register(Address)
# class AddressAdmin(admin.ModelAdmin):
#     list_display = (
#         'created_at',
#         'modified_at',
#         'id',
#         'line_1',
#         'line_2',
#         'line_3',
#         'city',
#         'region',
#         'postal_code',
#         'country',
#     )
#     list_filter = ('created_at', 'modified_at', 'country')
#     date_hierarchy = 'created_at'
#

# @admin.register(Country)
# class CountryAdmin(admin.ModelAdmin):
#     list_display = ('id', 'name', 'code', 'calling_code', 'currency')
#     list_filter = ('currency',)
#     search_fields = ('name',)


@admin.register(Note)
class NoteAdmin(admin.ModelAdmin):
    list_display = (
        'author',
        'author_anonymous',
        'authored_at',
        'created_at',
        'modified_at',
        'id',
        'text',
    )
    list_filter = (
        'author',
        'author_anonymous',
        'authored_at',
        'created_at',
        'modified_at',
    )
    date_hierarchy = 'created_at'


# @admin.register(Upload)
# class UploadAdmin(admin.ModelAdmin):
#     list_display = (
#         'id',
#         'created_at',
#         'modified_at',
#         'original',
#         'name',
#         'thumbnail',
#         'meta_data',
#     )
#     list_filter = ('created_at', 'modified_at')
#     search_fields = ('name',)
#     date_hierarchy = 'created_at'
