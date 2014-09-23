from pip.utils import cached_property
from pip.link import Link
from pip.finder_funcs import _package_versions


class FindLinksFinder(object):
    def __init__(self, req, config, state):
        self.req = req
        self.config = config
        self.state = state

    @cached_property
    def found(self):
        return list(_package_versions(
            # We trust every directly linked archive in find_links
            [Link(url, '-f', trusted=True) for url in self.config.find_links],
            self.req.name.lower(),
            self.config,
            self.state,
        ))
