from pip.utils import cached_property
from pip.found import FoundVersion
from pip.link import INSTALLED_VERSION


class InstalledFinder(object):
    def __init__(self, req):
        self.req = req

    @cached_property
    def found(self):
        req = self.req
        if req.satisfied_by is None:
            return []
        else:
            return [
                FoundVersion(req.satisfied_by.version, INSTALLED_VERSION)
            ]
