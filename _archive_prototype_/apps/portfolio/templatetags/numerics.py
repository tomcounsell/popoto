from django import template

register = template.Library()

@register.filter
def percentage(value, arg=0):
    value *= 100
    value, precision = round(value, arg), arg
    return '{:.{prec}f}%'.format(value, prec=precision)
