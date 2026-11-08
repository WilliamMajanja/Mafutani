"""CommCoin Node Compositor — Wayland-like desktop architecture (stdlib only).

A *Wayland-inspired* compositor model (not libwayland/DRM/KMS):
- protocol: display, registry, globals, surface, output, seat, pointer/keyboard,
  xdg_wm_base/toplevel semantics, commit/damage.
- scene: compositing scene graph and damage.
- desktop: shell policy/workspaces/task surfaces.
- service: state + events bridge for the node and `#/desktop` web renderer.
"""
from compositor.service import CompositorService
__all__ = ["CompositorService"]
