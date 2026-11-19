"""Email-style rich text editor for the task details (Streamlit custom component, no extra pip package needed).

Toolbar: headings, text size, bold / italic / underline / strikethrough, text + highlight colour, bullets, numbers,
indent, alignment, links, tick boxes, clear formatting.
Keyboard shortcuts (all work inside the box):
    Ctrl+B bold            Ctrl+I italic            Ctrl+U underline         Ctrl+K insert link
    Ctrl+Shift+8 bullets   Ctrl+Shift+7 numbers     Ctrl+] indent more       Ctrl+[ indent less
    Ctrl+\\ clear format    Alt+Shift+5 strikethrough
    Ctrl+Shift+.  bigger text      Ctrl+Shift+,  smaller text
    Ctrl+Shift+L / E / R  align left / centre / right
The value is raw HTML (a string that always starts with a tag). tasks.clean_note() makes it safe before it is stored / e-mailed.
The editor lives in the folder  rich_note/index.html  (upload that folder to GitHub next to this file).
"""
import os

import streamlit as st

_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rich_note")
_comp = None


def rich_note(initial="", key=None, height=320, placeholder="Write the details here..."):
    """Show the editor and return its HTML. Inside a st.form the value arrives when the form button is pressed.
    ALWAYS read the text afterwards with value(key) (or the return value): both are the same string."""
    global _comp
    if not os.path.isfile(os.path.join(_DIR, "index.html")):
        st.warning("The formatting box files are missing (folder rich_note/index.html), so a plain text box is shown instead. "
                   "Upload the rich_note folder to GitHub next to app.py.")
        return st.text_area("Details", value=initial or "", key=key, height=160)
    try:
        if _comp is None:
            import streamlit.components.v1 as components
            _comp = components.declare_component("p2c_rich_note", path=_DIR)
        v = _comp(initial=initial or "", key=key, default=initial or "", height=int(height), placeholder=placeholder)
    except Exception as ex:
        st.warning(f"The formatting box could not load ({ex}), so a plain text box is shown instead.")
        return st.text_area("Details", value=initial or "", key=key, height=160)
    return v if isinstance(v, str) else (initial or "")


def value(key, default=""):
    """The text of the editor with this key, as it is right now (works inside form callbacks)."""
    v = st.session_state.get(key, default)
    return v if isinstance(v, str) else default
