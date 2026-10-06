import uuid
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('payments', '0005_add_cancelled_status'),
    ]

    operations = [
        migrations.AddField(
            model_name='webhookdelivery',
            name='event_id',
            field=models.UUIDField(blank=True, editable=False, null=True),
        ),
        migrations.AddField(
            model_name='webhookdelivery',
            name='event_type',
            field=models.CharField(blank=True, max_length=50, null=True),
        ),
    ]
