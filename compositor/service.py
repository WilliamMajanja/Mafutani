"""CommCoin Node Compositor service - integrates Wayland-like model with node.

Exposes state for API/UI rendering and provides actions (create surface, move,
focus, close) used by the `#/desktop` web compositor shell.
"""
from compositor.scene import Scene
from compositor.desktop import DesktopShell
from compositor import protocol


class CompositorService(object):
    def __init__(self):
        self.scene = Scene()
        self.desktop = DesktopShell(self.scene)
        self.display = protocol.WlDisplay()
        # register globals
        comp = protocol.WlCompositor(self.scene)
        seat = protocol.WlSeat()
        out = protocol.WlOutput()
        xdg = protocol.XdgWmBase(self.desktop)
        self.globals = {
            "wl_compositor": comp, "wl_seat": seat, "wl_output": out, "xdg_wm_base": xdg,
        }
        self.display.globals = [
            protocol.Global("wl_compositor", 1, 5, lambda: comp),
            protocol.Global("wl_seat", 2, 8, lambda: seat),
            protocol.Global("wl_output", 3, 4, lambda: out),
            protocol.Global("xdg_wm_base", 4, 5, lambda: xdg),
        ]
        self._boot_surfaces()

    def _boot_surfaces(self):
        # Default node desktop surfaces (Wayland-like toplevels)
        self.create_toplevel("CommCoin Node", "commcoin.node", 60, 60, 520, 320, role="node_status")
        self.create_toplevel("Merkle / Chain", "commcoin.chain", 140, 120, 520, 300, role="chain")
        self.create_toplevel("Wallet & Miner", "commcoin.wallet", 220, 180, 480, 280, role="wallet")

    def create_toplevel(self, title, app_id, x, y, w, h, role=None):
        surf = self.globals["wl_compositor"].create_surface()
        surf.x, surf.y, surf.w, surf.h = int(x), int(y), int(w), int(h)
        xdg_s = self.globals["xdg_wm_base"].get_xdg_surface(surf)
        tl = xdg_s.get_toplevel()
        tl.set_title(title); tl.set_app_id(app_id)
        if role: surf.meta_role = role
        surf.attach({"type": "shm", "format": "abstract"}, 0, 0)
        surf.commit()
        return surf.to_dict()

    def snapshot(self, node_info=None):
        return {
            "type": "commcoin-compositor",
            "wayland_like": True,
            "display": self.display.to_dict(),
            "outputs": [self.globals["wl_output"].to_dict()],
            "seat": self.globals["wl_seat"].to_dict(),
            "scene": self.scene.snapshot(),
            "shell": self.desktop.snapshot(),
            "node": node_info or {},
        }

    def action(self, cmd, payload=None):
        p = payload or {}
        if cmd == "create_toplevel":
            return self.create_toplevel(p.get("title","Window"), p.get("app_id","app"),
                                        p.get("x",100), p.get("y",100), p.get("w",480), p.get("h",320))
        if cmd == "focus": self.desktop.focus(int(p.get("id"))); return {"ok":True}
        if cmd == "close": self.desktop.close(int(p.get("id"))); return {"ok":True}
        if cmd == "move":
            s = self.scene.surfaces.get(int(p.get("id")))
            if s: s.x = int(p.get("x",s.x)); s.y = int(p.get("y",s.y)); self.scene.mark_damage(s)
            return {"ok":True}
        if cmd == "title":
            s = self.scene.surfaces.get(int(p.get("id")))
            if s: s.title = str(p.get("title",s.title)); self.desktop._sync_tasks()
            return {"ok":True}
        return {"error": "unknown command"}


_service = None
def get_service():
    global _service
    if _service is None: _service = CompositorService()
    return _service
