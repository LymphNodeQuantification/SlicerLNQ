"""Custom Slicer layout for the focused single-case review experience.

The default Slicer chrome (module selector, data tree, console, 3D view,
slice intersections, etc.) is way too much surface for someone whose only
job per case is "look at the heat map, click a few times, hit Save & next."
This module collapses everything that isn't the slice view + our tools
panel, and restores it on exit so a reviewer who clicks a different
module gets their normal Slicer back.

Hiding / restoring is keyed on a snapshot of `visible` flags taken at
enter time, so it's safe across module reload and the user fiddling
with views in between sessions."""
from __future__ import annotations

import qt
import slicer


# Layout XML: one big Red slice on the left, the module panel docks on
# the right. Slicer's central widget is the SliceViewers; the module
# panel is a QDockWidget docked to the right of the main window.
REVIEW_LAYOUT_XML = """
<layout type="horizontal">
  <item>
    <view class="vtkMRMLSliceNode" singletontag="Red">
      <property name="orientation" action="default">Axial</property>
      <property name="viewlabel" action="default">R</property>
      <property name="viewcolor" action="default">#F34A33</property>
    </view>
  </item>
</layout>
"""

_REVIEW_LAYOUT_ID = 10081  # arbitrary; outside Slicer's built-in IDs


def installLayout():
    """Register the single-slice layout with the layout node once.
    Idempotent — re-calling just returns the existing ID."""
    layoutNode = slicer.app.layoutManager().layoutLogic().GetLayoutNode()
    if not layoutNode.IsLayoutDescription(_REVIEW_LAYOUT_ID):
        layoutNode.AddLayoutDescription(_REVIEW_LAYOUT_ID, REVIEW_LAYOUT_XML)
    return _REVIEW_LAYOUT_ID


class ChromeState:
    """Snapshot of the main window's visible widgets at enter time. The
    Review widget keeps one of these around so exit() restores whatever
    state the user was in before they opened the module."""

    def __init__(self):
        self.layoutId = None
        self.menuBarVisible = True
        self.toolBars = {}                 # name -> bool visible
        self.dockWidgets = {}              # name -> bool visible
        self.dataProbeVisible = True
        self.helpAcknowledgementVisible = True

    @classmethod
    def capture(cls):
        s = cls()
        main = slicer.util.mainWindow()
        if main is None:
            return s
        s.layoutId = slicer.app.layoutManager().layout
        s.menuBarVisible = main.menuBar().isVisible()
        for tb in main.findChildren(qt.QToolBar):
            s.toolBars[tb.objectName] = tb.isVisible()
        for dw in main.findChildren(qt.QDockWidget):
            s.dockWidgets[dw.objectName] = dw.isVisible()
        return s


def hideChrome():
    """Hide most of Slicer's chrome and return a ChromeState snapshot
    that restoreChrome() can later use to put everything back."""
    state = ChromeState.capture()
    main = slicer.util.mainWindow()
    if main is None:
        return state

    # Menu bar stays so users can still File/quit, but toolbars get
    # collapsed because they overlap with the actions we surface in
    # the tools panel.
    main.menuBar().setVisible(True)
    for tb in main.findChildren(qt.QToolBar):
        tb.setVisible(False)

    # Hide every dock widget except the module-panel dock (where our
    # tools UI lives) and the central view.
    for dw in main.findChildren(qt.QDockWidget):
        name = (dw.objectName or "").lower()
        if "panel" in name or "module" in name:
            dw.setVisible(True)
        else:
            dw.setVisible(False)

    # Inside the module panel, hide the header chrome (Help &
    # Acknowledgement, Reload & Test) since this module is meant to be
    # used by non-developers.
    _setModulePanelHelpVisible(main, False)

    # Switch to the single-Red-slice layout.
    layoutId = installLayout()
    slicer.app.layoutManager().setLayout(layoutId)

    return state


def restoreChrome(state):
    """Put the menu / toolbars / dock widgets back the way they were."""
    if state is None:
        return
    main = slicer.util.mainWindow()
    if main is None:
        return
    main.menuBar().setVisible(state.menuBarVisible)
    for tb in main.findChildren(qt.QToolBar):
        if tb.objectName in state.toolBars:
            tb.setVisible(state.toolBars[tb.objectName])
    for dw in main.findChildren(qt.QDockWidget):
        if dw.objectName in state.dockWidgets:
            dw.setVisible(state.dockWidgets[dw.objectName])
    _setModulePanelHelpVisible(main, True)
    if state.layoutId is not None:
        slicer.app.layoutManager().setLayout(state.layoutId)


# ctk's collapsible button uses .text (with "&&" for the mnemonic) rather
# than windowTitle. Match by objectName ("HelpCollapsibleButton",
# "ReloadCollapsibleButton") since those are stable across Slicer versions.
_HEADER_OBJECT_NAMES = ("HelpCollapsibleButton", "ReloadCollapsibleButton")


def _setModulePanelHelpVisible(main, visible):
    mp = slicer.util.findChild(main, "ModulePanel") if main is not None else None
    if mp is None:
        return
    for w in mp.findChildren(qt.QWidget):
        if w.objectName in _HEADER_OBJECT_NAMES:
            w.setVisible(visible)
