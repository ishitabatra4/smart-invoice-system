
from django.contrib import messages
from django.contrib.auth import login, logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import AuthenticationForm
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from .forms import RegisterForm, BusinessProfileForm
from .models import BusinessProfile


def register_view(request):
    if request.user.is_authenticated:
        return redirect("dashboard")

    form = RegisterForm(request.POST or None)

    if request.method == "POST" and form.is_valid():
        user = form.save()
        login(request, user)
        messages.success(request, "Your account was created successfully.")
        return redirect("dashboard")

    return render(request, "accounts/register.html", {"form": form})


def login_view(request):
    if request.user.is_authenticated:
        return redirect("dashboard")

    form = AuthenticationForm(request, data=request.POST or None)

    if request.method == "POST" and form.is_valid():
        user = form.get_user()
        login(request, user)
        messages.success(request, "Welcome back!")
        return redirect("dashboard")

    return render(request, "accounts/login.html", {"form": form})


@login_required
def dashboard_view(request):
    profile = BusinessProfile.objects.filter(user=request.user).first()

    return render(
        request,
        "accounts/dashboard.html",
        {"profile": profile},
    )


@require_POST
@login_required
def logout_view(request):
    logout(request)
    messages.success(request, "You have been logged out.")
    return redirect("login")


@login_required
def business_settings_view(request):
    profile = BusinessProfile.objects.filter(user=request.user).first()

    if profile is None:
        profile = BusinessProfile.objects.create(
            user=request.user,
            business_name=request.user.username,
            email=request.user.email or "",
        )

    if request.method == "POST":
        form = BusinessProfileForm(request.POST, instance=profile)

        if form.is_valid():
            form.save()
            messages.success(request, "Business profile updated successfully.")
            return redirect("business_settings")
    else:
        form = BusinessProfileForm(instance=profile)

    return render(
        request,
        "accounts/profile.html",
        {"form": form, "profile": profile}
    )
