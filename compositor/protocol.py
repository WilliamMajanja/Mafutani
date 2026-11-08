"""Wayland-like protocol/model objects (stdlib only).

Semantic model (inspired by Wayland): clients bind globals from a registry,
create surfaces, attach buffers (abstracted), commit state, and receive
input/output events. The compositor owns composition and the final scene.

This is an in-process/Python object model with serializable state, not the
Wayland wire protocol.
"""
import time

_next_id = 1

def _id():
    global _next_id
    i = _next_id; _next_id += 1; return i


class WlObject(object):
    def __init__(self, iface, oid=None):
        self.id = oid or _id()
        self.iface = iface
        self.destroyed = False
    def destroy(self): self.destroyed = True
    def to_dict(self): return {"id": self.id, "interface": self.iface}


class WlDisplay(WlObject):
    def __init__(self):
        super(WlDisplay, self).__init__("wl_display", 1)
        self.registry = WlRegistry()
        self.globals = []
        self.clients = {}
    def to_dict(self):
        d = super(WlDisplay, self).to_dict(); d["globals"] = [g.to_dict() for g in self.globals]; return d


class WlRegistry(WlObject):
    def __init__(self): super(WlRegistry, self).__init__("wl_registry")
    def bind(self, global_obj): return global_obj.bind()


class Global(WlObject):
    def __init__(self, iface, name, version=1, factory=None):
        super(Global, self).__init__("wl_global")
        self.name = name; self.version = version; self.iface_name = iface; self.factory = factory
    def bind(self): return self.factory() if self.factory else WlObject(self.iface_name)
    def to_dict(self): return {"name": self.name, "interface": self.iface_name, "version": self.version}


class WlCompositor(WlObject):
    def __init__(self, scene): super(WlCompositor, self).__init__("wl_compositor"); self.scene = scene
    def create_surface(self): return WlSurface(self.scene)
    def create_region(self): return WlRegion()


class WlRegion(WlObject):
    def __init__(self): super(WlRegion, self).__init__("wl_region"); self.rects = []


class WlSurface(WlObject):
    def __init__(self, scene):
        super(WlSurface, self).__init__("wl_surface")
        self.scene = scene
        self.buffer = None       # abstract buffer handle/metadata
        self.damage = []
        self.x = 80; self.y = 80; self.w = 480; self.h = 320
        self.title = "surface"; self.app_id = "commcoin"
        self.role = None; self.mapped = False; self.commits = 0
        self.created_at = time.time()
        scene.add_surface(self)
    def attach(self, buf, x=0, y=0): self.buffer = buf
    def damage_rect(self, x,y,w,h): self.damage.append([x,y,w,h])
    def commit(self):
        self.commits += 1; self.mapped = True; self.scene.mark_damage(self); self.damage = []
    def set_role(self, role): self.role = role
    def destroy(self): super(WlSurface, self).destroy(); self.mapped = False; self.scene.remove_surface(self)
    def to_dict(self):
        d = super(WlSurface, self).to_dict()
        d.update({"x":self.x,"y":self.y,"w":self.w,"h":self.h,"title":self.title,
                  "app_id":self.app_id,"role":self.role,"mapped":self.mapped,"commits":self.commits})
        return d


class WlOutput(WlObject):
    def __init__(self, x=0,y=0,w=1280,h=800,make="CommCoin",model="NodeCompositor"):
        super(WlOutput, self).__init__("wl_output")
        self.x=x; self.y=y; self.w=w; self.h=h; self.make=make; self.model=model; self.scale=1
    def to_dict(self): return {"id":self.id,"interface":self.iface,"x":self.x,"y":self.y,"w":self.w,"h":self.h,"make":self.make,"model":self.model,"scale":self.scale}


class WlSeat(WlObject):
    def __init__(self): super(WlSeat, self).__init__("wl_seat"); self.capabilities = ["pointer","keyboard"]
    def get_pointer(self): return WlPointer(self)
    def get_keyboard(self): return WlKeyboard(self)
    def to_dict(self): return {"id":self.id,"interface":self.iface,"capabilities":self.capabilities}


class WlPointer(WlObject):
    def __init__(self, seat): super(WlPointer, self).__init__("wl_pointer"); self.seat=seat; self.focus=None
class WlKeyboard(WlObject):
    def __init__(self, seat): super(WlKeyboard, self).__init__("wl_keyboard"); self.seat=seat; self.focus=None


class XdgWmBase(WlObject):
    def __init__(self, desktop): super(XdgWmBase, self).__init__("xdg_wm_base"); self.desktop = desktop
    def get_xdg_surface(self, surface): return XdgSurface(surface, self.desktop)

class XdgSurface(WlObject):
    def __init__(self, surface, desktop): super(XdgSurface, self).__init__("xdg_surface"); self.surface=surface; self.desktop=desktop
    def get_toplevel(self): return XdgToplevel(self.surface, self.desktop)
    def get_popup(self, parent, positioner): return XdgPopup(self.surface, parent)

class XdgToplevel(WlObject):
    def __init__(self, surface, desktop):
        super(XdgToplevel, self).__init__("xdg_toplevel")
        self.surface = surface; self.desktop = desktop; surface.set_role("xdg_toplevel")
        desktop.manage(surface)
    def set_title(self, t): self.surface.title = str(t)
    def set_app_id(self, a): self.surface.app_id = str(a)
    def close(self): self.surface.destroy()

class XdgPopup(WlObject):
    def __init__(self, surface, parent): super(XdgPopup, self).__init__("xdg_popup"); self.surface=surface; surface.set_role("xdg_popup")
