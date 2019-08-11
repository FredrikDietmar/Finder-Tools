from . import FinderTools

def getMetaData():
    return {}

def register(app):
    return {"output_device": FinderTools.SendToFinderPlugin(), "extension": FinderTools.FinderToolsSettings()}
