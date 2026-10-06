from django.apps import AppConfig
from django.db.models.signals import post_delete, post_save


class BillingConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'billing'

    def ready(self):
        from . import rates
        from .models import CcaAdjustment, UtilityRate

        for model in (UtilityRate, CcaAdjustment):
            for signal in (post_save, post_delete):
                signal.connect(
                    lambda **kw: rates.clear_cache(),
                    sender=model,
                    weak=False,
                    dispatch_uid=f"rates-cache-{model.__name__}-{signal.__class__.__name__}-{id(signal)}",
                )
