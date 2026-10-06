import uuid
from django.db import migrations


def populate_event_data(apps, schema_editor):
    WebhookDelivery = apps.get_model('payments', 'WebhookDelivery')
    status_to_event_type = {
        'succeeded': 'payment.succeeded',
        'failed': 'payment.failed',
        'cancelled': 'payment.cancelled',
    }
    for delivery in WebhookDelivery.objects.all():
        if delivery.event_id is None:
            delivery.event_id = uuid.uuid4()
        if not delivery.event_type:
            payment_status = None
            if delivery.payment_id:
                payment = getattr(delivery, 'payment', None)
                if payment:
                    payment_status = payment.status
            if not payment_status and isinstance(delivery.payload, dict):
                payment_status = delivery.payload.get('status') or delivery.payload.get('data', {}).get('status')

            delivery.event_type = status_to_event_type.get(payment_status, 'payment.succeeded')
        delivery.save(update_fields=['event_id', 'event_type'])


class Migration(migrations.Migration):

    dependencies = [
        ('payments', '0006_webhookdelivery_event_id_event_type_nullable'),
    ]

    operations = [
        migrations.RunPython(populate_event_data, reverse_code=migrations.RunPython.noop),
    ]
