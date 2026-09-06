# SafePay

An idempotent payment processing API guarantees that a payment request,
even if accidentally sent multiple times (network glitch, retry, double
tap), only ever actually processes once.

## The problem

A customer taps "Send 500 birr" but their connection stutters right after.
Not knowing if it worked, they tap again. Without protection, that's two
charges instead of one. SafePay solves this at the database level, not
just in application code.

## Real-world alignment

Chapa (Ethiopia's first NBE-licensed private payment gateway) documents
idempotent webhook handling and HMAC-signed webhooks as core parts of
their real API. SafePay builds the same class of system.

## Stack

Django, Django REST Framework, PostgreSQL, Celery, Redis, Docker

## Setup

```bash
python -m venv venv
source venv/Scripts/activate  # or venv/bin/activate on Mac/Linux
pip install -r requirements.txt
# create a .env file (see .env.example) and a local "safepay" Postgres database
python manage.py migrate
python manage.py test
python manage.py runserver
```