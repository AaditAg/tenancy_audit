"""AppTest cannot serialise st.date_input(value=None); skip those widgets so flows can be driven headlessly."""
from streamlit.testing.v1 import element_tree as _et

_orig = _et.get_widget_state


def _safe(node):
    try:
        return _orig(node)
    except AttributeError:
        return None


def install():
    _et.get_widget_state = _safe
    # get_widget_states() skips falsy results in recent versions; make sure None is tolerated
    import inspect
    src = inspect.getsource(_et.ElementTree.get_widget_states)
    if "if w" not in src and "is not None" not in src:
        orig_gws = _et.ElementTree.get_widget_states

        def gws(self):
            from streamlit.proto.WidgetStates_pb2 import WidgetStates
            states = WidgetStates()
            for node in self:
                pass
            return orig_gws(self)
        # fall through: patching get_widget_state is enough on current versions
