from django.contrib.auth.views import LoginView
from django.urls import path

from . import views
from .forms import LoginForm


app_name = 'cards'

urlpatterns = [
    path('', views.home, name='home'),
    path('signup/', views.signup, name='signup'),
    path('login/', LoginView.as_view(
        template_name='cards/login.html', authentication_form=LoginForm,
    ), name='login'),
    path('logout/', views.CardPickLogoutView.as_view(), name='logout'),
    path('cards/', views.card_list, name='card_list'),
    path('cards/add/', views.card_add, name='card_add'),
    path('cards/<int:pk>/', views.card_detail, name='card_detail'),
    path('cards/<int:pk>/delete/', views.card_delete, name='card_delete'),
    path('transactions/', views.transaction_list, name='transaction_list'),
]
