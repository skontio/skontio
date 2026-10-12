from django.contrib import admin

from skontio.audit.models import AuditEvent


@admin.register(AuditEvent)
class AuditEventAdmin(admin.ModelAdmin):
    list_display = ["seq", "created_at", "actor_kind", "actor", "tool_name", "action", "entity", "entity_id"]
    list_filter = ["actor_kind", "ai_initiated", "action"]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
