import os
from pathlib import Path

from trame import Singleton, state, controller as ctrl
from trame.internal.app import get_app_instance

from simput.core import (
    ProxyManager,
    UIManager,
    ProxyDomainManager,
    Proxy,
    ProxyObjectAdapter,
)
from simput.ui.web import VuetifyResolver

from pv_visualizer.app.engine.proxymanager.const import SETTINGS_PROXIES

from . import paraview, domains, definitions
from .decorators import AdvancedDecorator

try:
    from paraview import simple
except:
    pass

PENDING = True

# -----------------------------------------------------------------------------
# PV <=> Simput proxy state exchange
# -----------------------------------------------------------------------------


class ParaViewProxyObjectAdapter(ProxyObjectAdapter):
    @staticmethod
    def commit(simput_proxy):
        pv_proxy = simput_proxy.object
        change_count = 0

        for name in simput_proxy.edited_property_names:
            value = simput_proxy[name]
            if isinstance(value, Proxy):
                value = paraview.unwrap(value.object if value else None)
            elif value is None:
                continue

            property = pv_proxy.GetProperty(name)

            if isinstance(value, (list, tuple)):
                for i, v in enumerate(value):
                    before = property.GetElement(i)
                    property.SetElement(i, v)
                    after = property.GetElement(i)
                    if before != after:
                        change_count += 1
            elif property.GetClassName() in [
                "vtkSMInputProperty",
                "vtkSMProxyProperty",
            ]:
                before = property.GetProxy(0)
                property.SetProxy(0, value)
                after = property.GetProxy(0)
                if before != after:
                    change_count += 1
            else:
                before = property.GetElement(0)
                property.SetElement(0, value)
                after = property.GetElement(0)
                if before != after:
                    change_count += 1

        if change_count:
            pv_proxy.UpdateVTKObjects()

        return change_count

    @staticmethod
    def reset(simput_proxy):
        pv_proxy = simput_proxy.object
        for name in simput_proxy.edited_property_names:
            pv_property = pv_proxy.GetProperty(name)
            if pv_property is not None:
                pv_property.ClearUncheckedElements()

    @staticmethod
    def fetch(simput_proxy):
        pv_proxy = simput_proxy.object
        for name in simput_proxy.list_property_names():
            pv_property = paraview.unwrap(pv_proxy.GetProperty(name))

            if pv_property is None:
                # print(f"No property {name} for proxy {pv_proxy.GetXMLName()}")
                continue

            # Custom handling for proxy
            property_class = pv_property.GetClassName()
            if property_class in ["vtkSMProxyProperty", "vtkSMInputProperty"]:
                value = []
                size = pv_property.GetNumberOfProxies()
                for i in range(size):
                    proxy = pv_property.GetProxy(i)
                    value.append(PV_PXM.handle_proxy(proxy))

                if size > 1:
                    simput_proxy.set_property(name, value)
                elif len(value):
                    simput_proxy.set_property(name, value[0])
            else:
                size = pv_property.GetNumberOfElements()
                if size == 0:
                    continue

                if size > 1:
                    value = []
                    for i in range(size):
                        value.append(pv_property.GetElement(i))
                else:
                    value = pv_property.GetElement(0)

                # print(f"{property_class}({size})::{name} = {value} ({type(value)})")
                simput_proxy.set_property(name, value)

        simput_proxy.commit()

    @staticmethod
    def update(simput_proxy, *property_names):
        pv_proxy = simput_proxy.object

        for name in property_names:
            value = simput_proxy[name]
            if isinstance(value, Proxy):
                value = paraview.unwrap(value.object if value else None)
            elif value is None:
                continue

            property = pv_proxy.GetProperty(name)

            if property.GetClassName() in [
                "vtkSMInputProperty",
                "vtkSMProxyProperty",
            ]:
                if isinstance(value, (list, tuple)):
                    for i, v in enumerate(value):
                        if isinstance(v, Proxy):
                            v = paraview.unwrap(v.object if v else None)
                        property.SetUncheckedProxy(i, v)
                else:
                    property.SetUncheckedProxy(0, value)
            elif isinstance(value, (list, tuple)):
                for i, v in enumerate(value):
                    property.SetUncheckedElement(i, v)
            else:
                property.SetUncheckedElement(0, value)


PV_ADAPTER = ParaViewProxyObjectAdapter()
# -----------------------------------------------------------------------------


class PVObjectFactory:
    def __init__(self):
        self._next = None

    def next(self, proxy):
        self._next = proxy

    def create(self, name, **kwargs):
        obj = self._next
        self._next = None

        return obj


@Singleton
class ParaviewProxyManager:
    def __init__(self):
        # Manage relationship between pv and simput
        self._factory = PVObjectFactory()
        self._cache_proxy_def = {}
        self._id_pv_to_simput = {}
        domains.register_domains()

        # Load Simput models and layouts
        self._pxm = ProxyManager(self._factory)
        ui_resolver = VuetifyResolver()
        self._ui_manager = UIManager(self._pxm, ui_resolver)
        self._pdm = ProxyDomainManager()
        self._pxm.add_life_cycle_listener(self._pdm)
        self._pxm.on(self.on_pxm_event)

        # Controller binding
        ctrl.on_data_change.add(self.on_active_change)
        ctrl.on_delete = self.on_proxy_delete

        # No active simput proxy just yet
        state.source_id = 0
        state.representation_id = 0

        # Debug
        self._write_definitions_base = str(
            Path(Path(__file__).parent / "definitions").resolve().absolute()
        )

    def update_active_proxies(self):
        setting_proxies = []
        for item in SETTINGS_PROXIES:
            setting_proxies.append(
                {
                    "name": item[0],
                    "id": self.handle_proxy(simple.GetSettingsProxy(item[1])),
                    "icon": item[2],
                }
            )
        state.setting_proxies = setting_proxies

        view = simple.GetActiveView()
        if view is None:
            view = simple.GetRenderView()
            simple.SetActiveView(view)
        state.view_id = self.handle_proxy(view)

    @property
    def factory(self):
        return self._factory

    @property
    def pxm(self):
        return self._pxm

    @property
    def pdm(self):
        return self._pdm

    @property
    def ui_manager(self):
        return self._ui_manager

    def on_pxm_event(self, topic, **kwrags):
        if topic == "commit":
            ctrl.on_data_change()  # Trigger render

    def reload_domains(self):
        app = get_app_instance()
        app.update(ref="simput", method="reload", args=["domain"])

    def reload_data(self):
        app = get_app_instance()
        app.update(ref="simput", method="reload", args=["data"])

    def on_active_change(self, **kwargs):
        source = simple.GetActiveSource()
        view = simple.GetActiveView()
        representation = None
        if source is not None:
            representation = simple.GetRepresentation(proxy=source, view=view)
            state.active_proxy_source_id = source.GetGlobalIDAsString()
            state.active_proxy_representation_id = representation.GetGlobalIDAsString()

        state.source_id = self.handle_proxy(source)
        state.representation_id = self.handle_proxy(representation)
        state.view_id = self.handle_proxy(view)
        ctrl.pv_reaction_representation_scalarbar_update()

    def on_proxy_delete(self, pv_id):
        """!!! THIS IS NOT WORKING !!!"""
        pv_view = simple.GetActiveView()

        s_id = self._id_pv_to_simput[pv_id]
        s_source = self._pxm.get(s_id)

        pv_source = s_source.object
        pv_rep = simple.GetRepresentation(proxy=pv_source, view=pv_view)

        s_id = self._id_pv_to_simput[pv_rep.GetGlobalIDAsString()]
        s_rep = self._pxm.get(s_id)

        self._pxm.delete(s_rep.id)
        self._pxm.delete(s_source.id)

        pv_rep.Visibility = 0  # Not sure why still around after delete
        simple.Delete(pv_rep)
        simple.Delete(pv_source)

        # Trigger some life cycle events
        ctrl.on_active_proxy_change()
        ctrl.on_data_change()

    def handle_proxy(self, proxy):
        if proxy is None:
            return 0

        proxy_type = definitions.proxy_type(proxy)
        proxy_id = proxy.GetGlobalIDAsString()

        if self.pxm.get_definition(proxy_type) is None:
            self._proxy_ensure_definition(proxy)

        if proxy_id not in self._id_pv_to_simput:
            self._proxy_ensure_binding(proxy)

        return self._id_pv_to_simput.get(proxy_id, 0)

    def _proxy_extract_sub(
        self, proxy, list_to_fill=None, property_types=["vtkSMProxyProperty"]
    ):
        if list_to_fill is None:
            list_to_fill = []

        nb_groups = proxy.GetNumberOfPropertyGroups()
        for g_idx in range(nb_groups):
            group = proxy.GetPropertyGroup(g_idx)
            nb_props = group.GetNumberOfProperties()
            for p_idx in range(nb_props):
                prop = group.GetProperty(p_idx)
                if prop.GetClassName() in property_types:
                    size = prop.GetNumberOfProxies()
                    for i in range(size):
                        s_proxy = prop.GetProxy(i)
                        if s_proxy is not None:
                            # print("add sub proxy", s_proxy.GetClassName())
                            list_to_fill.append(s_proxy)

        return list_to_fill

    def _write_definition(self, proxy_type, extension, content):
        file_name = (
            Path(self._write_definitions_base)
            / f"{'/'.join(proxy_type.split('__'))}.{extension}"
        )
        os.makedirs(file_name.parent, exist_ok=True)
        with open(file_name, "w") as file:
            file.write(content)

    def _proxy_ensure_definition(self, proxy):
        proxy_type = definitions.proxy_type(proxy)
        if self.pxm.get_definition(proxy_type) is not None:
            return

        # Look first on our dependencies
        sub_proxies = self._proxy_extract_sub(proxy)
        for sub_p in sub_proxies:
            self._proxy_ensure_definition(sub_p)

        # Add definition
        model_yaml = definitions.proxy_model(proxy)
        ui_xml = definitions.proxy_ui(proxy)
        self._pxm.load_model(yaml_content=model_yaml)
        self._ui_manager.load_language(yaml_content=model_yaml)
        self._ui_manager.load_ui(xml_content=ui_xml)

        # Write definitions
        if self._write_definitions_base:
            self._write_definition(proxy_type, "yaml", model_yaml)
            self._write_definition(proxy_type, "xml", ui_xml)

    def _proxy_ensure_binding(self, proxy):
        proxy = paraview.unwrap(proxy)
        proxy_id = proxy.GetGlobalIDAsString()
        if proxy_id in self._id_pv_to_simput:
            return

        # Reserve spot to prevent any recursive loop
        self._id_pv_to_simput[proxy_id] = PENDING

        # Look first on our dependencies
        sub_proxies = self._proxy_extract_sub(proxy)
        for sub_p in sub_proxies:
            self._proxy_ensure_binding(sub_p)

        # Take care of ourself
        proxy_type = definitions.proxy_type(proxy)
        self._factory.next(proxy)
        simput_entry = self._pxm.create(proxy_type, _object_adapter=PV_ADAPTER)
        self._id_pv_to_simput[proxy_id] = simput_entry.id

        # Read property from proxy and update simput entry
        simput_entry.fetch()

        return simput_entry.id

    def refresh_active_proxies(self):
        for _id in [state.source_id, state.representation_id]:
            simput_rep = self._pxm.get(_id)
            if simput_rep:
                simput_rep.fetch()

        self.reload_data()
        self.reload_domains()


# -----------------------------------------------------------------------------

PV_PXM = ParaviewProxyManager()
PV_PXM.update_active_proxies()

# -----------------------------------------------------------------------------
# Life cycle listener
# -----------------------------------------------------------------------------

ctrl.on_active_proxy_change.add(ParaviewProxyManager().on_active_change)

# ---------------------------------------------------------
# Listeners
# ---------------------------------------------------------

ctrl.pxm_refresh_active_proxies = PV_PXM.refresh_active_proxies


@state.change("ui_advanced")
def update_advanced(ui_advanced, **kwargs):
    AdvancedDecorator.advance_mode = ui_advanced
    PV_PXM.reload_domains()
