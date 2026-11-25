"""MO2 plugin factory; keep build metadata importable without Qt."""

def createPlugin():
    from .plugin import BridgePlugin
    return BridgePlugin()
