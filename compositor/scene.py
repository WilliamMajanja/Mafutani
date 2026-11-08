"""Compositing scene graph and damage tracking (Wayland-like)."""
import time

class Scene(object):
    def __init__(self):
        self.surfaces = {}  # id -> WlSurface
        self.damage_serial = 0
        self.last_damage = 0.0

    def add_surface(self, s): self.surfaces[s.id] = s
    def remove_surface(self, s): self.surfaces.pop(s.id, None)

    def mark_damage(self, s):
        self.damage_serial += 1
        self.last_damage = time.time()

    def stacked(self):
        # simple stacking: mapped surfaces by creation, later on top
        ss = [s for s in self.surfaces.values() if getattr(s,'mapped',False) and not s.destroyed]
        ss.sort(key=lambda s: (s.created_at, s.id))
        return ss

    def snapshot(self):
        return {
            "damage_serial": self.damage_serial,
            "surfaces": [s.to_dict() for s in self.stacked()],
        }
