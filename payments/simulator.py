import uuid
from decimal import Decimal
from django.conf import settings
from django.contrib.auth.views import redirect_to_login
from django.http import Http404, HttpResponseForbidden, HttpResponseNotAllowed
from django.shortcuts import render, redirect
from django.views.decorators.csrf import csrf_protect

from payments.models import Merchant, Payment
from payments.investigator import build_investigation

DEMO_MERCHANT_NAME = "Simulator (test)"


def get_or_create_simulator_merchant() -> Merchant:
    """
    Get or create the single demo merchant used by the simulator.
    Always livemode-safe: no webhook URL, no live key, default_provider='fake'.
    """
    merchant = Merchant.objects.filter(name=DEMO_MERCHANT_NAME).first()
    if merchant:
        return merchant

    _, api_key_hash, key_prefix = Merchant.generate_test_key_pair()
    merchant, _ = Merchant.objects.get_or_create(
        name=DEMO_MERCHANT_NAME,
        defaults={
            "api_key_hash": api_key_hash,
            "key_prefix": key_prefix,
            "is_active": True,
            "live_enabled": False,
            "webhook_url": None,
            "live_webhook_url": None,
            "live_api_key_hash": None,
            "live_key_prefix": "",
            "default_provider": "fake",
            "fallback_providers": [],
        },
    )
    return merchant


def _check_simulator_access(request):
    """
    Helper to verify simulator access:
    - 404 if SIMULATOR_ENABLED is False.
    - Redirect to admin login if not authenticated.
    - 403 Forbidden if authenticated but not staff.
    Returns HttpResponse if access is denied/redirected, or None if permitted.
    """
    if not getattr(settings, "SIMULATOR_ENABLED", False):
        raise Http404("Simulator is not enabled.")

    if not request.user.is_authenticated:
        return redirect_to_login(request.get_full_path(), login_url="/admin/login/")

    if not request.user.is_staff:
        return HttpResponseForbidden("Staff access required.")

    return None


def simulator_dashboard(request):
    """
    GET /simulator/
    Staff-only view. Shows three action buttons and (if ?payment=<id> is present)
    displays the payment status, investigator summary, and timeline.
    """
    access_response = _check_simulator_access(request)
    if access_response:
        return access_response

    payment_id = request.GET.get("payment")
    investigation = None
    if payment_id:
        try:
            payment = Payment.objects.prefetch_related("status_history").get(id=payment_id)
            investigation = build_investigation(payment)
        except (Payment.DoesNotExist, ValueError):
            investigation = None

    context = {
        "investigation": investigation,
    }
    return render(request, "payments/simulator.html", context)


@csrf_protect
def simulator_action(request, action):
    """
    POST /simulator/new/<action>/
    Creates a new fake-provider payment of 100.00 for the demo merchant and advances state:
    - pay: transition_to("processing") then transition_to("succeeded")
    - fail: transition_to("processing") then transition_to("failed")
    - never: leaves payment pending
    Redirects to /simulator/?payment=<id>
    """
    access_response = _check_simulator_access(request)
    if access_response:
        return access_response

    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])

    allowed_actions = ("pay", "fail", "never")
    if action not in allowed_actions:
        raise Http404(f"Unknown action: {action}")

    merchant = get_or_create_simulator_merchant()

    payment = Payment.objects.create(
        merchant=merchant,
        livemode=False,
        provider="fake",
        amount=Decimal("100.00"),
        sender="Simulator",
        receiver="Simulator Demo",
        idempotency_key=f"sim-{uuid.uuid4().hex}",
    )

    if action == "pay":
        payment.transition_to("processing")
        payment.transition_to("succeeded")
    elif action == "fail":
        payment.transition_to("processing")
        payment.transition_to("failed")
    elif action == "never":
        pass  # remains pending

    return redirect(f"/simulator/?payment={payment.id}")
