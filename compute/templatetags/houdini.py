from django import template

register = template.Library()


@register.filter
def pct(value, digits=0):
    if value is None:
        return "–"
    return f"{value * 100:.{int(digits)}f}%"


@register.filter
def num(value, digits=1):
    if value is None:
        return "–"
    return f"{value:,.{int(digits)}f}"


@register.filter
def runway(days):
    if days is None:
        return "Not shrinking"
    if days <= 0:
        return "Below target"
    if days < 14:
        return f"{days:.0f} days"
    if days > 365:
        return "> 1 year"
    return f"{days / 7:.0f} weeks"
