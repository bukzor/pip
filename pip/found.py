from pip.link import INSTALLED_VERSION
from pip.exceptions import UnsupportedWheel
from pip.pep425tags import supported_tags
from pip.utils import cached_property, is_prerelease
from pip.wheel import Wheel, wheel_ext
from pip._vendor import pkg_resources


class FoundVersion(object):
    """Represents a version of a package, found at a particular place."""

    def __init__(self, version, link):
        self.version = version
        self.link = link

    @cached_property
    def parsed_version(self):
        return pkg_resources.parse_version(self.version)

    @cached_property
    def currently_installed(self):
        return self.link == INSTALLED_VERSION

    @cached_property
    def prerelease(self):
        return is_prerelease(self.version)

    @classmethod
    def sort(cls, versions):
        """
        Bring the latest version (and wheels) to the front, but maintain the
        existing ordering as secondary. See the docstring for `_link_sort_key`
        for details. This function is isolated for easier unit testing.
        """
        return sorted(
            versions,
            key=cls._sort_key,
            reverse=True
        )

    def _sort_key(self):
        """
        Function used to generate link sort key for FoundVersion's.
        The greater the return value, the more preferred it is.
        If not finding wheels, then sorted by version only.
        If finding wheels, then the sort order is by version, then:
          1. existing installs
          2. wheels ordered via Wheel.support_index_min()
          3. source archives
        Note: it was considered to embed this logic into the Link
              comparison operators, but then different sdist links
              with the same version, would have to be considered equal
        """
        support_num = len(supported_tags)
        if self.currently_installed:
            pri = 1
        elif self.link.ext == wheel_ext:
            wheel = Wheel(
                self.link.filename
            )  # can raise InvalidWheelFilename
            if not wheel.supported():
                raise UnsupportedWheel(
                    "%s is not a supported wheel for this platform. "
                    "It can't be sorted." % wheel.filename
                )
            pri = -(wheel.support_index_min())
        else:  # sdist
            pri = -(support_num)
        return (self.parsed_version, pri)

    def __repr__(self):
        return '%s(%r, %r)' % (
            self.__class__.__name__,
            self.version,
            self.link,
        )
