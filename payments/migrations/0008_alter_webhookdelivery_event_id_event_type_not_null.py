import uuid
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('payments', '0007_populate_webhookdelivery_event_id_and_type'),
    ]

    operations = [
        migrations.AlterField(
            model_name='webhookdelivery',
            name='event_id',
            field=models.UUIDField(default=uuid.uuid4, editable=False, unique=True),
        ),
        migrations.AlterField(
            model_name='webhookdelivery',
            name='event_type',
            field=models.CharField(max_length=50),
        ),
    ]
