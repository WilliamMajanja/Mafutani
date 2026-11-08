"""Wayland-like desktop shell policy (xdg-shell inspired)."""

class DesktopShell(object):
    def __init__(self, scene):
        self.scene = scene
        self.workspaces = [{"id": 1, "name": "main", "surfaces": []}]
        self.active_workspace = 1
        self.tasks = []

    def manage(self, surface):
        if surface.id not in self.workspaces[0]["surfaces"]:
            self.workspaces[0]["surfaces"].append(surface.id)
        self._sync_tasks()

    def _sync_tasks(self):
        self.tasks = []
        for s in self.scene.stacked():
            self.tasks.append({"surface_id": s.id, "title": s.title, "app_id": s.app_id, "role": s.role})

    def focus(self, surface_id):
        # bring to top by reordering creation? scene stacks by created_at; adjust
        s = self.scene.surfaces.get(surface_id)
        if s:
            import time; s.created_at = time.time(); self._sync_tasks()

    def close(self, surface_id):
        s = self.scene.surfaces.get(surface_id)
        if s: s.destroy(); self._sync_tasks()

    def snapshot(self):
        self._sync_tasks()
        return {"workspaces": self.workspaces, "active_workspace": self.active_workspace, "tasks": self.tasks}
