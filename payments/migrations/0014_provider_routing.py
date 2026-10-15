import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('payments', '0013_payment_last_synced_at_payment_needs_review_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='merchant',
            name='default_provider',
            field=models.CharField(blank=True, max_length=30, null=True),
        ),
        migrations.AddField(
            model_name='merchant',
            name='fallback_providers',
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.CreateModel(
            name='ProviderAttempt',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('provider', models.CharField(max_length=30)),
                ('outcome', models.CharField(choices=[('created', 'Created'), ('failed_not_created', 'Failed — Not Created'), ('failed_unknown', 'Failed — Unknown')], max_length=30)),
                ('error_type', models.CharField(blank=True, default='', max_length=60)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('payment', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='provider_attempts', to='payments.payment')),
            ],
            options={
                'ordering': ['created_at', 'id'],
            },
        ),
    ]
