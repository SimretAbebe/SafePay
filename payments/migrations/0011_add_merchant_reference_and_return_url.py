from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('payments', '0010_payment_provider_status_alter_payment_status_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='payment',
            name='merchant_reference',
            field=models.CharField(blank=True, db_index=True, max_length=100, null=True),
        ),
        migrations.AddField(
            model_name='payment',
            name='return_url',
            field=models.URLField(blank=True, max_length=500, null=True),
        ),
    ]
