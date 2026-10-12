from django.urls import path

from skontio.web import views

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("einrichtung/", views.setup, name="setup"),
    path("kunden/", views.customer_list, name="customer_list"),
    path("kunden/neu/", views.customer_create, name="customer_create"),
    path("kunden/<int:pk>/", views.customer_detail, name="customer_detail"),
    path("leistungen/", views.item_list, name="item_list"),
    path("leistungen/neu/", views.item_create, name="item_create"),
    path("leistungen/<int:pk>/", views.item_edit, name="item_edit"),
    path("leistungen/<int:pk>/archivieren/", views.item_archive, name="item_archive"),
]
