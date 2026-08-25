# -*- coding: utf-8 -*-
def classFactory(iface):
    from .plugin import CompartmentCompass
    return CompartmentCompass(iface)
