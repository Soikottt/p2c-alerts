"""A small Gmail-like text box (bold, italic, underline, bullets, numbers, tick boxes, text and highlight colours).
Used for the task details on the My Tasks page. Returns HTML (cleaned later by tasks.clean_note)."""
import os

import streamlit as st

_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rich_note")
_comp = None


def rich_note(initial="", key=None, height=240):
    """Show the editor. Inside a st.form the value arrives when the form button is pressed."""
    global _comp
    try:
        if _comp is None:
            import streamlit.components.v1 as components
            _comp = components.declare_component("p2c_rich_note", path=_DIR)
        v = _comp(initial=initial or "", key=key, default=initial or "", height=height)
        return v if isinstance(v, str) else (initial or "")
    except Exception:
        st.warning("The formatting box could not load, so a plain text box is shown instead.")
        return st.text_area("Details", value=initial or "", key=f"{key}_plain", height=150)
